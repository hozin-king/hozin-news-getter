#!/usr/bin/env python3
"""Enrich: ambil isi FULL + klasifikasi kategori untuk artikel BARU.

Alur pipa: getter* -> data/articles.jsonl -> enrich.py -> rollup_news.py.

File ini hanya memproses artikel yang belum ada di cache
data/fulltext.jsonl (satu baris per artikel, kunci = url_hash tautan),
jadi tiap artikel diunduh halamannya PALING BANYAK SEKALI selama masa
hidup pipa. Cache dibatasi MAX_CACHE baris terbaru (berdasarkan urutan
articles.jsonl) agar repo tidak bengkak.

Isi cache per baris: {hash, link, content, content_status,
content_source, content_chars, categories, enriched_at}.
Kategori dihitung dari judul + ringkasan + isi full (news_lib).

Batas run: MAX_PER_RUN artikel baru (sisanya menyusul run berikutnya)
supaya run Actions tetap singkat. 1 request per artikel, UA jelas
(common.fetch). Dijalankan workflow news-rollup.yml sebelum rollup.
Hanya stdlib.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
ARTICLES_FILE = os.path.join(DATA, "articles.jsonl")
CACHE_FILE = os.path.join(DATA, "fulltext.jsonl")
MAX_PER_RUN = 80
MAX_CACHE = 2000

sys.path.insert(0, BASE)
from common import fetch, url_hash  # noqa: E402
import news_lib  # noqa: E402


def now_iso():
    return datetime.now(WIB).isoformat(timespec="seconds")


def load_articles():
    items = []
    if not os.path.exists(ARTICLES_FILE):
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


def load_cache():
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


def main():
    articles = load_articles()
    cache = load_cache()
    todo = []
    for a in articles:
        h = url_hash(a.get("link", ""))
        if h and h not in cache:
            todo.append((h, a))
    # TERBARU DULU: articles.jsonl urut append (lama -> baru), sedangkan
    # latest.json & file kategori menampilkan yang terbaru — artikel
    # segar harus lebih dulu diperkaya, antrean lama menyusul.
    todo.sort(key=lambda ha: str(ha[1].get("fetched_at") or ""),
              reverse=True)
    print(f"[enrich] {len(articles)} artikel; cache {len(cache)}; "
          f"belum diproses {len(todo)}; batas run {MAX_PER_RUN}")

    done = 0
    for h, a in todo[:MAX_PER_RUN]:
        try:
            res = news_lib.get_full_content(a, fetch)
        except Exception as e:  # jangan jatuhkan run karena 1 artikel
            print(f"[enrich] LEWAT {a.get('link')}: {e}", flush=True)
            res = {"content": (a.get("description") or "").strip(),
                   "content_status": "partial", "content_source": "summary",
                   "content_chars": len((a.get("description") or ""))}
        cats = news_lib.classify(a.get("title", ""),
                                 (a.get("description") or "") + "\n" +
                                 (res.get("content") or ""))
        cache[h] = {
            "hash": h,
            "link": a.get("link", ""),
            "content": res["content"],
            "content_status": res["content_status"],
            "content_source": res["content_source"],
            "content_chars": res["content_chars"],
            "categories": cats,
            "enriched_at": now_iso(),
        }
        done += 1
        print(f"[enrich] {done}: [{res['content_status']}/"
              f"{res['content_source']}] {res['content_chars']} char "
              f"cats={cats} {a.get('title', '')[:60]}", flush=True)

    # Tulis ulang cache mengikuti urutan articles.jsonl (terbaru di
    # akhir), batasi MAX_CACHE entri terakhir.
    order = {}
    for idx, a in enumerate(articles):
        order[url_hash(a.get("link", ""))] = idx
    rows = sorted(cache.values(),
                  key=lambda r: order.get(r["hash"], -1))[-MAX_CACHE:]
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    n_full = sum(1 for r in rows if r["content_status"] == "full")
    print(f"[enrich] SELESAI: {done} baru diproses; cache {len(rows)} "
          f"baris ({n_full} full)")


if __name__ == "__main__":
    main()
