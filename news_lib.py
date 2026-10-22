#!/usr/bin/env python3
"""news_lib: klasifikasi kategori + pengambilan isi berita FULL.

Dipakai enrich.py (dan rollup_news.py untuk klasifikasi artikel lama).
Hanya stdlib — tanpa dependensi, pola sama dengan common.py/rollup.

Klasifikasi (categories.json):
  Skor per kategori = 3*s(strong di judul) + 2*(kata pendukung di judul)
                    + 2*(strong di badan) + 1*(kata pendukung di badan)
  Kategori ditempel bila skor >= 3. Pencocokan memakai batas kata
  (?<![a-z0-9]) ... (?![a-z0-9]) supaya 'ott' tidak kena 'roti' dsb.

Isi FULL (get_full_content), urutan usaha:
  (a) feed_content (dari content:encoded feed, ditangkap common.py)
      yang cukup panjang -> status full, sumber 'feed'.
  (b) unduh halaman artikel (1 request, UA jelas) lalu ekstrak teks
      paragraf artikel secara konservatif -> full bila hasilnya jauh
      lebih panjang dari ringkasan RSS; bila pendek/tidak lebih baik
      dari ringkasan -> partial, isi = ringkasan.
  (c) semua gagal -> partial, isi = ringkasan RSS.
Penanda selalu jujur: tidak pernah menandai ringkasan sebagai full.
"""
import html
import json
import os
import re
from html.parser import HTMLParser

BASE = os.path.dirname(os.path.abspath(__file__))
CATEGORIES_FILE = os.path.join(BASE, "categories.json")

# ---------------------------------------------------------------------------
# Klasifikasi kategori
# ---------------------------------------------------------------------------
_CAT_CACHE = None


def load_categories(path=CATEGORIES_FILE):
    global _CAT_CACHE
    if _CAT_CACHE is None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        compiled = {}
        for slug, spec in data.get("categories", {}).items():
            def rx(words):
                return [re.compile(r"(?<![a-z0-9])" + re.escape(w.lower()) +
                                   r"(?![a-z0-9])")
                        for w in words if w and w.strip()]
            compiled[slug] = {
                "label": spec.get("label", slug),
                "strong": rx(spec.get("strong", [])),
                "weak": rx(spec.get("keywords", [])),
            }
        _CAT_CACHE = compiled
    return _CAT_CACHE


def _count(patterns, text):
    return sum(1 for p in patterns if p.search(text))


def classify(title, body=""):
    """Kembalikan list slug kategori (urut alfabet). body = ringkasan+isi."""
    t = (title or "").lower()
    b = (body or "").lower()
    out = []
    for slug, spec in load_categories().items():
        score = (3 * _count(spec["strong"], t)
                 + 2 * _count(spec["weak"], t)
                 + 2 * _count(spec["strong"], b)
                 + 1 * _count(spec["weak"], b))
        if score >= 3:
            out.append(slug)
    return sorted(out)


