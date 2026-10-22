#!/usr/bin/env python3
"""Shared logic for Hozin News Getters.

Stdlib only (urllib + xml.etree) so it runs on stock GitHub runners
without pip installs. Sequential-safe: getters run one after another,
both read/write the same seen.json + articles.jsonl.
"""
import hashlib
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SEEN_FILE = os.path.join(DATA, "seen.json")
ARTICLES_FILE = os.path.join(DATA, "articles.jsonl")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

os.makedirs(DATA, exist_ok=True)


def fetch_curl(url, timeout=25):
    """Fetch via curl (handles flaky servers better than urllib)."""
    import subprocess
    out = subprocess.run(
        ["curl", "-sL", "--max-time", str(timeout),
         "-A", UA,
         "-H", "Accept: application/rss+xml,application/xml,text/xml,*/*",
         url],
        capture_output=True, timeout=timeout + 10)
    if out.returncode != 0 or not out.stdout:
        raise IOError(f"curl failed rc={out.returncode}")
    return out.stdout


def fetch_urllib(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml,application/xml,text/xml,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch(url, timeout=25, retries=3):
    last_err = None
    for attempt in range(retries):
        for impl in (fetch_curl, fetch_urllib):
            try:
                return impl(url, timeout)
            except Exception as e:
                last_err = e
        time.sleep(2 * (attempt + 1))
    raise last_err


CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"


def parse_rss(xml_bytes):
    """Return list of dicts: title, link, pubDate, description,
    feed_content (isi dari <content:encoded> bila feed menyediakannya,
    HTML mentah, '' bila tidak ada). Atom <entry> juga ditangani."""
    items = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items
    for item in root.iter("item"):
        def text(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        title = text("title")
        link = text("link")
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "pubDate": text("pubDate"),
            "description": text("description")[:500],
            "feed_content": text(CONTENT_ENCODED)[:60000],
        })
    if items:
        return items
    # Atom: <entry> dengan namespace atom
    ns = {"a": "http://www.w3.org/2005/Atom",
          "c": "http://purl.org/rss/1.0/modules/content/"}
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        def atext(tag):
            el = entry.find(f"a:{tag}", ns)
            return (el.text or "").strip() if el is not None and el.text else ""
        title = atext("title")
        link = ""
        for el in entry.findall("a:link", ns):
            if el.get("rel") in (None, "alternate") and el.get("href"):
                link = el.get("href")
                break
        if not title or not link:
            continue
        content_el = entry.find("a:content", ns)
        feed_content = ((content_el.text or "") if content_el is not None
                        else "")
        items.append({
            "title": title,
            "link": link,
            "pubDate": atext("published") or atext("updated"),
            "description": atext("summary")[:500],
            "feed_content": feed_content[:60000],
        })
    return items


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_seen(seen):
    # Keep the file bounded: last 5000 hashes only
    trimmed = sorted(seen)[-5000:]
    with open(SEEN_FILE, "w") as f:
        json.dump(trimmed, f)


def url_hash(link):
    return hashlib.sha256(link.encode()).hexdigest()[:16]


def run_getter(name, sources):
    """sources: list of (source_name, rss_url). Returns (new_count, total)."""
    seen = load_seen()
    new_articles = []
    for source_name, rss_url in sources:
        try:
            xml_bytes = fetch(rss_url)
            items = parse_rss(xml_bytes)
        except Exception as e:
            print(f"[{name}] FAIL {source_name}: {e}", flush=True)
            continue
        fresh = 0
        for it in items:
            h = url_hash(it["link"])
            if h in seen:
                continue
            seen.add(h)
            fresh += 1
            art = {
                "source": source_name,
                "getter": name,
                "title": it["title"],
                "link": it["link"],
                "pubDate": it["pubDate"],
                "description": it["description"],
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S+07:00", time.localtime()),
            }
            # content:encoded dari feed (bahan isi FULL jalur feed di
            # enrich.py) — hanya disimpan bila feed memang memberikannya.
            if it.get("feed_content"):
                art["feed_content"] = it["feed_content"]
            new_articles.append(art)
        print(f"[{name}] {source_name}: {len(items)} items, {fresh} new", flush=True)

    if new_articles:
        with open(ARTICLES_FILE, "a", encoding="utf-8") as f:
            for a in new_articles:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
    save_seen(seen)
    return len(new_articles)
