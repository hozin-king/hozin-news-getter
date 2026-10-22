#!/usr/bin/env python3
"""Rollup berita: data/articles.jsonl -> data/latest.json + arsip harian.

Keluaran:
  - data/latest.json : MAKS 100 artikel terbaru (berdasarkan fetched_at,
    terbaru dulu). TETAP RINGKAS (aplikasi memuatnya tiap dibuka): kini
    ditambah field categories + content_status + content_chars, TANPA
    badan isi.
  - data/full.json : 100 artikel yang sama DENGAN isi lengkap (content)
    dari cache enrich (data/fulltext.jsonl) — untuk layar baca artikel.
  - data/categories/<slug>.json : per kategori (categories.json), MAKS
    100 artikel berkategori itu dari kolam 300 artikel terbaru, DENGAN
    isi lengkap.
  - data/archive/<TAHUN>/<BULAN>/<TANGGAL>.jsonl : artikel per tanggal
    fetched_at, 1 JSON per baris, MAKS 500 baris/hari. Dibangun ulang
    dari articles.jsonl tiap run; kini membawa categories +
    content_status (badan isi TIDAK diarsipkan — ada di fulltext.jsonl).
  - data/archive/index.json : jumlah artikel per hari / minggu ISO /
    bulan / tahun, dibuat ulang dari file arsip yang ada.

Artikel lama yang belum lewat enrich tetap diklasifikasikan dari
judul+ringkasannya (news_lib.classify) dan content_status-nya
'partial' sampai enrich memprosesnya.
Dijalankan oleh workflow news-rollup.yml setelah enrich.py.
Hanya stdlib — tanpa dependensi.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
ARTICLES_FILE = os.path.join(DATA, "articles.jsonl")
LATEST_FILE = os.path.join(DATA, "latest.json")
FULL_FILE = os.path.join(DATA, "full.json")
CACHE_FILE = os.path.join(DATA, "fulltext.jsonl")
TRANS_FILE = os.path.join(DATA, "translations.jsonl")
CATEGORIES_DIR = os.path.join(DATA, "categories")
ARCHIVE_DIR = os.path.join(DATA, "archive")
MAX_LATEST = 100
MAX_ARCHIVE_PER_DAY = 500
MAX_PER_CATEGORY = 100
CATEGORY_POOL = 300

sys.path.insert(0, BASE)
from common import url_hash  # noqa: E402
import news_lib  # noqa: E402


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


def load_fulltext():
    """Cache enrich: {url_hash: {content, content_status, categories}}."""
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    cache[row["hash"]] = row
                except (json.JSONDecodeError, KeyError):
                    continue
    return cache


def load_translations():
    """Cache terjemahan (translate.py): {url_hash: row}."""
    cache = {}
    if os.path.exists(TRANS_FILE):
        with open(TRANS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    cache[row["hash"]] = row
                except (json.JSONDecodeError, KeyError):
                    continue
    return cache


def _content_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def decorate(article, cache, translations=None):
    """Salin artikel + tempel categories/content_status/content_chars
    (+ content bila with_content). Tidak mengubah dict asal.

    Field aditif: video_* dari cache enrich (bila artikel punya video
    tertanam) dan — untuk artikel sumber asing yang sudah diterjemahkan
    translate.py — title/description/content versi Bahasa Indonesia
    plus penanda translated/source_lang/original_title. Terjemahan
    hanya ditempel bila content_hash-nya cocok dengan isi saat ini.
    """
    a = dict(article)
    a.pop("feed_content", None)  # bahan mentah, bukan konsumsi aplikasi
    h = url_hash(a.get("link", ""))
    row = cache.get(h)
    if row:
        a["categories"] = row.get("categories", [])
        a["content_status"] = row.get("content_status", "partial")
        a["content_chars"] = row.get("content_chars", 0)
        content = row.get("content", "")
        if row.get("video_url"):
            a["video_type"] = row.get("video_type", "")
            a["video_id"] = row.get("video_id", "")
            a["video_url"] = row.get("video_url", "")
    else:
        a["categories"] = news_lib.classify(
            a.get("title", ""), a.get("description", ""))
        a["content_status"] = "partial"
        a["content_chars"] = len(a.get("description", ""))
        content = a.get("description", "")
    trow = (translations or {}).get(h)
    if (trow and trow.get("content")
            and trow.get("content_hash") == _content_hash(content)):
        a["original_title"] = trow.get("original_title",
                                       a.get("title", ""))
        a["title"] = trow.get("title", a.get("title", ""))
        if trow.get("description"):
            a["description"] = trow["description"]
        a["translated"] = True
        a["source_lang"] = "en"
        content = trow["content"]
        a["content_chars"] = len(content)
    return a, content


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
    cache = load_fulltext()
    translations = load_translations()

    # ---- dekorasi: categories + status isi (+ isi untuk varian full) ----
    slim = []        # tanpa content — untuk latest & arsip
    withfull = []    # dengan content — untuk full.json & per-kategori
    for a in articles:
        s, content = decorate(a, cache, translations)
        slim.append(s)
        wf = dict(s)
        wf["content"] = content
        withfull.append(wf)

    # ---- latest: 100 terbaru (ringkas) ----
    latest = slim[:MAX_LATEST]
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now, "count": len(latest),
                   "articles": latest}, f, ensure_ascii=False, indent=1)

    # ---- full: 100 yang sama, dengan isi lengkap ----
    full = withfull[:MAX_LATEST]
    with open(FULL_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now, "count": len(full),
                   "articles": full}, f, ensure_ascii=False, indent=1)

    # ---- per kategori: dari kolam artikel terbaru, dengan isi ----
    os.makedirs(CATEGORIES_DIR, exist_ok=True)
    cat_counts = {}
    for slug in news_lib.load_categories():
        items = [a for a in withfull[:CATEGORY_POOL]
                 if slug in a.get("categories", [])][:MAX_PER_CATEGORY]
        cat_counts[slug] = len(items)
        with open(os.path.join(CATEGORIES_DIR, f"{slug}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"updated_at": now, "category": slug,
                       "count": len(items), "articles": items},
                      f, ensure_ascii=False, indent=1)

    # ---- arsip per hari (bangun ulang dari sumber; tanpa badan isi) ----
    by_day = {}
    for a in slim:
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

    n_translated = sum(1 for a in withfull if a.get("translated"))
    print(f"SELESAI: {len(articles)} artikel dibaca; latest={len(latest)}; "
          f"arsip {len(by_day)} hari, total terarsip={index['total']}; "
          f"cache fulltext={len(cache)}; terjemahan aktif={n_translated}; "
          f"per-kategori={cat_counts}")


if __name__ == "__main__":
    main()
