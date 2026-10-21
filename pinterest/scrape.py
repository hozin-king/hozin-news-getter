#!/usr/bin/env python3
"""Scraper Pinterest publik -> pins_latest.json + arsip harian.

Metode (terverifikasi 21 Okt 2026, tanpa login):
  1. GET halaman pencarian /search/pins/?q=...&rs=typed pakai UA browser
     desktop untuk mengambil cookie sesi (termasuk csrftoken).
  2. GET /resource/BaseSearchResource/get/ dengan header
     X-Requested-With: XMLHttpRequest, X-CSRFToken dari cookie langkah 1,
     dan X-Pinterest-PWS-Handler: www/search/[id].js (WAJIB — tanpa header
     ini API membalas 403 "Invalid Resource Request"). Opsi pencarian juga
     wajib menyertakan "redux_normalize_feed": true.
     Respon JSON: resource_response.data.results; pin asli bertipe "pin"
     (hasil lain seperti "story" dilewati). Pagination lewat
     resource_response.bookmark (maks 3 halaman/topik).

Prinsip data (biar repo tidak bengkak):
  - pins_latest.json : MAKS 100 pin terbaru lintas topik, terbaru dulu;
    hasil baru masuk di depan, yang paling lama dibuang. Tiap item
    membawa field "topic" + "fetched_at".
  - seen.json        : id pin yang sudah pernah diambil (dedup),
    dibatasi 5000 terakhir.
  - archive/<TAHUN>/<BULAN>/<TANGGAL>.jsonl : pin BARU yang ditemukan
    hari itu, 1 JSON per baris, MAKS 500 baris/hari.
  - archive/index.json : jumlah pin per hari / minggu ISO / bulan /
    tahun, dibuat ulang dari file arsip yang ada (tanpa isi pin).

Topik dari topics.json. Maks 20 pin/topik/run, jeda sopan antar request.
Satu topik gagal -> dicatat, topik lain tetap jalan.
Hanya stdlib (urllib + http.cookiejar) — tanpa dependensi.
"""
import http.cookiejar
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
WIB = timezone(timedelta(hours=7))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOPICS_PATH = os.path.join(BASE_DIR, "topics.json")
LATEST_PATH = os.path.join(BASE_DIR, "pins_latest.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen.json")
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
MAX_PINS_PER_TOPIC = 20
MAX_PAGES = 3
MAX_LATEST = 100
MAX_SEEN = 5000
MAX_ARCHIVE_PER_DAY = 500
DELAY = 2.5  # jeda sopan antar request (detik)

# Fallback bila sertifikat kena intercept proxy (pola sama dgn cctv/scrape.py).
CTX_UNVERIFIED = ssl.create_default_context()
CTX_UNVERIFIED.check_hostname = False
CTX_UNVERIFIED.verify_mode = ssl.CERT_NONE


def make_opener(unverified=False):
    jar = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if unverified:
        handlers.append(urllib.request.HTTPSHandler(context=CTX_UNVERIFIED))
    return urllib.request.build_opener(*handlers), jar


def csrf_from(jar):
    for c in jar:
        if c.name == "csrftoken":
            return c.value
    return ""


