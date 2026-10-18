#!/usr/bin/env python3
"""Sultan Content Scraper — auto-update konten Sultan Simulator.

Dijalankan harian via GitHub Actions (.github/workflows/sultan-content.yml).
Stdlib only (+ curl, tersedia di runner) mengikuti gaya news-getter.

Sumber (semua gratis, tanpa API key):
  1. Forbes Real-Time Billionaires (ajax endpoint publik):
     https://www.forbes.com/ajax/list/data?year=2026&uri=billionaires&type=person
     -> sultan/data/billionaires.json (100 teratas, net worth USD)
  2. Kurs USD->IDR (open.er-api.com, fallback frankfurter.app):
     -> sultan/data/fx.json
  3. Logo brand (Clearbit, fallback Google favicon):
     -> sultan/data/logos/<slug>.png + manifest di content.json

Output gabungan: sultan/data/content.json — "database" GitHub yang mudah
diakses aplikasi via raw.githubusercontent.com.
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
LOGOS = os.path.join(DATA, "logos")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

os.makedirs(LOGOS, exist_ok=True)

FORBES_URL = ("https://www.forbes.com/ajax/list/data"
              "?year=2026&uri=billionaires&type=person")
FX_URLS = [
    "https://open.er-api.com/v6/latest/USD",
    "https://api.frankfurter.app/latest?from=USD&to=IDR",
]


def fetch(url, timeout=30):
    out = subprocess.run(
        ["curl", "-sL", "--max-time", str(timeout), "-A", UA, url],
        capture_output=True, timeout=timeout + 15)
    if out.returncode != 0 or not out.stdout:
        raise IOError(f"curl gagal rc={out.returncode} url={url}")
    return out.stdout


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def scrape_billionaires():
    raw = fetch(FORBES_URL, timeout=40)
    people = json.loads(raw)
    rows = []
    for p in people:
        worth_m = p.get("realTimeWorth")  # juta USD
        if not worth_m:
            continue
        rows.append({
            "rank": p.get("realTimeRank") or 0,
            "name": p.get("name", ""),
            "net_worth_usd": int(round(worth_m * 1_000_000)),
            "country": p.get("country", ""),
            "source": p.get("source", ""),
            "industry": p.get("industry", ""),
            "age": p.get("age"),
            "forbes_uri": p.get("uri", ""),
        })
    rows.sort(key=lambda r: r["net_worth_usd"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    top = rows[:100]
    write_json(os.path.join(DATA, "billionaires.json"), {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "count": len(rows),
        "source": "Forbes Real-Time Billionaires",
        "billionaires": top,
    })
    print(f"billionaires: {len(rows)} total, {len(top)} disimpan")
    return top


def scrape_fx():
    usd_idr = None
    used = None
    for url in FX_URLS:
        try:
            d = json.loads(fetch(url, timeout=20))
            if "rates" in d and "IDR" in d["rates"]:
                usd_idr = float(d["rates"]["IDR"])
                used = url
                break
        except Exception as e:
            print(f"fx {url} gagal: {e}")
    if not usd_idr:
        raise IOError("semua sumber kurs gagal")
    obj = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "usd_idr": round(usd_idr, 2),
        "source": used,
    }
    write_json(os.path.join(DATA, "fx.json"), obj)
    print(f"fx: 1 USD = {obj['usd_idr']:,.0f} IDR ({used})")
    return obj


def is_image(data):
    return (data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:3] == b"\xff\xd8\xff"
            or data[:4] in (b"RIFF",)
            or data[:6] in (b"GIF87a", b"GIF89a"))


def scrape_logos():
    with open(os.path.join(BASE, "brands.json"), encoding="utf-8") as f:
        brands = json.load(f)
    manifest = {}
    ok = 0
    for b in brands:
        slug, domain = b["slug"], b["domain"]
        data = None
        tried = [
            f"https://logo.clearbit.com/{domain}?size=256",
            f"https://www.google.com/s2/favicons?domain={domain}&sz=256",
        ]
        if b.get("logo"):
            tried.insert(0, b["logo"])
        for url in tried:
            try:
                blob = fetch(url, timeout=20)
                if is_image(blob) and len(blob) > 200:
                    data = blob
                    break
            except Exception:
                continue
        if data:
            with open(os.path.join(LOGOS, slug + ".png"), "wb") as f:
                f.write(data)
            manifest[slug] = {"name": b["name"], "file": f"sultan/data/logos/{slug}.png",
                              "bytes": len(data)}
            ok += 1
        else:
            print(f"logo gagal: {slug} ({domain})")
    print(f"logos: {ok}/{len(brands)} berhasil")
    return manifest


def main():
    billionaires = []
    fx = {}
    try:
        billionaires = scrape_billionaires()
    except Exception as e:
        print(f"BILLIONAIRES GAGAL: {e}", file=sys.stderr)
    try:
        fx = scrape_fx()
    except Exception as e:
        print(f"FX GAGAL: {e}", file=sys.stderr)
    logos = scrape_logos()
    write_json(os.path.join(DATA, "content.json"), {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "fx": fx,
        "billionaires_top100": billionaires,
        "logos": logos,
    })
    print("content.json ditulis")


if __name__ == "__main__":
    main()
