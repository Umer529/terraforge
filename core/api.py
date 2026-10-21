"""REST interface used by the iOS app and the web dashboard (JSON over HTTPS).

Chunked upload protocol (Chapter 5, "Offline First Capture and Resumable Upload"):
    POST /api/uploads                         -> {upload_id, total_chunks, chunk_size}
    GET  /api/uploads/<id>                    -> {received: [...]}  (resume point)
    PUT  /api/uploads/<id>/chunks/<n>         body = bytes, header X-Chunk-SHA256
    POST /api/uploads/<id>/complete           -> assembles, verifies, queues job
"""
import hashlib
import math
import os
from pathlib import Path

import numpy as np
from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from . import compare as compare_mod
from . import geo
from .auth import api_view, get_scan_or_404, json_body, role_of, visible_scans, visible_sites
from .models import ApiToken, LocationFix, Measurement, Output, Profile, ProcessingJob, Scan, UploadSession

MAX_CHUNK = 8 * 1024 * 1024


def upload_dir(upload):
    d = Path(settings.STORAGE_ROOT) / "uploads" / str(upload.pk)
    d.mkdir(parents=True, exist_ok=True)
    return d


def received_chunks(upload):
    return sorted(int(p.stem) for p in upload_dir(upload).glob("*.part"))


