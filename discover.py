#!/usr/bin/env python3
"""Penemuan sumber otomatis (mingguan) untuk Hozin News Getter.

Dijalankan workflow source-discovery.yml tiap Senin 03:17 UTC. Hemat:
bukan tiap run getter — cukup mingguan, dengan batas keras jumlah
sumber & kandidat baru, dan jeda sopan antar request.

Kanal berburu:
  a) data/seed_domains.txt — domain media Indonesia; yang belum jadi
     sumber, path feed umumnya dicoba: /rss, /feed, /rss.xml,
     /feed.xml, /index.rss, /berita/rss.
  b) Google News RSS per topik (indonesia, teknologi, ekonomi,
     olahraga, hiburan) — domain sumber artikelnya diambil, lalu
     path feed yang sama diprobe.

Validasi kandidat (ketat): HTTP 200, XML RSS/Atom ter-parse,
>= 3 item, item terbaru <= 7 hari, heuristik Bahasa Indonesia
(kata umum "dan/yang/di/ini/untuk" muncul di judul/deskripsi),
dan bukan duplikat sumber getter1/getter2 maupun sources_auto.

data/sources_auto.json: {name, url, added_at, last_ok, fail_count,
ok_streak, status}. Sumber baru berstatus "probation" (getter_auto
menaikkannya ke "active" setelah 3 run sukses berturut). Revalidasi
tiap run discover: fetch gagal -> fail_count+1; fail_count >= 3 ATAU
last_ok > 14 hari -> sumber di-DROP.

Hanya stdlib — tanpa dependensi. Tidak mengubah getter1/getter2.
"""
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

WIB = timezone(timedelta(hours=7))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SEED_FILE = os.path.join(DATA, "seed_domains.txt")
AUTO_FILE = os.path.join(DATA, "sources_auto.json")

FEED_PATHS = ["/rss", "/feed", "/rss.xml", "/feed.xml", "/index.rss",
              "/berita/rss"]
GN_TOPICS = ["indonesia", "teknologi", "ekonomi", "olahraga", "hiburan"]
MAX_AUTO_SOURCES = 30
MAX_NEW_PER_RUN = 10
MAX_GN_DOMAINS = 15
DELAY = 1.0  # jeda sopan antar request (detik)
ID_WORDS = ["dan", "yang", "di", "ini", "untuk"]
MULTI_SUFFIX = {"co.id", "or.id", "go.id", "ac.id", "web.id", "my.id",
                "sch.id", "co.uk", "com.au"}

sys.path.insert(0, BASE)
from common import UA, parse_rss  # noqa: E402

CTX_UNVERIFIED = ssl.create_default_context()
CTX_UNVERIFIED.check_hostname = False
CTX_UNVERIFIED.verify_mode = ssl.CERT_NONE


def now_wib():
    return datetime.now(WIB)


def now_iso():
    return now_wib().isoformat(timespec="seconds")


