"""Compare two scans of the same site (use case "Compare Scans").

Phone scans taken on different days have different AR session origins, so
scan B is first aligned onto scan A with ICP (seeded by GPS when both scans
are georeferenced), then the elevation change B - A gives cut and fill.
"""
import json
from pathlib import Path

import numpy as np
from django.conf import settings

from . import geo
from .models import Output


def result_dir(a_id, b_id):
    d = Path(settings.STORAGE_ROOT) / "compare" / f"{a_id}_{b_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_cloud(scan):
    out = scan.output(Output.POINTCLOUD)
    n = out.meta["count"]
    raw = np.fromfile(out.file_path, dtype=np.uint8)
    return np.frombuffer(raw[: n * 12].tobytes(), dtype=np.float32).reshape(n, 3).astype(np.float64)


def compare_scans(a, b, refresh=False):
    d = result_dir(a.pk, b.pk)
    cached = d / "result.json"
    if cached.exists() and not refresh:
        return json.loads(cached.read_text())

    xyz_a, xyz_b = load_cloud(a), load_cloud(b)
    seed = None
    if a.georeferenced and b.georeferenced:
        east, north = geo.latlon_to_local(b.origin_lat, b.origin_lon, a.origin_lat, a.origin_lon)
        seed = [float(east), float(north), 0.0]
    matrix, rms = geo.align_clouds(xyz_b, xyz_a, initial_offset=seed)
    xyz_b_aligned = xyz_b @ matrix[:3, :3].T + matrix[:3, 3]

    extent = np.ptp(xyz_a[:, :2], axis=0).max()
    diff, meta, stats = geo.dem_difference(xyz_a, xyz_b_aligned, cell=max(0.05, extent / 400))
    limit = float(np.nanpercentile(np.abs(diff), 98)) or 0.1
    geo.render_diff_png(diff, d / "diff.png", limit)

    result = {
        "a": a.pk, "b": b.pk,
        "matrix_b_to_a": matrix.tolist(),
        "alignment_rms_m": round(rms, 3),
        "yaw_deg": round(float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0]))), 1),
        "shift_m": [round(float(v), 2) for v in matrix[:3, 3]],
        "seeded_by_gps": seed is not None,
        "grid": {**meta, "limit_m": limit},
        "image": f"/api/compare/{a.pk}/{b.pk}/diff.png",
        **{k: round(v, 3) for k, v in stats.items()},
    }
    cached.write_text(json.dumps(result))
    return result