def scan_json(scan, detail=False):
    job = scan.jobs.order_by("-id").first()
    data = {
        "id": scan.pk, "title": scan.title, "site": scan.site.name, "site_id": scan.site_id,
        "status": scan.status, "captured_at": scan.captured_at.isoformat(),
        "captured_by": scan.captured_by.username, "device_model": scan.device_model,
        "path_length_m": scan.path_length_m, "notes": scan.notes,
        "origin": [scan.origin_lat, scan.origin_lon] if scan.georeferenced else None,
        "job": {"state": job.state, "log": job.log,
                "started_at": job.started_at and job.started_at.isoformat(),
                "finished_at": job.finished_at and job.finished_at.isoformat()} if job else None,
        "outputs": {o.kind: {"url": f"/api/scans/{scan.pk}/files/{o.kind}", "crs": o.crs,
                             "bbox": o.bounding_box, "meta": o.meta} for o in scan.outputs.all()},
    }
    if scan.status == Scan.UPLOADING and hasattr(scan, "upload"):
        data["upload"] = {"received": len(received_chunks(scan.upload)), "total": scan.upload.total_chunks}
    if detail:
        fixes = list(scan.fixes.order_by("pk").values_list("latitude", "longitude"))
        data["track"] = fixes[:: max(1, len(fixes) // 300)]
        data["gps_fixes"] = len(fixes)
    return data


# --------------------------------------------------------------------------- auth & lookup

@csrf_exempt
def login(request):
    """Exchange username/password for an API token (mobile app)."""
    if request.method != "POST":
        return JsonResponse({"error": "method not allowed"}, status=405)
    body = json_body(request)
    user = authenticate(request, username=body.get("username"), password=body.get("password"))
    if user is None:
        return JsonResponse({"error": "invalid credentials"}, status=401)
    token = ApiToken.objects.create(user=user)
    return JsonResponse({"token": token.key, "username": user.username, "role": role_of(user)})


@api_view(["GET"])
def sites(request):
    return JsonResponse({"sites": [{"id": s.pk, "name": s.name, "centre": [s.centre_lat, s.centre_lon]}
                                   for s in visible_sites(request.user)]})


@api_view(["GET"])
def scans(request):
    qs = visible_scans(request.user)
    if request.GET.get("site"):
        qs = qs.filter(site_id=request.GET["site"])
    return JsonResponse({"scans": [scan_json(s) for s in qs.prefetch_related("outputs", "jobs")]})


@api_view(["GET"])
def scan_detail(request, scan_id):
    return JsonResponse(scan_json(get_scan_or_404(request.user, scan_id), detail=True))


@api_view(["GET"])
def scan_file(request, scan_id, kind):
    scan = get_scan_or_404(request.user, scan_id)
    if kind == "package":
        if not scan.package_path or not os.path.exists(scan.package_path):
            raise Http404("package not available")
        return FileResponse(open(scan.package_path, "rb"), as_attachment=True, filename=Path(scan.package_path).name)
    if kind == "ply":
        return ply_download(scan)
    out = scan.output(kind)
    if out is None or not os.path.exists(out.file_path):
        raise Http404("output not available")
    if kind == Output.GEOTIFF:
        return FileResponse(open(out.file_path, "rb"), as_attachment=True, filename=f"terraforge_scan{scan.pk}.tif")
    return FileResponse(open(out.file_path, "rb"))


def ply_download(scan):
    """Processed point cloud (local ENU metres) as a binary PLY."""
    out = scan.output(Output.POINTCLOUD)
    if out is None:
        raise Http404("output not available")
    n = out.meta["count"]
    raw = np.fromfile(out.file_path, dtype=np.uint8)
    rec = np.empty(n, dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
    xyz = np.frombuffer(raw[: n * 12].tobytes(), dtype="<f4").reshape(n, 3)
    rgb = raw[n * 12:].reshape(n, 3)
    rec["x"], rec["y"], rec["z"] = xyz.T
    rec["r"], rec["g"], rec["b"] = rgb.T
    header = (f"ply\nformat binary_little_endian 1.0\ncomment TerraForge scan {scan.pk}, {out.crs}\n"
              f"element vertex {n}\nproperty float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n").encode()
    resp = HttpResponse(header + rec.tobytes(), content_type="application/octet-stream")
    resp["Content-Disposition"] = f'attachment; filename="terraforge_scan{scan.pk}.ply"'
    return resp


# --------------------------------------------------------------------------- upload

@api_view(["POST"])
def upload_create(request):
    if role_of(request.user) == Profile.REVIEWER:
        return JsonResponse({"error": "reviewers cannot upload scans"}, status=403)
    body = json_body(request)
    site = visible_sites(request.user).filter(pk=body["site_id"]).first()
    if site is None:
        raise Http404("site not found")
    size = int(body["size"])
    chunk_size = min(int(body.get("chunk_size") or 2 * 1024 * 1024), MAX_CHUNK)
    if size <= 0:
        raise ValueError("empty file")
    with transaction.atomic():
        scan = Scan.objects.create(
            site=site, captured_by=request.user, title=body.get("title") or Path(body["filename"]).stem,
            notes=body.get("notes", ""), device_model=body.get("device_model", ""),
            captured_at=parse_datetime(body["captured_at"]) if body.get("captured_at") else timezone.now(),
        )
        upload = UploadSession.objects.create(
            scan=scan, filename=Path(body["filename"]).name, size=size, chunk_size=chunk_size,
            total_chunks=math.ceil(size / chunk_size), sha256=body.get("sha256", ""),
            options={k: body[k] for k in ("up_axis",) if body.get(k)},
        )
        if body.get("latitude") not in (None, "") and body.get("longitude") not in (None, ""):
            LocationFix.objects.create(scan=scan, latitude=float(body["latitude"]), longitude=float(body["longitude"]))
    return JsonResponse({"upload_id": str(upload.pk), "scan_id": scan.pk,
                         "chunk_size": chunk_size, "total_chunks": upload.total_chunks}, status=201)


def _get_upload(request, upload_id):
    upload = UploadSession.objects.select_related("scan").filter(pk=upload_id, scan__captured_by=request.user).first()
    if upload is None:
        raise Http404("upload not found")
    return upload


@api_view(["GET"])
def upload_status(request, upload_id):
    upload = _get_upload(request, upload_id)
    return JsonResponse({"upload_id": str(upload.pk), "scan_id": upload.scan_id, "complete": upload.complete,
                         "total_chunks": upload.total_chunks, "chunk_size": upload.chunk_size,
                         "received": received_chunks(upload)})


@api_view(["PUT"])
def upload_chunk(request, upload_id, index):
    upload = _get_upload(request, upload_id)
    if upload.complete:
        return JsonResponse({"error": "upload already complete"}, status=409)
    if not 0 <= index < upload.total_chunks:
        raise ValueError("chunk index out of range")
    data = request.body
    expected = upload.chunk_size if index < upload.total_chunks - 1 else upload.size - upload.chunk_size * index
    if len(data) != expected:
        return JsonResponse({"error": f"chunk {index} has {len(data)} bytes, expected {expected}"}, status=400)
    digest = hashlib.sha256(data).hexdigest()
    sent = request.headers.get("X-Chunk-SHA256", "").lower()
    if sent and sent != digest:
        # The client re-sends the chunk (sequence diagram "Upload a Scan").
        return JsonResponse({"error": "checksum mismatch", "index": index}, status=422)
    part = upload_dir(upload) / f"{index:06d}.part"
    tmp = part.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, part)  # a chunk is either fully present or absent
    return JsonResponse({"index": index, "sha256": digest, "received": len(received_chunks(upload))})


@api_view(["POST"])
def upload_complete(request, upload_id):
    upload = _get_upload(request, upload_id)
    scan = upload.scan
    if upload.complete:
        return JsonResponse({"scan_id": scan.pk, "status": scan.status})
    missing = sorted(set(range(upload.total_chunks)) - set(received_chunks(upload)))
    if missing:
        return JsonResponse({"error": "missing chunks", "missing": missing}, status=409)

    dest = Path(settings.STORAGE_ROOT) / "scans" / str(scan.pk)
    dest.mkdir(parents=True, exist_ok=True)
    package = dest / upload.filename
    h = hashlib.sha256()
    with open(package, "wb") as out:
        for i in range(upload.total_chunks):
            chunk = (upload_dir(upload) / f"{i:06d}.part").read_bytes()
            h.update(chunk)
            out.write(chunk)
    if upload.sha256 and h.hexdigest() != upload.sha256.lower():
        package.unlink()
        return JsonResponse({"error": "package checksum mismatch"}, status=422)

    for p in upload_dir(upload).glob("*.part"):
        p.unlink()
    with transaction.atomic():
        upload.complete = True
        upload.save(update_fields=["complete"])
        scan.package_path = str(package)
        scan.status = Scan.QUEUED
        scan.save(update_fields=["package_path", "status"])
        ProcessingJob.objects.create(scan=scan)
    return JsonResponse({"scan_id": scan.pk, "status": scan.status, "sha256": h.hexdigest()})


@api_view(["POST"])
def scan_reprocess(request, scan_id):
    scan = get_scan_or_404(request.user, scan_id)
    if not scan.package_path:
        raise ValueError("scan has no uploaded package")
    scan.status = Scan.QUEUED
    scan.save(update_fields=["status"])
    ProcessingJob.objects.create(scan=scan)
    return JsonResponse(scan_json(scan))


# --------------------------------------------------------------------------- measurements

ASSUMED_Z_ERROR = 0.02  # m; LiDAR vertical noise to be replaced by the evaluation results


def measurement_json(m):
    return {"id": m.pk, "kind": m.kind, "label": m.label, "value": m.value, "unit": m.unit,
            "reference_surface": m.reference_surface, "points": m.points, "details": m.details,
            "created_by": m.created_by.username, "created_at": m.created_at.isoformat()}


def load_dem(scan):
    out = scan.output(Output.DEM)
    if out is None:
        raise ValueError("scan is not processed yet")
    data = np.load(out.file_path)
    return out, data["dem"], {k: data[k].item() for k in ("xmin", "ymax", "cell", "nx", "ny")}


def compute_measurement(scan, kind, pts, reference):
    """Server-side, authoritative computation. Points are local ENU metres."""
    pts = np.asarray(pts, float)
    if kind == Measurement.DISTANCE:
        if len(pts) != 2:
            raise ValueError("distance needs 2 points")
        d = float(np.linalg.norm(pts[1] - pts[0]))
        horiz = float(np.linalg.norm(pts[1, :2] - pts[0, :2]))
        return d, "m", "", {"horizontal_m": horiz, "vertical_m": float(pts[1, 2] - pts[0, 2]),
                            "error_m": ASSUMED_Z_ERROR * math.sqrt(2) + 0.01 * d}
    if len(pts) < 3:
        raise ValueError(f"{kind} needs at least 3 points")
    if kind == Measurement.AREA:
        a = geo.polygon_area(pts[:, :2])
        perimeter = float(np.linalg.norm(np.diff(np.vstack([pts[:, :2], pts[:1, :2]]), axis=0), axis=1).sum())
        return a, "m2", "plan (horizontal) area", {"perimeter_m": perimeter, "error_m2": perimeter * ASSUMED_Z_ERROR}
    if kind == Measurement.VOLUME:
        _, dem, meta = load_dem(scan)
        v = geo.volume_in_polygon(dem, meta, pts, reference)
        main = max(v["above_m3"], v["below_m3"])
        v["error_m3"] = v["plan_area_m2"] * ASSUMED_Z_ERROR
        v["direction"] = "above reference (fill / stockpile)" if v["above_m3"] >= v["below_m3"] else "below reference (cut / excavation)"
        return main, "m3", v["reference"], v
    raise ValueError(f"unknown measurement kind {kind}")


@api_view(["GET", "POST"])
def measurements(request, scan_id):
    scan = get_scan_or_404(request.user, scan_id)
    if request.method == "GET":
        return JsonResponse({"measurements": [measurement_json(m) for m in scan.measurements.select_related("created_by")]})
    body = json_body(request)
    kind = body["kind"]
    value, unit, ref, details = compute_measurement(scan, kind, body["points"], body.get("reference", "plane"))
    if not body.get("save", True):
        return JsonResponse({"value": value, "unit": unit, "reference_surface": ref, "details": details})
    m = Measurement.objects.create(scan=scan, output=scan.output(Output.POINTCLOUD), created_by=request.user, kind=kind,
                                   label=body.get("label", ""), value=value, unit=unit, reference_surface=ref,
                                   points=body["points"], details=details)
    return JsonResponse(measurement_json(m), status=201)


@api_view(["DELETE"])
def measurement_delete(request, measurement_id):
    m = Measurement.objects.filter(pk=measurement_id, scan__in=visible_scans(request.user)).first()
    if m is None:
        raise Http404("measurement not found")
    m.delete()
    return JsonResponse({"deleted": measurement_id})


# --------------------------------------------------------------------------- compare

@api_view(["GET"])
def compare(request):
    a = get_scan_or_404(request.user, int(request.GET["a"]))
    b = get_scan_or_404(request.user, int(request.GET["b"]))
    if a.site_id != b.site_id:
        return JsonResponse({"error": "scans belong to different sites"}, status=400)
    if a.status != Scan.READY or b.status != Scan.READY:
        return JsonResponse({"error": "both scans must be processed"}, status=400)
    return JsonResponse(compare_mod.compare_scans(a, b, refresh=request.GET.get("refresh") == "1"))


@api_view(["GET"])
def compare_image(request, a_id, b_id):
    get_scan_or_404(request.user, a_id)
    get_scan_or_404(request.user, b_id)
    path = compare_mod.result_dir(a_id, b_id) / "diff.png"
    if not path.exists():
        raise Http404("comparison not computed")
    return FileResponse(open(path, "rb"))
