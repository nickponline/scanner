#!/usr/bin/env python3
"""Upload a capture package exactly like the iOS app does (CONTRACTS.md §3): create scan → resumable PUTs → complete.

usage: upload_capture.py CAPTURE_DIR --api https://... --token TOKEN [--chunk-mb 32] [--no-complete]
Resumes interrupted uploads by querying the committed offset (PUT with Content-Range: bytes */SIZE).
"""
import argparse
import json
import sys
from pathlib import Path

import requests


def committed(url, size):
    r = requests.put(url, headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"}, timeout=60)
    if r.status_code in (200, 201):
        return size
    if r.status_code == 308:
        rng = r.headers.get("Range")
        return int(rng.split("-")[1]) + 1 if rng else 0
    raise RuntimeError(f"offset query failed {r.status_code}: {r.text[:200]}")


def upload(url, path: Path, chunk):
    size = path.stat().st_size
    off = committed(url, size) if size else 0
    with open(path, "rb") as f:
        if size == 0:
            r = requests.put(url, data=b"", timeout=60)
            r.raise_for_status()
            return
        while off < size:
            f.seek(off)
            data = f.read(chunk)
            end = off + len(data) - 1
            r = requests.put(url, data=data, headers={"Content-Range": f"bytes {off}-{end}/{size}"}, timeout=600)
            if r.status_code in (200, 201):
                return
            if r.status_code != 308:
                raise RuntimeError(f"{path}: upload failed {r.status_code} {r.text[:200]}")
            rng = r.headers.get("Range")
            off = int(rng.split("-")[1]) + 1 if rng else 0
            print(f"  {path.name}: {off / size:.0%}", end="\r", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", type=Path)
    ap.add_argument("--api", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--chunk-mb", type=int, default=32)
    ap.add_argument("--no-complete", action="store_true")
    a = ap.parse_args()
    H = {"Authorization": f"Bearer {a.token}"}
    manifest = json.loads((a.capture / "manifest.json").read_text())
    r = requests.post(f"{a.api}/v1/scans", json={"manifest": manifest}, headers=H, timeout=120)
    r.raise_for_status()
    res = r.json()
    scan = res["scan_id"]
    print("scan", scan)
    for u in res["uploads"]:
        p = a.capture / u["path"]
        print(f"uploading {u['path']} ({p.stat().st_size / 1e6:.1f} MB)")
        upload(u["url"], p, a.chunk_mb * 1024 * 1024 // (256 * 1024) * (256 * 1024))
    if a.no_complete:
        return
    r = requests.post(f"{a.api}/v1/scans/{scan}/complete", headers=H, timeout=120)
    print("complete:", r.status_code, r.text)
    r.raise_for_status()
    print(scan)


if __name__ == "__main__":
    sys.exit(main())