def probe_fetch(url, timeout=12):
    """Fetch ringan 1x percobaan; kembalikan bytes hanya bila HTTP 200."""
    try:
        out = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-A", UA,
             "-H", "Accept: application/rss+xml,application/xml,text/xml,*/*",
             "-w", "\n%{http_code}", url],
            capture_output=True, timeout=timeout + 5)
        if out.returncode == 0 and out.stdout:
            body, _, code = out.stdout.rpartition(b"\n")
            if code.strip() == b"200" and body:
                return body
            return None
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml,application/xml,text/xml,*/*"})
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=CTX_UNVERIFIED) as r:
            if r.status == 200:
                return r.read()
    except Exception:
        pass
    return None


def host_of(url):
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def reg_domain(url_or_host):
    """Domain registrable kasar: a.co.id / a.com style."""
    h = url_or_host
    if "://" in h:
        h = host_of(h)
    parts = [p for p in h.split(".") if p]
    if len(parts) <= 2:
        return h
    if ".".join(parts[-2:]) in MULTI_SUFFIX:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def source_name(domain):
    stem = domain.split(".")[0]
    return re.sub(r"[^a-z0-9]", "", stem.lower()) or "sumber"


def parse_feed_any(xml_bytes):
    """Parse RSS (item) maupun Atom (entry) -> list dict seperti common.

    Tiap item: title, link, pubDate (mentah), description, date (datetime
    aware atau None). Atom namespaces ditangani via pencocokan local-name.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    def child_text(el, names):
        for ch in el:
            if local(ch.tag) in names and ch.text:
                return ch.text.strip()
        return ""

    def to_dt(raw):
        if not raw:
            return None
        try:
            dt = parsedate_to_datetime(raw)
            if dt is not None:
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    items = []
    for el in root.iter():
        kind = local(el.tag)
        if kind not in ("item", "entry"):
            continue
        title = child_text(el, ("title",))
        desc = child_text(el, ("description", "summary", "content"))
        date_raw = child_text(el, ("pubDate", "published", "updated", "date"))
        link = child_text(el, ("link",))
        if kind == "entry" and not link:
            for ch in el:
                if local(ch.tag) == "link":
                    rel = ch.get("rel") or "alternate"
                    if rel == "alternate" and ch.get("href"):
                        link = ch.get("href")
                        break
        if not title or not link:
            continue
        items.append({"title": title, "link": link, "pubDate": date_raw,
                      "description": desc[:500], "date": to_dt(date_raw)})
    return items


def validate_body(body):
    """Validasi isi feed (tanpa fetch). Kembalikan (items, alasan|None)."""
    items = parse_feed_any(body)
    if not items:
        # parse_rss common sebagai cadangan jalur RSS klasik
        items = [{"title": it["title"], "link": it["link"],
                  "pubDate": it["pubDate"], "description": it["description"],
                  "date": None} for it in parse_rss(body)]
        for it in items:
            try:
                dt = parsedate_to_datetime(it["pubDate"]) if it["pubDate"] else None
                it["date"] = (dt if dt and dt.tzinfo else
                              (dt.replace(tzinfo=timezone.utc) if dt else None))
            except Exception:
                it["date"] = None
    if len(items) < 3:
        return None, f"item < 3 (dapat {len(items)})"
    dated = [it["date"] for it in items if it["date"]]
    if not dated:
        return None, "tidak ada tanggal item yang terbaca"
    newest = max(dated)
    if newest < now_wib() - timedelta(days=7):
        return None, f"item terbaru terlalu lama ({newest.date().isoformat()})"
    blob = " ".join((it["title"] + " " + it["description"]) for it in items).lower()
    hits = sum(len(re.findall(r"\b" + w + r"\b", blob)) for w in ID_WORDS)
    if hits < 5:
        return None, f"heuristik Bahasa Indonesia gagal (skor {hits})"
    return items, None


def validate_feed(url):
    """Fetch + validasi. Kembalikan (items, alasan_gagal|None)."""
    body = probe_fetch(url)
    if body is None:
        return None, "fetch gagal / bukan HTTP 200"
    return validate_body(body)


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
    os.makedirs(DATA, exist_ok=True)
    payload = {"updated_at": now_iso(), "count": len(sources),
               "sources": sources}
    with open(AUTO_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def existing_sources():
    """(set url feed, set domain registrable, set nama) dari getter1/2."""
    urls, domains, names = set(), set(), set()
    for mod in ("getter1", "getter2"):
        try:
            m = __import__(mod)
            for name, url in m.SOURCES:
                urls.add(url.rstrip("/"))
                domains.add(reg_domain(url))
                names.add(name)
        except Exception as e:
            print(f"peringatan: gagal membaca {mod}.SOURCES: {e}")
    return urls, domains, names


def load_seed_domains():
    domains = []
    if os.path.exists(SEED_FILE):
        with open(SEED_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    domains.append(line.lower())
    return domains


def google_news_domains():
    """Domain sumber artikel dari Google News RSS per topik."""
    found = []
    seen = set()
    for topic in GN_TOPICS:
        q = urllib.parse.quote(topic)
        url = (f"https://news.google.com/rss/search?q={q}"
               "&hl=id&gl=ID&ceid=ID:id")
        body = probe_fetch(url, timeout=15)
        time.sleep(DELAY)
        if not body:
            print(f"[gn] topik '{topic}': fetch gagal")
            continue
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            print(f"[gn] topik '{topic}': XML tidak ter-parse")
            continue
        n = 0
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] == "source":
                src = el.get("url") or ""
                d = reg_domain(src) if src else ""
                if d and d not in seen:
                    seen.add(d)
                    found.append(d)
                    n += 1
        print(f"[gn] topik '{topic}': {n} domain sumber baru")
    return found


def probe_domain(domain, known_urls):
    """Coba path feed umum; kembalikan (feed_url, items, alasan_terakhir).

    Varian host kedua (www <-> non-www) hanya dicoba bila varian
    pertama sama sekali tidak merespons HTTP 200 di path mana pun.
    """
    last_reason = "tidak ada path yang merespons"
    for scheme_host in (f"https://{domain}", f"https://www.{domain}"):
        host_alive = False
        for path in FEED_PATHS:
            url = scheme_host + path
            if url.rstrip("/") in known_urls:
                continue
            body = probe_fetch(url)
            time.sleep(DELAY)
            if body is None:
                continue
            host_alive = True
            items, reason = validate_body(body)
            if items is not None:
                return url, items, None
            last_reason = reason
        if host_alive:
            break
    return None, None, last_reason


def main():
    known_urls, known_domains, known_names = existing_sources()
    sources = load_auto()
    for s in sources:
        known_urls.add(s["url"].rstrip("/"))
        known_domains.add(reg_domain(s["url"]))
        known_names.add(s["name"])
    print(f"[discover] sumber tetap: {len(known_urls)} feed; "
          f"sources_auto saat ini: {len(sources)}")

    # ---- 1) revalidasi sumber otomatis yang sudah ada ----
    kept = []
    dropped = 0
    for s in sources:
        stale = False
        if s.get("last_ok"):
            try:
                last_ok = datetime.fromisoformat(s["last_ok"])
                stale = last_ok < now_wib() - timedelta(days=14)
            except ValueError:
                pass
        if stale:
            print(f"[revalidasi] DROP {s['name']}: last_ok > 14 hari")
            dropped += 1
            continue
        body = probe_fetch(s["url"])
        time.sleep(DELAY)
        if body is None or not parse_feed_any(body):
            s["fail_count"] = int(s.get("fail_count", 0)) + 1
            print(f"[revalidasi] {s['name']}: fetch gagal "
                  f"(fail_count={s['fail_count']})")
            if s["fail_count"] >= 3:
                print(f"[revalidasi] DROP {s['name']}: fail_count >= 3")
                dropped += 1
                continue
        else:
            s["fail_count"] = 0
            s["last_ok"] = now_iso()
        kept.append(s)
    sources = kept

    # ---- 2) berburu kandidat baru ----
    candidates = []
    for d in load_seed_domains():
        if reg_domain(d) not in known_domains:
            candidates.append(("seed", d))
    gn = google_news_domains()
    gn_new = 0
    for d in gn:
        if reg_domain(d) not in known_domains and gn_new < MAX_GN_DOMAINS:
            candidates.append(("googlenews", d))
            gn_new += 1
    print(f"[discover] {len(candidates)} domain kandidat untuk diprobe "
          f"(seed + {gn_new} dari Google News)")

    added, rejected = 0, []
    for origin, domain in candidates:
        if len(sources) >= MAX_AUTO_SOURCES:
            print(f"[discover] batas {MAX_AUTO_SOURCES} sumber otomatis tercapai")
            break
        if added >= MAX_NEW_PER_RUN:
            print(f"[discover] batas {MAX_NEW_PER_RUN} kandidat baru/run tercapai")
            break
        feed_url, items, reason = probe_domain(domain, known_urls)
        if feed_url is None:
            rejected.append((domain, reason))
            print(f"[probe] {domain}: ditolak ({reason})")
            continue
        name = source_name(reg_domain(feed_url))
        base_name, i = name, 2
        while name in known_names:
            name = f"{base_name}{i}"
            i += 1
        sources.append({
            "name": name,
            "url": feed_url,
            "added_at": now_iso(),
            "last_ok": now_iso(),
            "fail_count": 0,
            "ok_streak": 0,
            "status": "probation",
            "origin": origin,
        })
        known_names.add(name)
        known_urls.add(feed_url.rstrip("/"))
        known_domains.add(reg_domain(feed_url))
        added += 1
        print(f"[probe] {domain}: LOLOS -> {feed_url} "
              f"({len(items)} item) sebagai '{name}' [probation]")

    save_auto(sources)
    print(f"[discover] SELESAI: kandidat divalidasi lolos={added}, "
          f"ditolak={len(rejected)}, dropped={dropped}, "
          f"total sources_auto={len(sources)}")


if __name__ == "__main__":
    main()
