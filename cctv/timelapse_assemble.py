#!/usr/bin/env python3
"""Timelapse Harian CCTV — PASS ASSEMBLY (dijalankan 1x sehari via GitHub Actions).

1. Untuk tiap kamera: rakit frames/<id>_*.jpg (urutan nama = urutan waktu)
   jadi MP4 timelapse 12fps (~24 detik untuk 288 frame) via ffmpeg.
2. Unggah MP4 ke GitHub Release rolling tag `cctv-timelapse` (--clobber).
3. Hapus aset rilis yang lebih tua dari RETENSI_HARI.
4. Tulis ulang cctv/timelapse_index.json (dilihat aplikasi Hozin Tools).

Butuh: ffmpeg, gh CLI, env GH_TOKEN. Tanggal = WIB kemarin (arg --date).
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CAMS_JSON = os.path.join(BASE, "timelapse_cameras.json")
INDEX_JSON = os.path.join(BASE, "timelapse_index.json")

TAG = "cctv-timelapse"
REPO = "hozin-king/hozin-news-getter"
RETENSI_HARI = 7
FPS = 12
MIN_FRAMES = 10


def sh(cmd, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, **kw)


def rakit_mp4(cid, frame_files, out_path):
    # daftar file via concat demuxer (path absolut: ffmpeg me-resolve path
    # relatif terhadap lokasi file list, bukan cwd)
    lst = out_path + ".txt"
    with open(lst, "w") as f:
        for p in frame_files:
            f.write(f"file '{os.path.abspath(p)}'\n")
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "concat", "-safe", "0", "-r", str(FPS), "-i", lst,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
           "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, timeout=300)
    os.remove(lst)
    return r.returncode == 0 and os.path.exists(out_path)


def pastikan_rilis():
    r = sh(["gh", "release", "view", TAG, "--repo", REPO])
    if r.returncode != 0:
        r = sh(["gh", "release", "create", TAG,
                "--repo", REPO,
                "--title", "CCTV Timelapse Harian",
                "--notes", "Rekaman timelapse harian CCTV publik Indonesia "
                           "(1 frame / 5 menit, dirakit 12 fps). "
                           "Rolling 7 hari terakhir."])
        if r.returncode != 0:
            print("gagal buat rilis: " + r.stderr, file=sys.stderr)
            sys.exit(1)
        print("rilis cctv-timelapse dibuat", flush=True)


def daftar_aset():
    r = sh(["gh", "release", "view", TAG, "--repo", REPO, "--json", "assets",
            "--jq", ".assets[].name"])
    return [x for x in r.stdout.splitlines() if x.strip()] \
        if r.returncode == 0 else []


def tanggal_dari_nama(nama):
    m = re.search(r"_(\d{4}-\d{2}-\d{2})\.mp4$", nama)
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", required=True)
    ap.add_argument("--date", required=True,
                    help="tanggal WIB yang dirakit, format YYYY-MM-DD")
    a = ap.parse_args()

    cams = {c["id"]: c for c in
            json.load(open(CAMS_JSON, encoding="utf-8"))["kamera"]}
    out_dir = os.path.join(a.frames_dir, "out")
    os.makedirs(out_dir, exist_ok=True)

    dibuat = []  # (cid, path, n_frames)
    for cid in cams:
        files = sorted(glob.glob(os.path.join(a.frames_dir, f"{cid}_*.jpg")))
        if len(files) < MIN_FRAMES:
            print(f"  - {cid}: hanya {len(files)} frame, dilewati",
                  flush=True)
            continue
        out = os.path.join(out_dir, f"{cid}_{a.date}.mp4")
        if rakit_mp4(cid, files, out):
            dibuat.append((cid, out, len(files)))
            print(f"  + {cid}: {len(files)} frame -> {out}", flush=True)
        else:
            print(f"  ! {cid}: ffmpeg gagal", flush=True)

    if not dibuat:
        print("tidak ada video dibuat; index tidak diubah", flush=True)
        return

    pastikan_rilis()
    for cid, path, _ in dibuat:
        r = sh(["gh", "release", "upload", TAG, path, "--repo", REPO,
                "--clobber"])
        print(("  ^ " if r.returncode == 0 else "  ! upload gagal ") + path,
              flush=True)

    # prune aset > retensi
    batas = (datetime.strptime(a.date, "%Y-%m-%d").date()
             - timedelta(days=RETENSI_HARI - 1))
    for aset in daftar_aset():
        t = tanggal_dari_nama(aset)
        if t and t < batas.isoformat():
            sh(["gh", "release", "delete-asset", TAG, aset,
                "--repo", REPO, "-y"])
            print(f"  x hapus aset lama {aset}", flush=True)

    # tulis ulang index (hanya entri yang file-nya masih ada di rilis)
    aset_set = set(daftar_aset())
    videos = []
    for aset in sorted(aset_set):
        t = tanggal_dari_nama(aset)
        if not t:
            continue
        cid = aset[:-(len(t) + 5)]  # buang _YYYY-MM-DD.mp4
        cam = cams.get(cid)
        if not cam:
            continue
        videos.append({
            "id": cid,
            "nama": cam["nama"],
            "wilayah": cam["wilayah"],
            "kota": cam.get("kota", ""),
            "tanggal": t,
            "file": aset,
            "url": f"https://github.com/{REPO}/releases/download/{TAG}/{aset}",
        })
    videos.sort(key=lambda v: (v["tanggal"], v["id"]), reverse=True)
    index = {
        "updated_at": datetime.now().isoformat(timespec="seconds") + "Z",
        "retensi_hari": RETENSI_HARI,
        "videos": videos,
    }
    with open(INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print(f"index ditulis: {len(videos)} video -> {INDEX_JSON}", flush=True)


if __name__ == "__main__":
    main()
