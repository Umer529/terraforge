"""Processing worker pipeline (Chapter 5, "Processing Worker").

Each stage is a plain function with clear inputs and outputs and writes a
line to the job log, so a failure can be traced to its stage. The demo
builds the LiDAR-only model (the "fast preview" path in the report); the
photogrammetry fusion stage slots in after `load` in a later iteration.

Scan package (zip) written by the iOS app:
    manifest.json         title, captured_at, device_model, up_axis, scan_file, ...
    location_fixes.csv    timestamp,latitude,longitude,altitude,horizontal_accuracy,cam_x,cam_y,cam_z
    <scan_file>           LiDAR mesh / point cloud (.ply, .obj, .las, ...)
    photos/               optional, counted only in the demo
A bare .ply/.las/.obj/.xyz export (e.g. from 3d Scanner App) is accepted too.
"""
import csv
import json
import shutil
import time
import traceback
import zipfile
from pathlib import Path

import numpy as np
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import geo
from .models import LocationFix, Output, ProcessingJob, Scan

VIEWER_MAX_POINTS = 1_500_000


class JobLog:
    def __init__(self, job):
        self.job = job
        self.t0 = time.monotonic()

    def __call__(self, msg):
        line = f"[{time.monotonic() - self.t0:6.1f}s] {msg}"
        print(f"  job {self.job.pk}: {line}")
        self.job.log += line + "\n"
        self.job.save(update_fields=["log"])


def scan_dir(scan):
    d = Path(settings.STORAGE_ROOT) / "scans" / str(scan.pk)
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- stages

def unpack(scan, log):
    pkg = Path(scan.package_path)
    work = scan_dir(scan) / "package"
    manifest, fixes, cam = {}, [], []
    if zipfile.is_zipfile(pkg):
        with zipfile.ZipFile(pkg) as zf:
            zf.extractall(work)
        # iOS zips the session folder itself, so the files may sit one level down
        found = next(iter(sorted(work.rglob("manifest.json"))), None)
        if found:
            work = found.parent
        if (work / "manifest.json").exists():
            manifest = json.loads((work / "manifest.json").read_text())
        if (work / "location_fixes.csv").exists():
            with open(work / "location_fixes.csv", newline="") as fh:
                for row in csv.DictReader(fh):
                    fixes.append({
                        "latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
                        "altitude": _num(row.get("altitude")), "horizontal_accuracy": _num(row.get("horizontal_accuracy")),
                        "recorded_at": row.get("timestamp") or None,
                    })
                    if row.get("cam_x") not in (None, ""):
                        cam.append([float(row["cam_x"]), float(row["cam_y"]), float(row["cam_z"])])
        name = manifest.get("scan_file")
        scan_file = work / name if name else next(p for p in sorted(work.rglob("*")) if p.suffix.lower() in geo.POINT_EXTS)
        photos = len(list((work / "photos").glob("*"))) if (work / "photos").exists() else 0
        log(f"Unpacked package: {scan_file.name}, {len(fixes)} GPS fixes, {photos} photos")
    else:
        scan_file = pkg
        log(f"Received bare export {pkg.name} (no manifest)")
    up = manifest.get("up_axis") or scan.upload.options.get("up_axis") or geo.default_up_axis(scan_file)
    return scan_file, manifest, fixes, (np.array(cam) if len(cam) == len(fixes) and cam else None), up


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(scan_file, up_axis, log):
    xyz, rgb = geo.load_points(scan_file)
    if len(xyz) < 100:
        raise ValueError(f"only {len(xyz)} points in {scan_file.name}")
    xyz = geo.to_enu(xyz, up_axis)
    log(f"Loaded {len(xyz):,} points ({'with' if rgb is not None else 'no'} colour), up axis = {up_axis}")
    return xyz, rgb


def clean(xyz, rgb, log):
    n0 = len(xyz)
    xyz, rgb = geo.voxel_downsample(xyz, rgb, 0.01)
    xyz, rgb = geo.remove_outliers(xyz, rgb)
    log(f"Cleaned: 1 cm voxel grid + outlier filter, {n0:,} -> {len(xyz):,} points")
    return xyz, rgb


