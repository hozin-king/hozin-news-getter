#!/usr/bin/env python3
"""Getter Auto: ambil artikel dari data/sources_auto.json.

Sumber di daftar itu ditemukan & divalidasi discover.py (mingguan).
Getter ini jalan setelah rantai getter utama (workflow_run di
getter-auto.yml, concurrency group yang sama) supaya tidak tabrakan
commit di data/.

Pola sama persis dengan getter1/getter2 (common.py): seen.json
bersama, baris articles.jsonl berformat {source, getter="auto",
title, link, pubDate, description, fetched_at}. Batas: maks 10
artikel baru per sumber per run.

Setelah tiap fetch, statistik sumber diperbarui di sources_auto.json:
  - sukses (>= 1 item ter-parse): last_ok = sekarang, fail_count = 0,
    ok_streak + 1; status "probation" naik ke "active" setelah
    ok_streak >= 3.
  - gagal: fail_count + 1, ok_streak = 0.
Drop sumber mati adalah urusan discover.py (revalidasi mingguan).

Hanya stdlib (via common.py) — tanpa dependensi.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
AUTO_FILE = os.path.join(DATA, "sources_auto.json")
MAX_PER_SOURCE = 10
PROMOTE_STREAK = 3

sys.path.insert(0, BASE)
from common import (ARTICLES_FILE, fetch, load_seen, parse_rss,  # noqa: E402
                    save_seen, url_hash)


def now_iso():
    return datetime.now(WIB).isoformat(timespec="seconds")


def load_auto():
    if os.path.exists(AUTO_FILE):
        try:
            with open(AUTO_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sources"), list):
                return data["sources"]
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_auto(sources):
    payload = {"updated_at": now_iso(), "count": len(sources),
               "sources": sources}
    with open(AUTO_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def main():
    sources = load_auto()
    active = [s for s in sources
              if s.get("status") in ("probation", "active")]
    if not active:
        print("[auto] tidak ada sumber otomatis — selesai")
        return
    print(f"[auto] {len(active)} sumber otomatis diproses")

    seen = load_seen()
    new_articles = []
    for s in active:
        name, url = s["name"], s["url"]
        try:
            items = parse_rss(fetch(url))
            if not items:
                raise IOError("feed kosong / tidak ter-parse")
        except Exception as e:
            s["fail_count"] = int(s.get("fail_count", 0)) + 1
            s["ok_streak"] = 0
            print(f"[auto] FAIL {name}: {e} "
                  f"(fail_count={s['fail_count']})", flush=True)
            continue
        s["fail_count"] = 0
        s["last_ok"] = now_iso()
        s["ok_streak"] = int(s.get("ok_streak", 0)) + 1
        if s.get("status") == "probation" and s["ok_streak"] >= PROMOTE_STREAK:
            s["status"] = "active"
            print(f"[auto] {name}: naik status -> active", flush=True)
        fresh = 0
        for it in items:
            if fresh >= MAX_PER_SOURCE:
                break
            h = url_hash(it["link"])
            if h in seen:
                continue
            seen.add(h)
            fresh += 1
            new_articles.append({
                "source": name,
                "getter": "auto",
                "title": it["title"],
                "link": it["link"],
                "pubDate": it["pubDate"],
                "description": it["description"],
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S+07:00",
                                            time.localtime()),
            })
        print(f"[auto] {name}: {len(items)} items, {fresh} new", flush=True)

    if new_articles:
        with open(ARTICLES_FILE, "a", encoding="utf-8") as f:
            for a in new_articles:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
    save_seen(seen)
    save_auto(sources)
    print(f"[auto] done, {len(new_articles)} new articles")


if __name__ == "__main__":
    main()
