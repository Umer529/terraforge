"""Point cloud and terrain geometry used by the worker and the measurement API.

Conventions
-----------
* Local frame is ENU in metres: x = east, y = north, z = up, origin at the
  scan's georeference origin (or the cloud centroid if there is no GPS).
* ARKit exports are y-up with -z pointing forward/north, so (x, y, z)_arkit
  maps to (x, -z, y)_enu.
* Elevation grids (DEM) are row 0 = north, like GeoTIFF.
"""
import math
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

EARTH_R = 6378137.0
POINT_EXTS = {".ply", ".las", ".laz", ".obj", ".xyz", ".txt", ".csv"}


# --------------------------------------------------------------------------- loading

def default_up_axis(path):
    return "z" if Path(path).suffix.lower() in {".las", ".laz"} else "y"


def load_points(path):
    """Return (xyz float64 Nx3, rgb uint8 Nx3 or None) in the file's own axes."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".ply":
        from plyfile import PlyData

        v = PlyData.read(str(path))["vertex"].data
        xyz = np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float64)
        names = v.dtype.names
        rgb = None
        for keys in (("red", "green", "blue"), ("r", "g", "b"), ("diffuse_red", "diffuse_green", "diffuse_blue")):
            if all(k in names for k in keys):
                rgb = np.column_stack([v[k] for k in keys])
                break
        return xyz, _to_uint8(rgb)
    if ext in {".las", ".laz"}:
        import laspy

        las = laspy.read(str(path))
        xyz = np.column_stack([las.x, las.y, las.z]).astype(np.float64)
        rgb = None
        if {"red", "green", "blue"} <= set(las.point_format.dimension_names):
            rgb = np.column_stack([las.red, las.green, las.blue])
        return xyz, _to_uint8(rgb)
    if ext == ".obj":
        rows = []
        with open(path, "r", errors="ignore") as fh:
            for line in fh:
                if line.startswith("v "):
                    rows.append(line.split()[1:7])
        width = min(len(r) for r in rows)
        arr = np.array([r[:width] for r in rows], dtype=np.float64)
        rgb = arr[:, 3:6] if width >= 6 else None
        return arr[:, :3], _to_uint8(rgb)
    if ext in {".xyz", ".txt", ".csv"}:
        arr = np.loadtxt(path, delimiter="," if ext == ".csv" else None, comments="#", ndmin=2)
        rgb = arr[:, 3:6] if arr.shape[1] >= 6 else None
        return arr[:, :3], _to_uint8(rgb)
    raise ValueError(f"unsupported point cloud format: {ext}")


def _to_uint8(rgb):
    if rgb is None:
        return None
    rgb = np.asarray(rgb, dtype=np.float64)
    top = rgb.max() if rgb.size else 0
    if top <= 1.0:
        rgb = rgb * 255.0
    elif top > 255:
        rgb = rgb / 257.0  # 16-bit LAS colour
    return np.clip(rgb, 0, 255).astype(np.uint8)


def to_enu(xyz, up_axis):
    if up_axis == "y":
        return np.column_stack([xyz[:, 0], -xyz[:, 2], xyz[:, 1]])
    return xyz.copy()


# --------------------------------------------------------------------------- cleaning

def voxel_downsample(xyz, rgb, voxel):
    keys = np.floor(xyz / voxel).astype(np.int64)
    _, idx = np.unique(keys, axis=0, return_index=True)
    idx.sort()
    return xyz[idx], (rgb[idx] if rgb is not None else None)


def remove_outliers(xyz, rgb, k=8, std_ratio=2.5):
    """Statistical outlier removal: drop points far from their k neighbours."""
    if len(xyz) < k + 1:
        return xyz, rgb
    dist, _ = cKDTree(xyz).query(xyz, k=k + 1, workers=-1)
    mean = dist[:, 1:].mean(axis=1)
    keep = mean < mean.mean() + std_ratio * mean.std()
    return xyz[keep], (rgb[keep] if rgb is not None else None)


# --------------------------------------------------------------------------- georeference

def latlon_to_local(lat, lon, lat0, lon0):
    """Small-area east/north offsets (m) of lat/lon from an origin."""
    lat, lon = np.asarray(lat, float), np.asarray(lon, float)
    east = np.radians(lon - lon0) * EARTH_R * math.cos(math.radians(lat0))
    north = np.radians(lat - lat0) * EARTH_R
    return east, north


def local_to_latlon(east, north, lat0, lon0):
    lat = lat0 + np.degrees(np.asarray(north, float) / EARTH_R)
    lon = lon0 + np.degrees(np.asarray(east, float) / (EARTH_R * math.cos(math.radians(lat0))))
    return lat, lon


def fit_rigid_2d(src, dst):
    """Least-squares rotation + translation mapping src (Nx2) onto dst (Nx2)."""
    cs, cd = src.mean(0), dst.mean(0)
    h = (src - cs).T @ (dst - cd)
    u, _, vt = np.linalg.svd(h)
    r = vt.T @ u.T
    if np.linalg.det(r) < 0:
        vt[1] *= -1
        r = vt.T @ u.T
    return r, cd - r @ cs


def georeference(xyz, fixes, cam_track=None):
    """Place the cloud in an ENU frame anchored at the first GPS fix.

    fixes: list of dicts with latitude/longitude/altitude.
    cam_track: Nx2 ENU camera positions (from ARKit) paired with fixes. When
    present and the walk is long enough, a 2D rigid fit between the camera
    path and the GPS path recovers both offset and heading.
    Returns (xyz_enu, info dict).
    """
    lat0, lon0 = fixes[0]["latitude"], fixes[0]["longitude"]
    alts = [f["altitude"] for f in fixes if f.get("altitude") is not None]
    alt0 = float(np.median(alts)) if alts else None
    e, n = latlon_to_local([f["latitude"] for f in fixes], [f["longitude"] for f in fixes], lat0, lon0)
    gps = np.column_stack([e, n])

    if cam_track is not None and len(cam_track) >= 3 and np.ptp(cam_track, axis=0).max() > 5.0:
        r, t = fit_rigid_2d(cam_track, gps)
        residual = np.sqrt(((cam_track @ r.T + t - gps) ** 2).sum(1).mean())
        out = xyz.copy()
        out[:, :2] = xyz[:, :2] @ r.T + t
        heading = math.degrees(math.atan2(r[1, 0], r[0, 0]))
        return out, {"method": "camera path fitted to GPS track", "rms_m": round(float(residual), 2),
                     "rotation_deg": round(heading, 1), "lat0": lat0, "lon0": lon0, "alt0": alt0}

    # Translation only: the AR session origin is where the walk started.
    acc = [f.get("horizontal_accuracy") for f in fixes if f.get("horizontal_accuracy")]
    return xyz.copy(), {"method": "anchored at first GPS fix (no heading)", "rms_m": round(float(np.median(acc)), 2) if acc else None,
                        "rotation_deg": 0.0, "lat0": lat0, "lon0": lon0, "alt0": alt0}


def utm_epsg(lat, lon):
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


# --------------------------------------------------------------------------- elevation grid

def build_dem(xyz, cell=None, fill_iters=4, bounds=None):
    """Mean elevation per grid cell. bounds=(xmin, ymin, xmax, ymax) crops/pins the grid."""
    if bounds is None:
        bounds = (xyz[:, 0].min(), xyz[:, 1].min(), xyz[:, 0].max(), xyz[:, 1].max())
    xmin, ymin, xmax, ymax = (float(b) for b in bounds)
    inside = (xyz[:, 0] >= xmin) & (xyz[:, 0] <= xmax) & (xyz[:, 1] >= ymin) & (xyz[:, 1] <= ymax)
    xyz = xyz[inside]
    if cell is None:
        cell = max(0.05, max(xmax - xmin, ymax - ymin) / 1200.0)
    nx = int(math.ceil((xmax - xmin) / cell)) + 1
    ny = int(math.ceil((ymax - ymin) / cell)) + 1
    ix = ((xyz[:, 0] - xmin) / cell).astype(np.int64)
    iy = ((ymax - xyz[:, 1]) / cell).astype(np.int64)
    flat = iy * nx + ix
    sums = np.bincount(flat, weights=xyz[:, 2], minlength=nx * ny)
    counts = np.bincount(flat, minlength=nx * ny)
    with np.errstate(invalid="ignore", divide="ignore"):
        dem = (sums / counts).reshape(ny, nx)
    dem[counts.reshape(ny, nx) == 0] = np.nan
    dem = fill_small_holes(dem, fill_iters)
    return dem, {"xmin": float(xmin), "ymax": float(ymax), "cell": float(cell), "nx": nx, "ny": ny}


def fill_small_holes(dem, iters):
    """Fill gaps a few cells wide with the mean of their filled neighbours."""
    dem = dem.copy()
    for _ in range(iters):
        nan = np.isnan(dem)
        if not nan.any():
            break
        padded = np.pad(dem, 1, constant_values=np.nan)
        stack = np.stack([padded[1 + dy:padded.shape[0] - 1 + dy, 1 + dx:padded.shape[1] - 1 + dx]
                          for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx])
        valid = ~np.isnan(stack)
        n = valid.sum(0)
        with np.errstate(invalid="ignore"):
            mean = np.nansum(stack, 0) / n
        take = nan & (n >= 3)
        dem[take] = mean[take]
    return dem


def cell_centres(meta, rows, cols):
    x = meta["xmin"] + (cols + 0.5) * meta["cell"]
    y = meta["ymax"] - (rows + 0.5) * meta["cell"]
    return x, y


def sample_dem(dem, meta, x, y):
    col = np.floor((np.asarray(x) - meta["xmin"]) / meta["cell"]).astype(int)
    row = np.floor((meta["ymax"] - np.asarray(y)) / meta["cell"]).astype(int)
    ok = (row >= 0) & (row < dem.shape[0]) & (col >= 0) & (col < dem.shape[1])
    out = np.full(np.shape(x), np.nan)
    out[ok] = dem[row[ok], col[ok]]
    return out


# --------------------------------------------------------------------------- rendering

RAMP = np.array([[47, 79, 140], [52, 146, 168], [118, 186, 110], [222, 205, 96], [205, 120, 58], [140, 70, 50]], float)
DIVERGING = np.array([[33, 102, 172], [146, 197, 222], [247, 247, 247], [244, 165, 130], [178, 24, 43]], float)


def ramp_colors(values, lo, hi, ramp=RAMP):
    t = np.clip((np.asarray(values, float) - lo) / max(hi - lo, 1e-9), 0, 1) * (len(ramp) - 1)
    i = np.minimum(t.astype(int), len(ramp) - 2)
    f = (t - i)[..., None]
    return (ramp[i] * (1 - f) + ramp[i + 1] * f).astype(np.uint8)


def hillshade(dem, cell, azimuth=315.0, altitude=45.0):
    filled = np.where(np.isnan(dem), np.nanmean(dem), dem)
    gy, gx = np.gradient(filled, cell)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = math.radians(360 - azimuth + 90), math.radians(altitude)
    shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(shade, 0, 1)


def render_dem_png(dem, meta, path):
    valid = ~np.isnan(dem)
    lo, hi = np.nanpercentile(dem, [2, 98])
    rgb = ramp_colors(np.where(valid, dem, lo), lo, hi).astype(float)
    rgb *= (0.55 + 0.45 * hillshade(dem, meta["cell"]))[..., None]
    rgba = np.dstack([rgb.astype(np.uint8), np.where(valid, 235, 0).astype(np.uint8)])
    Image.fromarray(rgba, "RGBA").save(path)
    return float(lo), float(hi)


def render_diff_png(diff, path, limit):
    valid = ~np.isnan(diff)
    rgb = ramp_colors(np.where(valid, diff, 0), -limit, limit, DIVERGING)
    rgba = np.dstack([rgb, np.where(valid, 240, 0).astype(np.uint8)])
    Image.fromarray(rgba, "RGBA").save(path)


def write_geotiff(dem, meta, lat0, lon0, path):
    """Write the DEM as a GeoTIFF in the local UTM zone. Returns the CRS string."""
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import transform as warp

    epsg = utm_epsg(lat0, lon0)
    (e0,), (n0,) = warp("EPSG:4326", f"EPSG:{epsg}", [lon0], [lat0])
    affine = from_origin(e0 + meta["xmin"], n0 + meta["ymax"], meta["cell"], meta["cell"])
    with rasterio.open(path, "w", driver="GTiff", height=dem.shape[0], width=dem.shape[1], count=1,
                       dtype="float32", crs=f"EPSG:{epsg}", transform=affine, nodata=-9999.0,
                       compress="deflate") as dst:
        dst.write(np.where(np.isnan(dem), -9999.0, dem).astype(np.float32), 1)
    return f"EPSG:{epsg}"


# --------------------------------------------------------------------------- measurement

def polygon_area(xy):
    x, y = np.asarray(xy, float).T
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def points_in_polygon(px, py, poly):
    inside = np.zeros(px.shape, bool)
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        crosses = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x2 - x1) * (py - y1) / (y2 - y1) + x1
        inside ^= crosses & (px < xint)
    return inside


def volume_in_polygon(dem, meta, poly_xyz, reference="plane"):
    """Cut/fill volume between the terrain and a reference surface inside a polygon.

    reference: 'plane'  - best-fit plane through the polygon corners (pile/base)
               'lowest' - horizontal plane at the lowest corner
    """
    poly = np.asarray(poly_xyz, float)
    xs, ys = poly[:, 0], poly[:, 1]
    c0 = max(int((xs.min() - meta["xmin"]) / meta["cell"]), 0)
    c1 = min(int((xs.max() - meta["xmin"]) / meta["cell"]) + 1, dem.shape[1])
    r0 = max(int((meta["ymax"] - ys.max()) / meta["cell"]), 0)
    r1 = min(int((meta["ymax"] - ys.min()) / meta["cell"]) + 1, dem.shape[0])
    rows, cols = np.mgrid[r0:r1, c0:c1]
    cx, cy = cell_centres(meta, rows, cols)
    mask = points_in_polygon(cx, cy, poly[:, :2])
    if not mask.any():
        raise ValueError("polygon does not cover the scan")
    z = dem[r0:r1, c0:c1][mask]
    cx, cy = cx[mask], cy[mask]

    if reference == "plane" and len(poly) >= 3:
        a = np.column_stack([xs, ys, np.ones(len(xs))])
        coef, *_ = np.linalg.lstsq(a, poly[:, 2], rcond=None)
        ref = coef[0] * cx + coef[1] * cy + coef[2]
        ref_name = "best-fit plane through corners"
    else:
        ref = np.full(cx.shape, poly[:, 2].min())
        ref_name = f"level plane at {poly[:, 2].min():.2f} m"

    valid = ~np.isnan(z)
    diff = z[valid] - ref[valid]
    area_cell = meta["cell"] ** 2
    return {
        "above_m3": float(diff[diff > 0].sum() * area_cell),
        "below_m3": float(-diff[diff < 0].sum() * area_cell),
        "plan_area_m2": float(polygon_area(poly[:, :2])),
        "coverage": float(valid.mean()),
        "reference": ref_name,
    }


# --------------------------------------------------------------------------- comparison

def _rotz(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def _icp(src, tree, dst, rot, trans, iters, trim=0.8):
    """Trimmed ICP restricted to yaw + 3D translation (both clouds are gravity-aligned)."""
    for _ in range(iters):
        moved = src @ rot.T + trans
        dist, idx = tree.query(moved, workers=-1)
        keep = dist <= np.quantile(dist, trim)
        a, b = moved[keep], dst[idx[keep]]
        r2, t2 = fit_rigid_2d(a[:, :2], b[:, :2])
        step = np.eye(3)
        step[:2, :2] = r2
        rot = step @ rot
        trans = step @ trans + np.array([t2[0], t2[1], (b[:, 2] - a[:, 2]).mean()])
    moved = src @ rot.T + trans
    dist, _ = tree.query(moved, workers=-1)
    score = float(np.sqrt((np.sort(dist)[: int(len(dist) * trim)] ** 2).mean()))
    return rot, trans, score


def align_clouds(src, dst, initial_offset=None, sample=12000, seed=0):
    """Rigid transform (4x4) that maps src onto dst, plus trimmed RMS error."""
    rng = np.random.default_rng(seed)
    s = src[rng.choice(len(src), min(sample, len(src)), replace=False)]
    d = dst[rng.choice(len(dst), min(sample * 2, len(dst)), replace=False)]
    tree = cKDTree(d)

    starts = []
    if initial_offset is not None:
        starts.append((np.eye(3), np.asarray(initial_offset, float)))
    for yaw in range(0, 360, 20):
        rot = _rotz(yaw)
        starts.append((rot, d.mean(0) - s.mean(0) @ rot.T))

    best = min((_icp(s, tree, d, r, t, 12) for r, t in starts), key=lambda x: x[2])
    rot, trans, score = _icp(s, tree, d, best[0], best[1], 40)
    m = np.eye(4)
    m[:3, :3], m[:3, 3] = rot, trans
    return m, score


def dem_difference(xyz_a, xyz_b, cell, noise=0.02):
    """Elevation change b - a on a shared grid. Returns (diff, meta, stats)."""
    lo = np.maximum(xyz_a[:, :2].min(0), xyz_b[:, :2].min(0))
    hi = np.minimum(xyz_a[:, :2].max(0), xyz_b[:, :2].max(0))
    if np.any(hi <= lo):
        raise ValueError("the scans do not overlap after alignment")
    bounds = (lo[0], lo[1], hi[0], hi[1])
    dem_a, meta = build_dem(xyz_a, cell, bounds=bounds)
    dem_b, _ = build_dem(xyz_b, cell, bounds=bounds)
    diff = dem_b - dem_a
    diff[np.abs(diff) < noise] = 0.0
    cell_area = meta["cell"] ** 2
    valid = ~np.isnan(diff)
    stats = {
        "fill_m3": float(diff[valid & (diff > 0)].sum() * cell_area),
        "cut_m3": float(-diff[valid & (diff < 0)].sum() * cell_area),
        "overlap_m2": float(valid.sum() * cell_area),
        "noise_threshold_m": noise,
    }
    stats["net_m3"] = stats["fill_m3"] - stats["cut_m3"]
    return diff, meta, stats
