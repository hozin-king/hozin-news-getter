#!/usr/bin/env python3
"""Attack feed: agregat serangan global dari SANS DShield untuk globe /uptime.

Jalankan: python3 attack_feed.py [--out data/attacks.json] [--top 25]

Sumber (tanpa API key, tanpa data palsu):
  - https://isc.sans.edu/api/topips — IP penyerang terbanyak 24 jam terakhir
  - https://isc.sans.edu/api/sources/top/100 — 100 sumber penyerang teratas
  - https://isc.sans.edu/api/ip/<ip> — negara AS tiap IP
  - https://isc.sans.edu/api/dailysummary/<tanggal> — total laporan hari ini

Bila feed gagal atau negara yang terkumpul < 5, skrip keluar 1 TANPA
menulis file (file lama tetap dipakai globe). Output JSON dipakai halaman
https://hozinking.my.id/uptime lewat jsDelivr.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = {"User-Agent": "hozin-news-getter/attack-feed (+https://hozinking.my.id/uptime)"}
TOPIPS_URL = "https://isc.sans.edu/api/topips/10/json"
TOPSOURCES_URL = "https://isc.sans.edu/api/sources/top/100/json"
IP_URL = "https://isc.sans.edu/api/ip/{ip}/json"
SUMMARY_URL = "https://isc.sans.edu/api/dailysummary/{date}/json"


def fetch_text(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def top_ips():
    root = ET.fromstring(fetch_text(TOPIPS_URL, timeout=20))
    out = []
    for el in root.findall("ipaddress"):
        src = (el.findtext("source") or "").strip()
        try:
            reports = int(el.findtext("reports") or 0)
        except ValueError:
            reports = 0
        if src and reports > 0:
            out.append((src, reports))
    return out


def top_sources():
    """100 sumber penyerang teratas (daftar 'top sources' DShield)."""
    root = ET.fromstring(fetch_text(TOPSOURCES_URL, timeout=20))
    out = []
    for el in root.findall("data"):
        ip = (el.findtext("ip") or "").strip()
        try:
            attacks = int(el.findtext("attacks") or 0)
        except ValueError:
            attacks = 0
        if ip and attacks > 0:
            out.append((ip, attacks))
    return out


def ip_country(ip):
    try:
        root = ET.fromstring(fetch_text(IP_URL.format(ip=ip), timeout=10))
        c = (root.findtext("ascountry") or "").strip().upper()
        return c if len(c) == 2 else ""
    except Exception:
        return ""


def daily_summary():
    try:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        root = ET.fromstring(fetch_text(SUMMARY_URL.format(date=date), timeout=15))
        daily = root.find("daily")
        if daily is None:
            return {}
        return {
            "records": int(daily.findtext("records") or 0),
            "sources": int(daily.findtext("sources") or 0),
        }
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/attacks.json")
    args = ap.parse_args()

    # Gabung dua daftar DShield: 10 IP teratas 24 jam + 100 sumber teratas.
    # Keduanya didominasi segelintir negara — itu bentuk asli datanya.
    seen = {}
    for ip, n in top_sources() + top_ips():
        seen[ip] = seen.get(ip, 0) + n
    ips = sorted(seen.items(), key=lambda x: -x[1])
    if len(ips) < 5:
        print(f"[attack-feed] hanya {len(ips)} IP terkumpul, batal (file lama dipertahankan)")
        return 1

    by_country = {}
    for ip, reports in ips:
        country = ip_country(ip)
        if not country or country == "XX":
            continue
        cur = by_country.setdefault(country, {"c": country, "n": 0, "ips": 0})
        cur["n"] += reports
        cur["ips"] += 1
        time.sleep(0.2)
    countries = sorted(by_country.values(), key=lambda x: -x["n"])[:20]
    if len(countries) < 5:
        print(f"[attack-feed] hanya {len(countries)} negara terkumpul, batal")
        return 1

    summary = daily_summary()
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "SANS Internet Storm Center (DShield)",
        "source_url": "https://isc.sans.edu/",
        "window": "100 sumber penyerang teratas + 10 IP teratas 24 jam (SANS DShield)",
        "total_reports_today": summary.get("records") or None,
        "total_sources_today": summary.get("sources") or None,
        "countries": countries,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, args.out)
    top = ", ".join(f"{c['c']} {c['n']:,}" for c in countries[:5])
    print(f"[attack-feed] OK: {len(countries)} negara, {sum(c['n'] for c in countries):,} laporan — {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