def georeference(scan, xyz, fixes, cam, up_axis, log):
    if fixes:
        cam_enu = geo.to_enu(cam, up_axis)[:, :2] if cam is not None else None
        xyz, info = geo.georeference(xyz, fixes, cam_enu)
        scan.origin_lat, scan.origin_lon, scan.origin_alt = info["lat0"], info["lon0"], info["alt0"]
        err = f"~{info['rms_m']} m" if info["rms_m"] is not None else "unknown (manual location)"
        log(f"Georeferenced: {info['method']}, heading correction {info['rotation_deg']} deg, "
            f"position error {err}, origin {info['lat0']:.6f}, {info['lon0']:.6f}")
        return xyz, info
    centre = np.array([*xyz[:, :2].mean(0), 0.0])
    xyz = xyz - centre
    m = np.eye(4)
    m[:3, 3] = -centre
    log("No GPS fixes: kept in local scan coordinates (not georeferenced)")
    return xyz, {"method": "none", "offset": centre.tolist(), "matrix": m}


def apply_manifest(scan, manifest):
    """Fill scan metadata the uploader left at defaults from the app's manifest."""
    if manifest.get("title") and scan.title == Path(scan.upload.filename).stem:
        scan.title = manifest["title"]
    if manifest.get("captured_at"):
        scan.captured_at = parse_datetime(manifest["captured_at"]) or scan.captured_at
    if manifest.get("device_model"):
        scan.device_model = manifest["device_model"]
    if manifest.get("notes") and not scan.notes:
        scan.notes = manifest["notes"]


def save_fixes(scan, fixes):
    LocationFix.objects.filter(scan=scan).delete()
    LocationFix.objects.bulk_create([
        LocationFix(scan=scan, latitude=f["latitude"], longitude=f["longitude"], altitude=f["altitude"],
                    horizontal_accuracy=f["horizontal_accuracy"],
                    recorded_at=parse_datetime(f["recorded_at"]) if f.get("recorded_at") else None)
        for f in fixes
    ])


def make_outputs(job, scan, xyz, rgb, georef, log, model=None):
    """model: (path to textured GLB/glTF, 4x4 matrix from model coords to the scan frame)."""
    out = scan_dir(scan) / "out"
    out.mkdir(exist_ok=True)
    bbox = {k: round(float(v), 3) for k, v in zip(("xmin", "ymin", "zmin", "xmax", "ymax", "zmax"), [*xyz.min(0), *xyz.max(0)])}
    scan.outputs.all().delete()

    if model is not None:
        src, matrix = model
        dest = out / f"model{src.suffix.lower()}"
        shutil.copyfile(src, dest)
        Output.objects.create(job=job, scan=scan, kind=Output.MODEL, file_path=str(dest), bounding_box=bbox,
                              meta={"matrix": matrix.tolist(), "format": src.suffix.lower().lstrip(".")})
        log(f"Textured 3D model kept for the viewer ({dest.stat().st_size / 1e6:.1f} MB)")

    dem, meta = geo.build_dem(xyz)
    np.savez_compressed(out / "dem.npz", dem=dem, **{k: np.array(v) for k, v in meta.items()})
    covered = float((~np.isnan(dem)).sum() * meta["cell"] ** 2)
    Output.objects.create(job=job, scan=scan, kind=Output.DEM, file_path=str(out / "dem.npz"), bounding_box=bbox,
                          meta={**meta, "covered_m2": round(covered, 1)})
    log(f"Elevation grid {meta['nx']}x{meta['ny']} at {meta['cell'] * 100:.0f} cm, covers {covered:,.1f} m2")

    lo, hi = geo.render_dem_png(dem, meta, out / "preview.png")
    preview_meta = {"z_lo": lo, "z_hi": hi}
    if scan.georeferenced:
        crs = geo.write_geotiff(dem, meta, scan.origin_lat, scan.origin_lon, out / "terrain.tif")
        Output.objects.create(job=job, scan=scan, kind=Output.GEOTIFF, file_path=str(out / "terrain.tif"), crs=crs, bounding_box=bbox)
        south, west = geo.local_to_latlon(meta["xmin"], meta["ymax"] - meta["ny"] * meta["cell"], scan.origin_lat, scan.origin_lon)
        north, east = geo.local_to_latlon(meta["xmin"] + meta["nx"] * meta["cell"], meta["ymax"], scan.origin_lat, scan.origin_lon)
        preview_meta["bounds_latlon"] = [[float(south), float(west)], [float(north), float(east)]]
        log(f"Wrote GeoTIFF map ({crs})")
    Output.objects.create(job=job, scan=scan, kind=Output.PREVIEW, file_path=str(out / "preview.png"), meta=preview_meta)

    if len(xyz) > VIEWER_MAX_POINTS:
        pick = np.sort(np.random.default_rng(0).choice(len(xyz), VIEWER_MAX_POINTS, replace=False))
        xyz, rgb = xyz[pick], (rgb[pick] if rgb is not None else None)
    if rgb is None:
        rgb = geo.ramp_colors(xyz[:, 2], *np.percentile(xyz[:, 2], [2, 98]))
    with open(out / "cloud.bin", "wb") as fh:
        fh.write(xyz.astype(np.float32).tobytes())
        fh.write(rgb.astype(np.uint8).tobytes())
    Output.objects.create(job=job, scan=scan, kind=Output.POINTCLOUD, file_path=str(out / "cloud.bin"), bounding_box=bbox,
                          crs="local ENU (m)" if scan.georeferenced else "local scan (m)",
                          meta={"count": int(len(xyz)), "georef": georef})
    log(f"Viewer point cloud: {len(xyz):,} points")