# ---------------------------------------------------------------------------
# Pembersih HTML -> teks polos
# ---------------------------------------------------------------------------
class _TextExtract(HTMLParser):
    """Kumpulkan teks kandidat paragraf: <p> DAN <div> (banyak situs
    berita WordPress menaruh tiap paragraf artikel di <div> polos).
    Teks masuk ke frame kandidat terdalam; teks langsung milik div
    pembungkus biasanya hanya spasi sehingga tersaring oleh panjang.
    """

    SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe", "form",
                 "header", "footer", "nav", "aside", "button", "select",
                 "textarea", "figure"}
    CANDIDATE_TAGS = {"p", "div"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.link_depth = 0
        self.frames = []  # tumpukan {tag, buf, link_len}
        self.paras = []   # (teks, panjang_teks_tautan)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "a" and self.frames and self.skip_depth == 0:
            self.link_depth += 1
        elif tag in self.CANDIDATE_TAGS and self.skip_depth == 0:
            self.frames.append({"tag": tag, "buf": [], "link_len": 0})

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
        elif tag == "a" and self.link_depth > 0:
            self.link_depth -= 1
        elif tag in self.CANDIDATE_TAGS and self.frames:
            # tutup frame teratas yang tag-nya cocok (toleran HTML tak rapi)
            for i in range(len(self.frames) - 1, -1, -1):
                if self.frames[i]["tag"] == tag:
                    frame = self.frames.pop(i)
                    text = " ".join("".join(frame["buf"]).split())
                    if text:
                        self.paras.append((text, frame["link_len"]))
                    break

    def handle_data(self, data):
        if self.frames and self.skip_depth == 0:
            top = self.frames[-1]
            top["buf"].append(data)
            if self.link_depth > 0:
                top["link_len"] += len(data)


def html_to_paragraphs(html_text):
    """Ekstrak paragraf artikel dari HTML mentah (konservatif).

    Aturan: paragraf >= 60 karakter, rasio teks tautan < 40%, bukan
    boilerplate umum (hak cipta, baca juga, iklan, dsb). Paragraf
    duplikat dibuang. Mengembalikan teks gabungan ('' bila gagal).
    """
    parser = _TextExtract()
    try:
        parser.feed(html_text or "")
    except Exception:
        return ""
    boiler = ("all rights reserved", "copyright", "©", "baca juga:",
              "baca juga", "simak juga", "advertisement", "iklan",
              "scroll to continue", "halaman selanjutnya", "bagikan:",
              "share:", "follow us", "subscribe", "sign up for",
              "download aplikasi", "klik di sini untuk")
    seen = set()
    out = []
    for text, link_len in parser.paras:
        if len(text) < 60:
            continue
        if link_len > 0.4 * max(len(text), 1):
            continue
        low = text.lower()
        if any(b in low for b in boiler) and len(text) < 200:
            continue
        key = low[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return "\n\n".join(out)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_to_text(html_text):
    """HTML -> teks polos sederhana (untuk content:encoded dari feed)."""
    if not html_text:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text,
                  flags=re.S | re.I)
    text = re.sub(r"</p\s*>|<br\s*/?>|</div\s*>|</li\s*>", "\n", text,
                  flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if len(ln) >= 2]
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Pengambilan isi full
# ---------------------------------------------------------------------------
FULL_MIN_CHARS = 600      # isi di bawah ini tidak layak disebut full
FEED_MIN_CHARS = 600      # feed_content minimal untuk dianggap full


def get_full_content(article, fetcher):
    """Tentukan isi artikel selengkap mungkin.

    article: dict ala articles.jsonl (title/link/description/feed_content).
    fetcher: callable(url) -> bytes (common.fetch; 1 request per artikel).
    Return dict: {content, content_status: full|partial,
                  content_source: feed|page|summary, content_chars}.
    """
    raw_desc = (article.get("description") or "").strip()
    # Ringkasan beberapa feed mengandung HTML mentah — bersihkan dulu
    # supaya field content selalu teks polos.
    desc = html_to_text(raw_desc) if "<" in raw_desc else raw_desc

    # (a) isi penuh dari feed (content:encoded)
    feed_text = html_to_text(article.get("feed_content") or "")
    if len(feed_text) >= FEED_MIN_CHARS and len(feed_text) > len(desc):
        return {"content": feed_text, "content_status": "full",
                "content_source": "feed", "content_chars": len(feed_text)}

    # (b) halaman artikel
    try:
        raw = fetcher(article["link"])
        page_text = html_to_paragraphs(raw.decode("utf-8", "ignore"))
    except Exception:
        page_text = ""
    if len(page_text) >= FULL_MIN_CHARS and len(page_text) > len(desc):
        return {"content": page_text, "content_status": "full",
                "content_source": "page", "content_chars": len(page_text)}

    # Gagal full: pakai yang lebih panjang antara ringkasan & hasil parsial,
    # tapi status tetap partial (jujur — bukan isi lengkap).
    best = desc
    if len(page_text) > len(best):
        best = page_text
    if len(feed_text) > len(best):
        best = feed_text
    return {"content": best, "content_status": "partial",
            "content_source": "summary", "content_chars": len(best)}
