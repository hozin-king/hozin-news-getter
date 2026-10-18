#!/usr/bin/env python3
"""Scraper CCTV publik Indonesia -> cctv.json (per wilayah).

Tiap run 2 PASS:
  (a) VERIFIKASI: cek semua URL stream lama (HEAD/GET+Range, paralel);
      yang mati -> cari pengganti dari sumber sama, kalau tidak ada -> hapus.
  (b) DISCOVERY: scrape normal untuk kamera baru/perubahan.
Log tiap run: dicek/mati/diganti/dihapus/baru.

Sumber AKTIF (terverifikasi 18 Okt 2026):
  - BPJT (bpjt.pu.go.id/cctv): ~1500 kamera tol, stream m3u8 langsung di JSON halaman.
  - Kemenhub (mudik.kemenhub.go.id/cctv): ~290 kamera m3u8 langsung (array JS di halaman).
  - Bandung Pelindung (pelindung.bandung.go.id:8443/api/cek): ~430 kamera m3u8.

Sumber GAGAL (dicatat jujur, tidak diarang):
  - Lewatmana (lewatmana.com/cam): semua endpoint /camvidurl/<id>/ kosong (kamera offline).
  - Travoy (travoy.jasamarga.com): tidak bisa dijangkau dari jaringan ini; kamera
    Jasa Marga sudah tercover via BPJT (host jmlive.jasamarga.com).
  - Jakarta Smart City: HTTPS gagal dari jaringan ini.
  - Yogyakarta (cctv.jogjakarta.go.id): tidak bisa dijangkau.
  - Bina Marga: mirror data BPJT (sengaja tidak diduplikasi).

Output: cctv.json
  {"updated_at": "...", "wilayah": [{"nama": "...", "sumber": "...",
    "kamera": [{"nama": "...", "lokasi": "...", "stream": "https://...m3u8",
                "lat": ..., "lon": ...}]}]}
Hanya stdlib (urllib) — tanpa dependensi.
"""
import json
import os
import re
import ssl
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
WIB = timezone(timedelta(hours=7))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE_DIR, "cctv.json")

# SSL: coba verifikasi normal dulu, fallback unverified (untuk API Bandung).
CTX_UNVERIFIED = ssl.create_default_context()
CTX_UNVERIFIED.check_hostname = False
CTX_UNVERIFIED.verify_mode = ssl.CERT_NONE


def fetch(url, timeout=30, unverified=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = CTX_UNVERIFIED if unverified else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        if not unverified:
            # coba sekali lagi tanpa verifikasi SSL
            try:
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=CTX_UNVERIFIED) as r:
                    return r.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
        print(f"  ! gagal fetch {url}: {e}", file=sys.stderr)
        return ""


def parse_bpjt():
    """BPJT: objek JSON {..,"stream":"..",..} di HTML."""
    print("BPJT ...")
    html = fetch("https://bpjt.pu.go.id/cctv", timeout=60)
    if not html:
        return []
    pats = re.findall(r'\{[^{}]*"stream"\s*:\s*"[^"]*"[^{}]*\}', html)
    objs = []
    for p in pats:
        try:
            objs.append(json.loads(p.replace("\\/", "/")))
        except Exception:
            continue
    print(f"  objek: {len(objs)}")
    grouped = defaultdict(list)
    for o in objs:
        if o.get("status") not in ("1", "online"):
            continue
        stream = (o.get("streamhls") or "").strip()
        if not stream or "m3u8" not in stream:
            continue
        ruas = (o.get("nama_ruas") or "Lainnya").strip()
        nama = (o.get("nama_km") or o.get("nama_segment") or "Kamera").strip()
        lokasi = (o.get("nama_segment") or "").strip()
        try:
            lat = float(o["lat"]) if o.get("lat") else None
            lon = float(o["lon"]) if o.get("lon") else None
        except (ValueError, TypeError):
            lat, lon = None, None
        grouped[ruas].append({"nama": nama, "lokasi": lokasi,
                              "stream": stream, "lat": lat, "lon": lon})
    out = []
    for ruas in sorted(grouped):
        out.append({"nama": f"Tol {ruas}", "sumber": "BPJT",
                    "kamera": sorted(grouped[ruas], key=lambda k: k["nama"])})
    return out


