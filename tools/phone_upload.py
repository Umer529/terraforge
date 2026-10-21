"""Upload a scan package exactly like the TerraForge iOS app does.

Logs in, creates an upload session, sends SHA-256-checked chunks and resumes
from the last confirmed chunk after a failure. The upload id is kept in
`<package>.upload.json`, which plays the role of the app's local record.

Examples
  python tools/phone_upload.py sample_data/scan_day1.zip
  python tools/phone_upload.py sample_data/scan_day2.zip --drop-after 4    # simulate signal loss
  python tools/phone_upload.py sample_data/scan_day2.zip                   # run again -> resumes
  python tools/phone_upload.py my_scan.ply --lat 31.4815 --lon 74.3030     # bare 3d Scanner App export
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def bar(done, total, width=30):
    filled = int(width * done / total)
    return f"[{'#' * filled}{'.' * (width - filled)}] {done}/{total}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", type=Path)
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--user", default="operator")
    ap.add_argument("--password", default="terraforge")
    ap.add_argument("--site", type=int, help="site id (default: first site)")
    ap.add_argument("--title")
    ap.add_argument("--lat", type=float, help="GPS latitude for a bare export")
    ap.add_argument("--lon", type=float, help="GPS longitude for a bare export")
    ap.add_argument("--up-axis", choices=["y", "z"], help="override up axis for a bare export")
    ap.add_argument("--chunk-kb", type=int, default=1024)
    ap.add_argument("--drop-after", type=int, help="simulate losing the connection after N chunks")
    args = ap.parse_args()

    pkg = args.package
    if not pkg.exists():
        sys.exit(f"no such file: {pkg}")
    state_file = pkg.with_name(pkg.name + ".upload.json")
    s = requests.Session()

    print(f"TerraForge phone uploader -> {args.server}")
    r = s.post(f"{args.server}/api/auth/login", json={"username": args.user, "password": args.password})
    r.raise_for_status()
    s.headers["Authorization"] = f"Token {r.json()['token']}"
    print(f"  logged in as {args.user} ({r.json()['role']})")

    state = json.loads(state_file.read_text()) if state_file.exists() else None
    if state:
        r = s.get(f"{args.server}/api/uploads/{state['upload_id']}")
        if r.ok and not r.json()["complete"]:
            info = r.json()
            print(f"  resuming upload {info['upload_id'][:8]}: server has {len(info['received'])}/{info['total_chunks']} chunks")
        else:
            state = None
    if not state:
        sites = s.get(f"{args.server}/api/sites").json()["sites"]
        site_id = args.site or sites[0]["id"]
        size = pkg.stat().st_size
        body = {
            "site_id": site_id, "filename": pkg.name, "size": size, "chunk_size": args.chunk_kb * 1024,
            "sha256": sha256_file(pkg), "title": args.title, "device_model": "TerraForge uploader (simulated iPhone)",
            "captured_at": datetime.now(timezone.utc).isoformat(), "latitude": args.lat, "longitude": args.lon,
            "up_axis": args.up_axis,
        }
        r = s.post(f"{args.server}/api/uploads", json=body)
        if not r.ok:
            sys.exit(f"  server refused upload: {r.text}")
        state = r.json()
        state_file.write_text(json.dumps(state))
        print(f"  created upload {state['upload_id'][:8]} for scan #{state['scan_id']}: "
              f"{size / 1e6:.1f} MB in {state['total_chunks']} chunks of {state['chunk_size'] // 1024} KB")

    info = s.get(f"{args.server}/api/uploads/{state['upload_id']}").json()
    total, chunk_size, received = info["total_chunks"], info["chunk_size"], set(info["received"])
    sent_now = 0
    with open(pkg, "rb") as fh:
        for i in range(total):
            if i in received:
                continue
            if args.drop_after is not None and sent_now >= args.drop_after:
                print(f"\n  !! connection lost after chunk {i} (simulated). Saved progress; run again to resume.")
                return
            fh.seek(i * chunk_size)
            data = fh.read(chunk_size)
            for attempt in range(3):
                r = s.put(f"{args.server}/api/uploads/{state['upload_id']}/chunks/{i}", data=data,
                          headers={"X-Chunk-SHA256": hashlib.sha256(data).hexdigest(), "Content-Type": "application/octet-stream"})
                if r.ok:
                    break
                print(f"\n  chunk {i} rejected ({r.status_code}), retrying")
                time.sleep(0.5)
            else:
                sys.exit(f"  chunk {i} failed: {r.text}")
            received.add(i)
            sent_now += 1
            print(f"\r  uploading {bar(len(received), total)}", end="", flush=True)
    print()

    r = s.post(f"{args.server}/api/uploads/{state['upload_id']}/complete")
    if not r.ok:
        sys.exit(f"  completion failed: {r.text}")
    state_file.unlink(missing_ok=True)
    scan_id = r.json()["scan_id"]
    print(f"  package verified (sha256 {r.json()['sha256'][:12]}...), scan #{scan_id} queued for processing")

    last = None
    while True:
        scan = s.get(f"{args.server}/api/scans/{scan_id}").json()
        if scan["status"] != last:
            print(f"  status: {scan['status']}")
            last = scan["status"]
        if scan["status"] in ("ready", "failed"):
            break
        time.sleep(1)
    print(f"  open {args.server}/scans/{scan_id}/ in the dashboard")


if __name__ == "__main__":
    main()