# --------------------------------------------------------------------------- job runner

def run_job(job):
    scan = job.scan
    log = JobLog(job)
    job.started_at = timezone.now()
    job.save(update_fields=["started_at"])
    Scan.objects.filter(pk=scan.pk).update(status=Scan.PROCESSING)
    try:
        scan_file, manifest, fixes, cam, up = unpack(scan, log)
        xyz, rgb = load(scan_file, up, log)
        xyz, rgb = clean(xyz, rgb, log)
        if not fixes:
            fixes = [{"latitude": f.latitude, "longitude": f.longitude, "altitude": f.altitude,
                      "horizontal_accuracy": f.horizontal_accuracy, "recorded_at": None} for f in scan.fixes.all()]
        else:
            save_fixes(scan, fixes)
        apply_manifest(scan, manifest)
        xyz, georef = georeference(scan, xyz, fixes, cam, up, log)
        model = None
        if scan_file.suffix.lower() == ".glb":  # self-contained; a .gltf may reference side files
            model = (scan_file, georef["matrix"] @ geo.enu_matrix(up))
        georef.pop("matrix")
        if manifest.get("path_length_m"):
            scan.path_length_m = float(manifest["path_length_m"])
        elif cam is not None and len(cam) > 1:
            scan.path_length_m = float(np.linalg.norm(np.diff(cam, axis=0), axis=1).sum())
        make_outputs(job, scan, xyz, rgb, georef, log, model)
        scan.status = Scan.READY
        scan.save()
        job.state = ProcessingJob.DONE
        log("Done")
    except Exception as exc:  # report any stage failure on the job and scan
        log(f"FAILED: {exc}\n{traceback.format_exc()}")
        job.state = ProcessingJob.FAILED
        Scan.objects.filter(pk=scan.pk).update(status=Scan.FAILED)
    job.finished_at = timezone.now()
    job.save(update_fields=["state", "finished_at"])


def claim_next_job():
    """Atomically take the oldest queued job, so several workers can run."""
    for job in ProcessingJob.objects.filter(state=ProcessingJob.QUEUED).order_by("created_at")[:5]:
        if ProcessingJob.objects.filter(pk=job.pk, state=ProcessingJob.QUEUED).update(state=ProcessingJob.RUNNING):
            job.state = ProcessingJob.RUNNING
            return job
    return None
