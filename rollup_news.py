#!/usr/bin/env python3
"""Rollup berita: data/articles.jsonl -> data/latest.json + arsip harian.

Prinsip data (biar repo tidak bengkak), pola sama dengan Pinterest:
  - data/latest.json : MAKS 100 artikel terbaru (berdasarkan fetched_at,
    terbaru dulu).
  - data/archive/<TAHUN>/<BULAN>/<TANGGAL>.jsonl : artikel per tanggal
    fetched_at, 1 JSON per baris, MAKS 500 baris/hari (yang terbaru
    yang dipertahankan). Dibangun ulang dari articles.jsonl tiap run,
    jadi isinya selalu konsisten dengan sumber.
  - data/archive/index.json : jumlah artikel per hari / minggu ISO /
    bulan / tahun, dibuat ulang dari file arsip yang ada.

Tidak mengubah perilaku getter1/getter2 — skrip ini hanya membaca
articles.jsonl. Dijalankan oleh workflow news-rollup.yml.
Hanya stdlib — tanpa dependensi.
"""
import json
import os
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
ARTICLES_FILE = os.path.join(DATA, "articles.jsonl")
LATEST_FILE = os.path.join(DATA, "latest.json")
ARCHIVE_DIR = os.path.join(DATA, "archive")
MAX_LATEST = 100
MAX_ARCHIVE_PER_DAY = 500


def load_articles():
    """Baca semua artikel dari articles.jsonl (baris rusak dilewati)."""
    items = []
    if not os.path.exists(ARTICLES_FILE):
        print("articles.jsonl tidak ada — dianggap 0 artikel")
        return items
    with open(ARTICLES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def article_day(a):
    """Tanggal (YYYY-MM-DD) dari fetched_at; '' bila tidak valid."""
    ts = str(a.get("fetched_at") or "")
    day = ts[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
        return day
    except ValueError:
        return ""


def main():
    articles = load_articles()
    # Urut terbaru dulu berdasarkan fetched_at (format ISO -> urut leksikal).
    articles.sort(key=lambda a: str(a.get("fetched_at") or ""), reverse=True)

    now = datetime.now(WIB).isoformat(timespec="seconds")

    # ---- latest: 100 terbaru ----
    latest = articles[:MAX_LATEST]
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now, "count": len(latest),
                   "articles": latest}, f, ensure_ascii=False, indent=1)

    # ---- arsip per hari (bangun ulang dari sumber) ----
    by_day = {}
    for a in articles:
        day = article_day(a)
        if day:
            by_day.setdefault(day, []).append(a)
    for day, items in sorted(by_day.items()):
        d = datetime.strptime(day, "%Y-%m-%d").date()
        path = os.path.join(ARCHIVE_DIR, f"{d.year:04d}", f"{d.month:02d}",
                            f"{day}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # items sudah urut terbaru dulu; pertahankan 500 terbaru hari itu.
        with open(path, "w", encoding="utf-8") as f:
            for a in items[:MAX_ARCHIVE_PER_DAY]:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")

    # ---- index: hitung ulang dari file arsip ----
    per_hari = {}
    if os.path.isdir(ARCHIVE_DIR):
        for tahun in sorted(os.listdir(ARCHIVE_DIR)):
            ydir = os.path.join(ARCHIVE_DIR, tahun)
            if not os.path.isdir(ydir):
                continue
            for bulan in sorted(os.listdir(ydir)):
                mdir = os.path.join(ydir, bulan)
                if not os.path.isdir(mdir):
                    continue
                for nama in sorted(os.listdir(mdir)):
                    if nama.endswith(".jsonl"):
                        with open(os.path.join(mdir, nama),
                                  encoding="utf-8") as fh:
                            per_hari[nama[:-6]] = sum(1 for _ in fh)
    per_minggu, per_bulan, per_tahun = {}, {}, {}
    for tgl, n in sorted(per_hari.items()):
        try:
            d = datetime.strptime(tgl, "%Y-%m-%d").date()
        except ValueError:
            continue
        iso = d.isocalendar()
        wk = f"{iso[0]:04d}-W{iso[1]:02d}"
        per_minggu[wk] = per_minggu.get(wk, 0) + n
        per_bulan[tgl[:7]] = per_bulan.get(tgl[:7], 0) + n
        per_tahun[tgl[:4]] = per_tahun.get(tgl[:4], 0) + n
    index = {
        "updated_at": now,
        "total": sum(per_hari.values()),
        "per_hari": dict(sorted(per_hari.items())),
        "per_minggu": dict(sorted(per_minggu.items())),
        "per_bulan": dict(sorted(per_bulan.items())),
        "per_tahun": dict(sorted(per_tahun.items())),
    }
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    with open(os.path.join(ARCHIVE_DIR, "index.json"), "w",
              encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)

    print(f"SELESAI: {len(articles)} artikel dibaca; latest={len(latest)}; "
          f"arsip {len(by_day)} hari, total terarsip={index['total']}")


if __name__ == "__main__":
    main()
