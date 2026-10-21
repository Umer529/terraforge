"""Generate two synthetic iPhone scan packages of the same site on different dates.

They mimic what the TerraForge iOS app writes: an ARKit-frame (y-up) PLY,
GPS fixes paired with camera positions, and a manifest, zipped together.
Each date has its own AR session origin and heading, like real captures.

Ground truth (useful to check the measurement tools):
  day 1: conical stockpile r = 3.0 m, h = 1.5 m  -> 14.14 m3
  day 2: pile reduced to h = 0.9 m              ->  8.48 m3
         plus a 3 x 2 x 0.8 m excavation pit     ->  4.80 m3 cut
  so day 1 -> day 2: cut ~ 5.65 + 4.80 = 10.45 m3

Usage:  python tools/make_sample_scans.py [out_dir]
"""
import csv
import io
import json
import math
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

SITE_LAT, SITE_LON = 31.48150, 74.30300
EARTH_R = 6378137.0
PILE = (6.0, 2.0, 3.0)  # centre east, centre north, radius
PIT = (-6.0, -3.0, 3.0, 2.0, 0.8)  # centre e, n, size e, size n, depth


def terrain(e, n, pile_h, pit):
    z = 0.015 * e + 0.01 * n + 0.08 * np.sin(e / 4.0) * np.cos(n / 5.0)
    r = np.hypot(e - PILE[0], n - PILE[1])
    z = z + np.clip(pile_h * (1 - r / PILE[2]), 0, None)
    if pit:
        pe, pn, se, sn, depth = PIT
        inside = (np.abs(e - pe) < se / 2) & (np.abs(n - pn) < sn / 2)
        z = np.where(inside, z - depth, z)
    return z


def colours(e, n, z, rng):
    base = np.where((np.hypot(e - PILE[0], n - PILE[1]) < PILE[2] + 0.3)[:, None],
                    [150, 112, 72], [126, 118, 88])
    grass = (np.sin(e * 1.3) * np.cos(n * 0.9) > 0.55)[:, None]
    base = np.where(grass, [88, 122, 64], base)
    return np.clip(base + rng.normal(0, 10, (len(e), 3)), 0, 255).astype(np.uint8)