def fetch_search(opener, jar, query, bookmark=None):
    """Satu halaman hasil pencarian. Return (results, bookmark_baru)."""
    search_path = "/search/pins/?q=" + urllib.parse.quote(query) + "&rs=typed"
    search_url = "https://www.pinterest.com" + search_path
    # Langkah 1: ambil cookie sesi dari halaman pencarian.
    req = urllib.request.Request(search_url, headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as r:
        r.read()
    time.sleep(DELAY)
    # Langkah 2: panggil BaseSearchResource.
    options = {"query": query, "scope": "pins", "rs": "typed",
               "redux_normalize_feed": True}
    if bookmark:
        options["bookmarks"] = [bookmark]
    data = {"options": options, "context": {}}
    api_url = ("https://www.pinterest.com/resource/BaseSearchResource/get/?"
               + urllib.parse.urlencode({
                   "source_url": search_path,
                   "data": json.dumps(data),
                   "_": str(int(time.time() * 1000)),
               }))
    req = urllib.request.Request(api_url, headers={
        "User-Agent": UA,
        "Referer": search_url,
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRFToken": csrf_from(jar),
        "X-Pinterest-PWS-Handler": "www/search/[id].js",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    with opener.open(req, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8", errors="ignore"))
    rr = payload.get("resource_response") or {}
    results = (rr.get("data") or {}).get("results") or []
    if not results and rr.get("status") not in (None, "success"):
        raise IOError(f"BaseSearchResource status={rr.get('status')}")
    return results, rr.get("bookmark") or ""


def pin_record(p, topic, fetched_at):
    """Ubah objek pin mentah Pinterest jadi record ringkas; None bila tak layak."""
    images = p.get("images") or {}
    img = images.get("orig") or {}
    if not img.get("url"):
        for size in ("736x", "474x", "236x"):
            if (images.get(size) or {}).get("url"):
                img = images[size]
                break
    if not img.get("url"):
        return None
    pid = str(p.get("id") or "")
    if not pid:
        return None
    title = p.get("title") or p.get("grid_title") or ""
    if not isinstance(title, str):
        title = ""
    desc = p.get("description") or ""
    if not isinstance(desc, str):
        desc = ""
    return {
        "id": pid,
        "topic": topic,
        "title": title.strip(),
        "description": desc.strip(),
        "image_url": img["url"],
        "width": img.get("width"),
        "height": img.get("height"),
        "pin_url": f"https://www.pinterest.com/pin/{pid}/",
        "board": (p.get("board") or {}).get("name") or "",
        "pinner": (p.get("pinner") or {}).get("username") or "",
        "fetched_at": fetched_at,
    }


def scrape_topic(opener, jar, query, fetched_at):
    """Ambil sampai MAX_PINS_PER_TOPIC pin untuk satu topik (dedup per id)."""
    pins, seen_ids, bookmark = [], set(), None
    for _ in range(MAX_PAGES):
        if len(pins) >= MAX_PINS_PER_TOPIC:
            break
        results, bookmark = fetch_search(opener, jar, query, bookmark)
        for r in results:
            if r.get("type") != "pin":
                continue
            rec = pin_record(r, query, fetched_at)
            if rec and rec["id"] not in seen_ids:
                seen_ids.add(rec["id"])
                pins.append(rec)
                if len(pins) >= MAX_PINS_PER_TOPIC:
                    break
        if not bookmark:
            break
        time.sleep(DELAY)
    return pins[:MAX_PINS_PER_TOPIC]


# ---------------- penyimpanan: seen / latest / arsip ----------------

def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, encoding="utf-8") as f:
                return list(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_seen(seen_list):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen_list[-MAX_SEEN:], f)


def load_latest():
    if os.path.exists(LATEST_PATH):
        try:
            with open(LATEST_PATH, encoding="utf-8") as f:
                return list(json.load(f).get("pins") or [])
        except (json.JSONDecodeError, OSError):
            pass
    return []


def archive_path_for(day):
    return os.path.join(ARCHIVE_DIR, f"{day.year:04d}", f"{day.month:02d}",
                        f"{day.isoformat()}.jsonl")


def append_archive(new_pins, day):
    """Tulis pin baru ke arsip hari itu; hormati batas MAX_ARCHIVE_PER_DAY.
    Return jumlah baris yang benar-benar tertulis."""
    if not new_pins:
        return 0
    path = archive_path_for(day)
    existing = 0
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = sum(1 for _ in f)
    room = MAX_ARCHIVE_PER_DAY - existing
    if room <= 0:
        print(f"  arsip {day.isoformat()} sudah penuh "
              f"({MAX_ARCHIVE_PER_DAY} baris); pin baru tidak diarsipkan")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in new_pins[:room]:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return min(len(new_pins), room)


def rebuild_index():
    """Hitung jumlah pin per hari/minggu/bulan/tahun dari file arsip."""
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
                    if not nama.endswith(".jsonl"):
                        continue
                    with open(os.path.join(mdir, nama),
                              encoding="utf-8") as f:
                        per_hari[nama[:-6]] = sum(1 for _ in f)
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
        "updated_at": datetime.now(WIB).isoformat(timespec="seconds"),
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
    return index


def main():
    with open(TOPICS_PATH, encoding="utf-8") as f:
        topics = json.load(f)
    now = datetime.now(WIB)
    fetched_at = now.isoformat(timespec="seconds")
    seen_list = load_seen()
    seen_set = set(seen_list)
    opener, jar = make_opener()
    new_pins, gagal, per_topik = [], [], {}
    for query in topics:
        print(f"Topik: {query} ...", flush=True)
        try:
            pins = scrape_topic(opener, jar, query, fetched_at)
        except ssl.SSLError:
            print("  ! SSL error, coba ulang tanpa verifikasi sertifikat",
                  file=sys.stderr)
            opener, jar = make_opener(unverified=True)
            try:
                pins = scrape_topic(opener, jar, query, fetched_at)
            except Exception as e:
                print(f"  ! GAGAL: {e}", file=sys.stderr)
                gagal.append(query)
                per_topik[query] = 0
                continue
        except Exception as e:
            print(f"  ! GAGAL: {e}", file=sys.stderr)
            gagal.append(query)
            per_topik[query] = 0
            continue
        fresh = [p for p in pins if p["id"] not in seen_set]
        for p in fresh:
            seen_set.add(p["id"])
            seen_list.append(p["id"])
        new_pins.extend(fresh)
        per_topik[query] = len(fresh)
        print(f"  pin baru: {len(fresh)} (dari {len(pins)} hasil)")
        time.sleep(DELAY)

    # latest rolling: pin baru di depan, lama digeser keluar sampai 100.
    old_latest = load_latest()
    new_ids = {p["id"] for p in new_pins}
    merged = new_pins + [p for p in old_latest if p["id"] not in new_ids]
    latest = merged[:MAX_LATEST]
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated_at": fetched_at, "count": len(latest),
                   "pins": latest}, f, ensure_ascii=False, indent=1)
    save_seen(seen_list)

    n_archived = append_archive(new_pins, now.date())
    index = rebuild_index()

    print(f"SELESAI: {len(new_pins)} pin baru "
          f"({', '.join(f'{k}={v}' for k, v in per_topik.items())}); "
          f"latest={len(latest)}; diarsipkan={n_archived}; "
          f"total arsip={index['total']}")
    if gagal:
        print(f"Topik gagal: {', '.join(gagal)}")


if __name__ == "__main__":
    main()
