# TerraForge — FYP demo

Working prototype of the full TerraForge flow from the FYP report:

**capture → chunked resumable upload → queued processing → 3D viewer + map → measure → compare**

| Report subsystem | Demo implementation |
|---|---|
| Mobile app (Swift, ARKit, RealityKit) | Source in [`ios/TerraForge`](ios/TerraForge) (needs Mac + LiDAR iPhone). Until then, scans come from `tools/phone_upload.py` (same protocol) or from 3d Scanner App / Polycam exports |
| Cloud backend (Django REST) | `core/api.py` — token login, sites, scans, chunked upload with SHA-256 checks and resume, measurements, downloads, role/site access checks |
| Job queue (Celery planned) | Jobs stored in the database, `manage.py run_worker` claims them in a separate process — same states (queued → processing → ready/failed) |
| Processing worker | `core/pipeline.py` — unpack → load → clean (voxel + outlier filter) → georeference (camera path fitted to GPS) → elevation grid → **GeoTIFF** (GDAL via rasterio) → viewer cloud |
| Web dashboard (Three.js, Leaflet) | `templates/`, `static/js/` — layout of the dashboard wireframe: scan list, 3D viewer, map with GeoTIFF overlay, measurement tools, scan details |
| Database (PostgreSQL planned) | SQLite for the demo, same tables as the data dictionary (`core/models.py`) |

Not built yet (FYP-II): photogrammetry fusion (Meshroom/AliceVision), GLB model export, PostgreSQL + Celery deployment, accuracy evaluation against survey data.

## Setup (once)

```
setup_demo.bat
```
Creates `.venv`, installs packages, creates the database, demo users and two sample scans in `sample_data/`.

Accounts (password `terraforge`): **admin** (administrator), **operator** (field operator), **reviewer** (project reviewer).

## Run

```
start_demo.bat
```
Opens two windows (server + worker) and the browser at http://127.0.0.1:8000. Keep both windows open.

## Demo script (≈ 8 minutes)

1. **Architecture** (1 min) — show the report's architecture diagram, then the two console windows: REST server and worker are separate processes, as designed.
2. **Upload like the phone, with a signal drop** (2 min) — in a third terminal:
   ```
   .venv\Scripts\python tools\phone_upload.py sample_data\scan_day1.zip --drop-after 3
   .venv\Scripts\python tools\phone_upload.py sample_data\scan_day1.zip
   ```
   The first run stops after 3 chunks; the second asks the server what it has and **resumes from chunk 4**. Watch the worker window process the job.
3. **Dashboard** (2 min) — log in as `reviewer`. Show the scan list, the 3D point cloud (rotate/zoom), the elevation map on the map, the GPS track, scan details and the processing log, and the GeoTIFF download (open it in QGIS if available).
4. **Measure** (2 min) — Distance: click two points. Volume: click around the base of the stockpile, then Finish. The synthetic pile is **14.14 m³**; the tool gives ≈ 14.5 m³ ± 0.8. Measurements are saved with the scan.
5. **Compare** (1 min) — upload `scan_day2.zip`, open Compare, choose day 1 → day 2. The two captures have different start points and headings; ICP aligns them and the cut is **≈ 10.7 m³ against a true 10.45 m³**.
6. **Roles** — log in as `operator` (can upload) vs `reviewer` (no Upload menu); `admin` gets *Users & sites* (Django admin).
7. **iOS code** — open `ios/TerraForge/CaptureController.swift` and `APIClient.swift` and walk through the capture and upload code.

Use `reset_demo.bat` beforehand if you want to upload everything live.

## Using a real iPhone scan (borrowed LiDAR iPhone, no Mac needed)

1. On the iPhone install **3d Scanner App** (free) or **Polycam**. Scan in LiDAR mode — walk slowly around a soil heap, a ramp or a pit, 1–2 m away.
2. Export: 3d Scanner App → *Share → Point Cloud → PLY* (or *LAS*); Polycam → *Export → Point cloud → PLY/LAS*. AirDrop/WhatsApp/Drive the file to the laptop.
3. Dashboard → **Upload** (as `operator`) → choose the file → *Use site centre* or type the latitude/longitude of where you scanned (Google Maps long-press gives it) → Upload.
4. Up axis: leave *Auto* (PLY/OBJ = y-up from ARKit, LAS = z-up). If the scan appears standing on its side, re-upload with the other option.
5. Scan the same heap twice (e.g. before and after moving some soil) to demo **Compare** on real data.

Supported: `.ply .las .laz .obj .xyz` and TerraForge `.zip` packages.

## Accuracy note for evaluators

Error figures (±) use an assumed LiDAR vertical error of 2 cm; replacing that with measured values from surveyed reference objects is the planned evaluation (report Ch. 2 and the testing plan).