def write_ply(xyz, rgb):
    header = (
        "ply\nformat binary_little_endian 1.0\ncomment TerraForge capture (ARKit world frame, y-up)\n"
        f"element vertex {len(xyz)}\nproperty float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
    ).encode()
    rec = np.empty(len(xyz), dtype=[("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("r", "u1"), ("g", "u1"), ("b", "u1")])
    rec["x"], rec["y"], rec["z"] = xyz.T
    rec["r"], rec["g"], rec["b"] = rgb.T
    return header + rec.tobytes()


def true_colour(e, n, pile_h, pit):
    """Ground-truth surface colour of the synthetic site (what a camera would see)."""
    e, n = np.asarray(e, float), np.asarray(n, float)
    patch = np.sin(e * 0.9 + 2 * np.sin(n * 0.7)) * np.cos(n * 1.1 - e * 0.3)
    mix = np.clip((patch - 0.1) * 2.5, 0, 1)[:, None]
    col = np.array([84, 118, 56]) * (1 - mix) + np.array([142, 114, 82]) * mix
    path = (np.abs(n + 6.0) < 0.8)[:, None]  # concrete path along the south edge
    col = np.where(path, [176, 173, 166], col)
    r = np.hypot(e - PILE[0], n - PILE[1])
    stripes = 0.85 + 0.15 * np.sin(np.arctan2(n - PILE[1], e - PILE[0]) * 14)
    col = np.where((r < PILE[2] * min(1.0, pile_h / 1.5 + 0.2))[:, None], np.array([160, 140, 110]) * stripes[:, None], col)
    if pit:
        pe, pn, se, sn, _ = PIT
        col = np.where(((np.abs(e - pe) < se / 2) & (np.abs(n - pn) < sn / 2))[:, None], [92, 70, 50], col)
    fine = 0.92 + 0.08 * np.sin(e * 23) * np.sin(n * 19)
    return np.clip(col * fine[:, None], 0, 255)


def write_mesh_ply(verts, faces):
    """Untextured LiDAR mesh as the app exports it (ARKit frame, y-up)."""
    header = (
        "ply\nformat binary_little_endian 1.0\ncomment TerraForge LiDAR mesh (ARKit world frame, y-up)\n"
        f"element vertex {len(verts)}\nproperty float x\nproperty float y\nproperty float z\n"
        f"element face {len(faces)}\nproperty list uchar int vertex_indices\nend_header\n"
    ).encode()
    frec = np.empty(len(faces), dtype=[("n", "u1"), ("i", "<i4", (3,))])
    frec["n"], frec["i"] = 3, faces
    return header + verts.astype("<f4").tobytes() + frec.tobytes()


def render_photo(pts, cols, cam_to_world, k, w, h):
    """Render what the phone camera sees (ARKit camera: x right, y up, looking down -z)."""
    inv = np.linalg.inv(cam_to_world)
    pc = pts @ inv[:3, :3].T + inv[:3, 3]
    d = -pc[:, 2]
    ok = d > 0.2
    u = k[0, 2] + k[0, 0] * pc[ok, 0] / d[ok]
    v = k[1, 2] - k[1, 1] * pc[ok, 1] / d[ok]
    c, d = cols[ok], d[ok]
    u, v = u.astype(int), v.astype(int)
    # splat size grows as points get closer, so the surface renders without holes
    size = np.clip(np.ceil(k[0, 0] * 0.03 / d), 2, 9).astype(int)
    pix, dep, col = [], [], []
    for du in range(9):
        for dv in range(9):
            m = (size > max(du, dv)) & (u + du < w) & (v + dv < h) & (u + du >= 0) & (v + dv >= 0)
            pix.append((v[m] + dv) * w + u[m] + du)
            dep.append(d[m])
            col.append(c[m])
    pix, dep, col = np.concatenate(pix), np.concatenate(dep), np.concatenate(col)
    order = np.argsort(-dep)  # far first; nearest splat is written last and wins (depth buffer)
    img = np.repeat(np.linspace([150, 185, 225], [205, 222, 240], h).astype(np.uint8)[:, None, :], w, axis=1).reshape(-1, 3)
    img[pix[order]] = col[order]
    img = img.reshape(h, w, 3)
    hit = np.zeros(h * w, bool)
    hit[pix] = True
    hit = hit.reshape(h, w)
    for _ in range(5):  # fill pinholes from hit neighbours; open sky stays sky
        pad_img = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge").astype(np.int32)
        pad_hit = np.pad(hit, 1)
        acc = np.zeros((h, w, 3), np.int32)
        cnt = np.zeros((h, w), np.int32)
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                nh = pad_hit[dy:dy + h, dx:dx + w]
                acc += pad_img[dy:dy + h, dx:dx + w] * nh[..., None]
                cnt += nh
        fill = ~hit & (cnt >= 4)
        img[fill] = (acc[fill] / cnt[fill, None]).astype(np.uint8)
        hit |= fill
    return img


def make_scan(day, pile_h, pit, start_angle, yaw_deg, when, seed, with_photos=False):
    """The operator walks an ellipse around the site, starting at start_angle (deg).

    with_photos=False: coloured point cloud (like a 3d Scanner App export).
    with_photos=True:  untextured LiDAR mesh + posed camera photos (like the TerraForge app).
    """
    rng = np.random.default_rng(seed)
    t = np.radians(start_angle) + np.linspace(0, 2 * math.pi, 90)
    pe, pn = 11 * np.cos(t), 6.5 * np.sin(t)
    start = (pe[0], pn[0], float(terrain(pe[:1], pn[:1], pile_h, pit)[0]) + 1.4)

    # world ENU -> AR session frame (origin at walk start, arbitrary heading)
    yaw = math.radians(yaw_deg)
    c, s = math.cos(yaw), math.sin(yaw)

    def to_session(pe, pn, pz):
        de, dn = pe - start[0], pn - start[1]
        se, sn = c * de + s * dn, -s * de + c * dn
        return np.column_stack([se, pz - start[2], -sn])  # ARKit: x=right, y=up, z=back

    def dir_to_session(v):
        return np.array([c * v[0] + s * v[1], v[2], -(-s * v[0] + c * v[1])])

    extra = {}
    if with_photos:
        ge, gn = np.meshgrid(np.arange(-14, 14.001, 0.05), np.arange(-9, 9.001, 0.05))
        gz = terrain(ge, gn, pile_h, pit) + rng.normal(0, 0.005, ge.shape)
        verts = to_session(ge.ravel(), gn.ravel(), gz.ravel())
        ny_, nx_ = ge.shape
        idx = np.arange(nx_ * ny_).reshape(ny_, nx_)
        a, b, cc, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
        faces = np.vstack([np.column_stack([a, b, cc]), np.column_stack([b, d, cc])])
        extra["mesh.ply"] = write_mesh_ply(verts, faces)

        # dense coloured surface used only to render the camera photos
        m = 2_500_000
        se, sn = rng.uniform(-14, 14, m), rng.uniform(-9, 9, m)
        surf = to_session(se, sn, terrain(se, sn, pile_h, pit))
        cols = true_colour(se, sn, pile_h, pit).astype(np.uint8)
        w, h = 640, 480
        k = np.array([[500.0, 0, w / 2], [0, 500.0, h / 2], [0, 0, 1]])
        for i, j in enumerate(range(1, len(pe))):
            print(f"\r  rendering camera photo {i + 1}/{len(pe) - 1}", end="", flush=True)
            pos = np.array([pe[j], pn[j], terrain(pe[j:j + 1], pn[j:j + 1], pile_h, pit)[0] + 1.4])
            # rotate through shots: towards the site centre, outwards to the edge, down at the path
            kind = i % 3
            if kind == 2:
                nxt = min(j + 1, len(pe) - 1)
                look = np.array([pe[nxt] - pe[j - 1], pn[nxt] - pn[j - 1], 0.0])
            else:
                look = np.array([-pos[0], -pos[1], 0.0]) * (1 if kind == 0 else -1)
            look /= np.linalg.norm(look)
            pitch = np.radians((30, 45, 75)[kind])
            fwd = np.cos(pitch) * look + np.sin(pitch) * np.array([0, 0, -1.0])
            right = np.cross(fwd, [0, 0, 1.0])
            right /= np.linalg.norm(right)
            up = np.cross(right, fwd)
            cam = np.eye(4)
            cam[:3, 0], cam[:3, 1], cam[:3, 2] = dir_to_session(right), dir_to_session(up), dir_to_session(-fwd)
            cam[:3, 3] = to_session(pos[:1], pos[1:2], pos[2:3])[0]
            half = k.copy()
            half[:2] /= 2  # render at half resolution, then upscale (4x faster)
            img = render_photo(surf, cols, cam, half, w // 2, h // 2)
            jpg = io.BytesIO()
            Image.fromarray(img).resize((w, h), Image.BILINEAR).save(jpg, "JPEG", quality=88)
            name = f"photos/img_{i + 1:04d}"
            extra[name + ".jpg"] = jpg.getvalue()
            extra[name + ".json"] = json.dumps({
                "transform_column_major": cam.T.ravel().tolist(), "intrinsics": k.T.ravel().tolist(),
                "width": w, "height": h, "timestamp": 2.0 * j})
        print()
    else:
        n_pts = 450_000
        e = rng.uniform(-14, 14, n_pts)
        n = rng.uniform(-9, 9, n_pts)
        z = terrain(e, n, pile_h, pit) + rng.normal(0, 0.008, n_pts)  # ~1 cm LiDAR noise
        rgb = colours(e, n, z, rng)
        # a few flying-pixel outliers, as real LiDAR has
        k = 1500
        out_idx = rng.choice(n_pts, k, replace=False)
        z[out_idx] += rng.uniform(0.3, 2.0, k)
        extra["mesh.ply"] = write_ply(to_session(e, n, z), rgb)

    # camera path with a GPS fix every ~1 m (phone GPS noise ~1.5 m)
    pz = terrain(pe, pn, pile_h, pit) + 1.4
    cam = to_session(pe, pn, pz)
    gps_e = pe + rng.normal(0, 1.5, len(pe))
    gps_n = pn + rng.normal(0, 1.5, len(pn))
    lat = SITE_LAT + np.degrees(gps_n / EARTH_R)
    lon = SITE_LON + np.degrees(gps_e / (EARTH_R * math.cos(math.radians(SITE_LAT))))

    fixes = io.StringIO()
    w = csv.writer(fixes)
    w.writerow(["timestamp", "latitude", "longitude", "altitude", "horizontal_accuracy", "cam_x", "cam_y", "cam_z"])
    for i in range(len(pe)):
        ts = (when + timedelta(seconds=2 * i)).isoformat()
        w.writerow([ts, f"{lat[i]:.8f}", f"{lon[i]:.8f}", f"{217 + rng.normal(0, 2):.2f}", "4.0",
                    f"{cam[i, 0]:.3f}", f"{cam[i, 1]:.3f}", f"{cam[i, 2]:.3f}"])

    path_len = float(np.hypot(np.diff(pe), np.diff(pn)).sum())
    manifest = {
        "format": "terraforge-session/1",
        "title": "Stockpile area - LiDAR + photos" if with_photos else f"Stockpile area - day {day}",
        "notes": "Synthetic capture generated by tools/make_sample_scans.py",
        "captured_at": when.isoformat(),
        "device_model": "iPhone 15 Pro (simulated)",
        "up_axis": "y",
        "scan_file": "mesh.ply",
        "path_length_m": round(path_len, 1),
        "gps_fixes": len(pe),
        "photos": sum(1 for name in extra if name.endswith(".jpg")),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("location_fixes.csv", fixes.getvalue())
        for name, data in extra.items():
            zf.writestr(name, data)
    return buf.getvalue()


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data")
    out.mkdir(parents=True, exist_ok=True)
    pkt = timezone(timedelta(hours=5))
    day1 = make_scan(1, 1.5, False, start_angle=200, yaw_deg=37, when=datetime(2026, 10, 14, 10, 0, tzinfo=pkt), seed=1)
    day2 = make_scan(2, 0.9, True, start_angle=30, yaw_deg=-112, when=datetime(2026, 10, 20, 15, 30, tzinfo=pkt), seed=2)
    fused = make_scan(3, 1.5, False, start_angle=120, yaw_deg=75, when=datetime(2026, 10, 19, 11, 0, tzinfo=pkt), seed=3,
                      with_photos=True)
    for name, data in (("scan_day1.zip", day1), ("scan_day2.zip", day2), ("scan_lidar_photos.zip", fused)):
        (out / name).write_bytes(data)
        print(f"wrote {out / name} ({len(data) / 1e6:.1f} MB)")
    print("truth: day1 pile 14.14 m3 | day2 pile 8.48 m3 + pit 4.80 m3 | change: ~10.45 m3 cut")
    print("scan_lidar_photos.zip: untextured LiDAR mesh + 89 posed photos (pile 14.14 m3)")


if __name__ == "__main__":
    main()
