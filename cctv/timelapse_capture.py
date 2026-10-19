#!/usr/bin/env python3
"""Timelapse Harian CCTV — PASS CAPTURE (dijalankan tiap 5 menit via GitHub Actions).

Untuk tiap kamera di timelapse_cameras.json: ambil 1 frame JPEG (480x270)
dari stream HLS via ffmpeg, simpan sebagai frames/<id>_<HHMM>.jpg (waktu WIB).

Frame dikumpulkan antar-run lewat artifact `tl-frames-YYYY-MM-DD`
(diunduh sebelum script jalan, diunggah ulang sesudahnya oleh workflow).
Kamera yang offline/gagal dilewati diam-diam (assembly menoleransi gap).
"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CCTV_JSON = os.path.join(BASE, "cctv.json")
CAMS_JSON = os.path.join(BASE, "timelapse_cameras.json")
FRAMES_DIR = os.path.join(os.getcwd(), "frames")
WIB = timezone(timedelta(hours=7))

TIMEOUT_S = 25
WIDTH, HEIGHT = 480, 270


def resolve_streams():
    """Petakan (wilayah, nama) -> stream URL dari cctv.json."""
    d = json.load(open(CCTV_JSON, encoding="utf-8"))
    m = {}
    for w in d.get("wilayah", []):
        wn = w.get("nama", "")
        for k in w.get("kamera", []):
            key = (wn, k.get("nama", ""))
            if key not in m:
                m[key] = k.get("stream", "")
    return m


def capture_one(cam, stream, stamp):
    cid = cam["id"]
    out = os.path.join(FRAMES_DIR, f"{cid}_{stamp}.jpg")
    if os.path.exists(out):
        return (cid, "skip-sudah-ada")
    if not stream:
        return (cid, "skip-tanpa-stream")
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", stream,
        "-vframes", "1",
        "-s", f"{WIDTH}x{HEIGHT}",
        "-q:v", "5",
        out,
    ]
    try:
        r = subprocess.run(cmd, timeout=TIMEOUT_S,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return (cid, "ok")
        if os.path.exists(out):
            os.remove(out)
        return (cid, "gagal-ffmpeg")
    except subprocess.TimeoutExpired:
        return (cid, "gagal-timeout")
    except Exception as e:
        return (cid, f"gagal-{e}")


def main():
    os.makedirs(FRAMES_DIR, exist_ok=True)
    streams = resolve_streams()
    cams = json.load(open(CAMS_JSON, encoding="utf-8"))["kamera"]
    stamp = datetime.now(WIB).strftime("%H%M")

    jobs = []
    for cam in cams:
        s = streams.get((cam["wilayah"], cam["nama"]), "")
        if not s:
            print(f"  ! {cam['id']}: tidak ketemu di cctv.json "
                  f"(wilayah/nama berubah?)", flush=True)
        jobs.append((cam, s))

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(capture_one, cam, s, stamp): cam["id"]
                for cam, s in jobs}
        for f in as_completed(futs):
            cid, status = f.result()
            if status == "ok":
                ok += 1
            elif status != "skip-sudah-ada":
                fail += 1
                print(f"  ! {cid}: {status}", flush=True)
    print(f"CAPTURE {stamp} WIB: {ok} ok, {fail} gagal", flush=True)


if __name__ == "__main__":
    main()