def _extract_js_array(html, varname):
    m = re.search(varname + r"\s*=\s*\[", html)
    if not m:
        return []
    start = html.find("[", m.start())
    depth, instr, esc, q, end = 0, False, False, "", start
    for j in range(start, len(html)):
        c = html[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                instr = False
        else:
            if c in "\"'":
                instr, q = True, c
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
    try:
        return json.loads(html[start:end])
    except Exception as e:
        print(f"  ! parse array {varname} gagal: {e}", file=sys.stderr)
        return []


def parse_kemenhub():
    """Kemenhub: const cctv = [...] di HTML; ambil protocol m3u8."""
    print("Kemenhub ...")
    html = fetch("https://mudik.kemenhub.go.id/cctv", timeout=60)
    if not html:
        return []
    objs = _extract_js_array(html, r"cctv")
    print(f"  objek: {len(objs)}")
    grouped = defaultdict(list)
    for o in objs:
        if o.get("protocol") != "m3u8":
            continue
        stream = (o.get("stream_url") or "").strip()
        if not stream:
            continue
        prov = (o.get("nama_provinsi") or "Lainnya").strip().title()
        nama = (o.get("nama_cctv") or o.get("nama_lokasi") or "Kamera").strip()
        lokasi = (o.get("nama_lokasi") or "").strip()
        kab = (o.get("nama_kabupaten_kota") or "").strip()
        if kab and kab.lower() not in lokasi.lower():
            lokasi = f"{lokasi} ({kab})".strip()
        grouped[prov].append({"nama": nama, "lokasi": lokasi,
                              "stream": stream, "lat": None, "lon": None})
    out = []
    for prov in sorted(grouped):
        out.append({"nama": prov, "sumber": "Kemenhub",
                    "kamera": sorted(grouped[prov], key=lambda k: k["nama"])})
    return out


def parse_bandung():
    """Bandung Pelindung: JSON API langsung."""
    print("Bandung ...")
    raw = fetch("https://pelindung.bandung.go.id:8443/api/cek",
                timeout=90, unverified=True)
    if not raw:
        return []
    try:
        cams = json.loads(raw)
    except Exception as e:
        print(f"  ! parse JSON Bandung gagal: {e}", file=sys.stderr)
        return []
    print(f"  objek: {len(cams)}")
    kams = []
    for c in cams:
        stream = (c.get("stream_cctv") or "").strip()
        if not stream:
            continue
        nama = (c.get("cctv_name") or "Kamera").strip()
        try:
            lat = float(c["lat"]) if c.get("lat") else None
            lon = float(c["lng"]) if c.get("lng") else None
        except (ValueError, TypeError):
            lat, lon = None, None
        kams.append({"nama": nama, "lokasi": "Kota Bandung",
                     "stream": stream, "lat": lat, "lon": lon})
    if not kams:
        return []
    return [{"nama": "Kota Bandung", "sumber": "Pelindung Bandung",
             "kamera": sorted(kams, key=lambda k: k["nama"])}]


from concurrent.futures import ThreadPoolExecutor, as_completed


LOGO_BASE = ("https://raw.githubusercontent.com/hozin-king/hozin-news-getter"
             "/main/cctv/logos")
LOGOS = {
    "BPJT": LOGO_BASE + "/bpjt.png",
    "Kemenhub": LOGO_BASE + "/kemenhub.png",
    "Pelindung Bandung": LOGO_BASE + "/pelindung.png",
}


def check_url_alive(url, timeout=10):
    """Cek cepat tanpa download full stream: HEAD, fallback GET + Range."""
    if not url:
        return False
    for method in ("HEAD", "GET"):
        try:
            h = {"User-Agent": UA}
            if method == "GET":
                h["Range"] = "bytes=0-1023"
            req = urllib.request.Request(url, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status < 400:
                    if method == "GET":
                        r.read(1024)
                    return True
        except Exception:
            continue
    return False


def ckey(sumber, nama):
    return (str(sumber or "").strip().lower(),
            str(nama or "").strip().lower())


def main():
    # ---- muat data lama ----
    old = {}
    if os.path.exists(OUT_PATH):
        try:
            d = json.load(open(OUT_PATH, encoding="utf-8"))
            for w in d.get("wilayah", []):
                for k in w.get("kamera", []):
                    old[ckey(w.get("sumber"), k.get("nama"))] = (w, k)
        except Exception as e:
            print(f"  ! baca {OUT_PATH} gagal: {e}", file=sys.stderr)

    # ---- PASS A: verifikasi URL lama (paralel) ----
    items = list(old.items())
    alive = {}
    if items:
        print(f"VERIFY: cek {len(items)} URL stream ...", flush=True)
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(check_url_alive, k.get("stream", "")): key
                    for key, (w, k) in items}
            for f in as_completed(futs):
                try:
                    alive[futs[f]] = bool(f.result())
                except Exception:
                    alive[futs[f]] = False

    # ---- PASS B: discovery (scrape normal) ----
    wilayah = []
    wilayah += parse_bpjt()
    wilayah += parse_kemenhub()
    wilayah += parse_bandung()
    fresh = {}
    for w in wilayah:
        for k in w.get("kamera", []):
            fresh[ckey(w.get("sumber"), k.get("nama"))] = (
                w.get("nama", ""), w.get("sumber", ""), k)

    # ---- gabung ----
    n_dead = n_replaced = n_removed = n_new = 0
    merged = {}

    def get_w(nama_w, sumber):
        k = (nama_w, sumber)
        if k not in merged:
            merged[k] = {"nama": nama_w, "sumber": sumber,
                         "logo": LOGOS.get(sumber, ""),
                         "kamera": []}
        return merged[k]

    for key, (w_old, k_old) in items:
        if alive.get(key):
            if key in fresh:  # hidup: pakai data fresh (URL mungkin berubah)
                wn, src, k_new = fresh[key]
                get_w(wn, src)["kamera"].append(k_new)
            else:
                get_w(w_old.get("nama", ""),
                      w_old.get("sumber", ""))["kamera"].append(k_old)
        else:
            n_dead += 1
            if key in fresh:  # mati tapi ada pengganti dari sumber sama
                wn, src, k_new = fresh[key]
                get_w(wn, src)["kamera"].append(k_new)
                n_replaced += 1
            else:  # mati dan tidak ada gantinya -> hapus
                n_removed += 1

    for key, (wn, src, k_new) in fresh.items():
        if key not in old:
            get_w(wn, src)["kamera"].append(k_new)
            n_new += 1

    wilayah_out = sorted(merged.values(), key=lambda w: w["nama"])
    for w in wilayah_out:
        w["kamera"] = sorted(w["kamera"], key=lambda k: k["nama"])
    total = sum(len(w["kamera"]) for w in wilayah_out)
    data = {
        "updated_at": datetime.now(WIB).isoformat(timespec="seconds"),
        "sumber_gagal": [
            "Lewatmana: kamera offline (endpoint /camvidurl kosong)",
            "Travoy: tidak terjangkau; kamera Jasa Marga tercover via BPJT",
            "Jakarta Smart City: HTTPS gagal dari jaringan scraper",
            "Yogyakarta: tidak terjangkau dari jaringan scraper",
            "Bina Marga: mirror data BPJT (sengaja tidak diduplikasi)",
        ],
        "wilayah": wilayah_out,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"VERIFY: {len(items)} dicek, {n_dead} mati "
          f"({n_replaced} diganti, {n_removed} dihapus)")
    print(f"DISCOVERY: {n_new} kamera baru")
    print(f"SELESAI: {len(wilayah_out)} wilayah, {total} kamera -> {OUT_PATH}")


if __name__ == "__main__":
    main()
