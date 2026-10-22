#!/usr/bin/env python3
"""Translate: terjemahan otomatis EN -> ID untuk artikel sumber asing.

Alur pipa: getter* -> articles.jsonl -> enrich.py -> translate.py ->
rollup_news.py. Dijalankan workflow news-rollup.yml di antara enrich
dan rollup.

Hanya artikel dari sumber yang ditandai "translate": true di
data/sources_auto.json (atau ber-field source_lang di articles.jsonl)
yang diproses, dan hanya bila isi FULL-nya sudah ada di cache enrich
(data/fulltext.jsonl, content_status == "full") — artikel partial
ditahan sampai isinya lengkap, tidak diterbitkan setengah.

Yang diterjemahkan: judul + ringkasan (dibersihkan dari HTML) + isi
FULL. Isi panjang dipecah per paragraf menjadi potongan <= CHUNK_LIMIT
karakter untuk transport, diterjemahkan satu per satu (jeda antar
permintaan), lalu DIGABUNG UTUH kembali. Bila satu potongan pun gagal
setelah retry, SELURUH item ditahan (tidak ada baris cache yang
ditulis) dan dicatat di data/translate_report.json.

Mesin:
  - "google": endpoint Google Translate tidak resmi
    (translate.googleapis.com/translate_a/single, client=gtx) via
    curl/urllib — tanpa API key. Ini mesin utama.
  - "argos": Argos Translate offline (paket argostranslate + model
    en->id) — hanya dipakai bila terpasang; workflow memasangnya
    sebagai fallback saat laporan mesin google memuat kegagalan.
  - "auto" (default): google dulu; per item jatuh ke argos bila
    google gagal dan argos tersedia.

Cache: data/translations.jsonl, satu baris per artikel:
{hash, link, content_hash, title, original_title, description,
content, chars_original, chars_translated, engine, translated_at}.
Kunci = url_hash(tautan); content_hash = sha256(isi asli)[:16] —
isi berubah -> terjemahan lama dianggap basi dan diterjemahkan ulang.
rollup_news.py menempelkan hasil cache ini ke keluaran.

Batas: MAX_PER_RUN item per run (terbaru dulu) agar run Actions
tidak超时. Hanya stdlib.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
ARTICLES_FILE = os.path.join(DATA, "articles.jsonl")
FULLTEXT_FILE = os.path.join(DATA, "fulltext.jsonl")
TRANS_FILE = os.path.join(DATA, "translations.jsonl")
REPORT_FILE = os.path.join(DATA, "translate_report.json")
AUTO_FILE = os.path.join(DATA, "sources_auto.json")
MAX_PER_RUN = 10
MAX_CACHE = 500
CHUNK_LIMIT = 3800
SLEEP_BETWEEN = 0.6     # jeda antar permintaan (hormati layanan)
GTX_URL = ("https://translate.googleapis.com/translate_a/single"
           "?client=gtx&sl=en&tl=id&dt=t")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

sys.path.insert(0, BASE)
from common import url_hash  # noqa: E402
import news_lib  # noqa: E402


def now_iso():
    return datetime.now(WIB).isoformat(timespec="seconds")


def content_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Mesin google (endpoint gtx, tanpa API key)
# ---------------------------------------------------------------------------
def _gtx_curl(text, timeout=45):
    """POST via curl: q dibaca dari berkas temp (--data-urlencode q@f)."""
    fd, path = tempfile.mkstemp(prefix="gtx_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        out = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-A", UA,
             "--data-urlencode", f"q@{path}", GTX_URL],
            capture_output=True, timeout=timeout + 10)
        if out.returncode != 0 or not out.stdout:
            raise IOError(f"curl gtx rc={out.returncode}")
        return out.stdout
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _gtx_urllib(text, timeout=45):
    data = urllib.parse.urlencode({"q": text}).encode("utf-8")
    req = urllib.request.Request(GTX_URL, data=data,
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def gtx_chunk(text, retries=3):
    """Terjemahkan SATU potongan (<= CHUNK_LIMIT) via endpoint gtx."""
    last_err = None
    for attempt in range(retries):
        for impl in (_gtx_curl, _gtx_urllib):
            try:
                raw = impl(text)
                resp = json.loads(raw.decode("utf-8", "replace"))
                out = "".join(seg[0] for seg in resp[0]
                              if seg and seg[0])
                if out.strip():
                    return out
                raise IOError("respons gtx kosong")
            except Exception as e:  # coba impl/retry berikutnya
                last_err = e
        time.sleep(2 * (attempt + 1))
    raise IOError(f"gtx gagal setelah {retries} retry: {last_err}")


# ---------------------------------------------------------------------------
# Mesin argos (offline, fallback — hanya bila paket terpasang)
# ---------------------------------------------------------------------------
_argos_ok = None


def argos_available():
    global _argos_ok
    if _argos_ok is None:
        try:
            import argostranslate.translate  # noqa: F401
            _argos_ok = True
        except Exception:
            _argos_ok = False
    return _argos_ok


def argos_chunk(text):
    import argostranslate.translate as at
    out = at.translate(text, "en", "id")
    if not out or not out.strip():
        raise IOError("respons argos kosong")
    return out


# ---------------------------------------------------------------------------
# Pemecahan & penggabungan teks
# ---------------------------------------------------------------------------
def _hard_split(text, limit):
    """Pecah satu paragraf raksasa di batas kalimat, lalu batas kata."""
    parts, cur = [], ""
    for sent in text.replace(". ", ".\x00").split("\x00"):
        if len(sent) > limit:
            for word in sent.split(" "):
                cand = (cur + " " + word) if cur else word
                if len(cand) <= limit:
                    cur = cand
                else:
                    if cur:
                        parts.append(cur)
                    cur = word
            continue
        cand = (cur + " " + sent) if cur else sent
        if len(cand) <= limit:
            cur = cand
        else:
            if cur:
                parts.append(cur)
            cur = sent
    if cur:
        parts.append(cur)
    return parts


def chunk_text(text, limit=CHUNK_LIMIT):
    """Pecah teks per paragraf menjadi potongan <= limit karakter.

    Return list (potongan, pemisah_sebelumnya): pemisah "\\n\\n" untuk
    batas antar-paragraf, " " untuk sambungan di dalam satu paragraf
    raksasa (hard split) — supaya teks yang digabung kembali utuh
    mengikuti struktur aslinya.
    """
    chunks, cur = [], ""
    for para in (text or "").split("\n\n"):
        para = para.strip("\n")
        if not para.strip():
            continue
        if len(para) > limit:
            if cur:
                chunks.append((cur, "\n\n"))
                cur = ""
            for i, piece in enumerate(_hard_split(para, limit)):
                chunks.append((piece, "\n\n" if i == 0 else " "))
            continue
        cand = (cur + "\n\n" + para) if cur else para
        if len(cand) <= limit:
            cur = cand
        else:
            chunks.append((cur, "\n\n"))
            cur = para
    if cur:
        chunks.append((cur, "\n\n"))
    return chunks


def translate_text(text, engine):
    """Terjemahkan teks utuh (dipecah & digabung kembali).

    engine: 'google' | 'argos'. Mengembalikan (teks_terjemahan, engine).
    Melempar IOError bila ada potongan yang gagal / hasil tak masuk akal.
    """
    fn = gtx_chunk if engine == "google" else argos_chunk
    chunks = chunk_text(text)
    if not chunks:
        return "", engine
    out = ""
    for i, (ch, sep) in enumerate(chunks):
        res = fn(ch)
        # Pemeriksaan kewajaran: terjemahan ID tidak pernah menyusut
        # sampai < 30% dari aslinya — di bawah itu anggap gagal.
        if len(res) < 0.3 * len(ch):
            raise IOError(
                f"hasil terjemahan mencurigakan ({len(res)} vs {len(ch)})")
        out = (out + sep + res) if out else res
        if i < len(chunks) - 1:
            time.sleep(SLEEP_BETWEEN)
    return out, engine


def translate_item(title, desc, content, mode):
    """Terjemahkan satu item utuh. Return (dict_hasil, engine).

    Melempar Exception bila gagal di mesin mana pun yang dicoba —
    pemanggil menahan item (tidak menulis cache).
    """
    engines = []
    if mode in ("auto", "google"):
        engines.append("google")
    if mode in ("auto", "argos") and argos_available():
        engines.append("argos")
    if mode == "argos" and not argos_available():
        raise IOError("argos diminta tapi paket argostranslate tidak ada")
    last_err = None
    for engine in engines:
        try:
            t_title, _ = translate_text(title, engine)
            time.sleep(SLEEP_BETWEEN)
            t_desc = ""
            if desc:
                t_desc, _ = translate_text(desc, engine)
                time.sleep(SLEEP_BETWEEN)
            t_content, _ = translate_text(content, engine)
            return ({"title": t_title, "description": t_desc,
                     "content": t_content}, engine)
        except Exception as e:
            last_err = e
            print(f"[translate] mesin {engine} gagal: {e}", flush=True)
            continue
    raise IOError(f"semua mesin gagal: {last_err}")


# ---------------------------------------------------------------------------
# Pemuatan data
# ---------------------------------------------------------------------------
def load_jsonl(path):
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def foreign_source_names():
    names = set()
    if os.path.exists(AUTO_FILE):
        try:
            with open(AUTO_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for s in data.get("sources", []):
                if s.get("translate"):
                    names.add(s.get("name"))
        except (json.JSONDecodeError, OSError):
            pass
    return names


def main():
    mode = "auto"
    if "--engine" in sys.argv:
        mode = sys.argv[sys.argv.index("--engine") + 1]

    articles = load_jsonl(ARTICLES_FILE)
    fulltext = {r["hash"]: r for r in load_jsonl(FULLTEXT_FILE)
                if r.get("hash")}
    cache = {r["hash"]: r for r in load_jsonl(TRANS_FILE)
             if r.get("hash")}
    foreign = foreign_source_names()

    candidates, held_partial = [], 0
    for a in articles:
        if a.get("source") not in foreign and a.get("source_lang") != "en":
            continue
        h = url_hash(a.get("link", ""))
        row = fulltext.get(h)
        if not row or row.get("content_status") != "full":
            held_partial += 1
            continue
        content = row.get("content") or ""
        if not content:
            held_partial += 1
            continue
        chash = content_hash(content)
        old = cache.get(h)
        if old and old.get("content_hash") == chash:
            continue  # sudah diterjemahkan untuk isi persis ini
        candidates.append((h, a, content, chash))
    # Terbaru dulu (fetched_at terbesar) — sama seperti enrich.py.
    candidates.sort(key=lambda x: str(x[1].get("fetched_at") or ""),
                    reverse=True)
    print(f"[translate] mode={mode}; kandidat {len(candidates)}; "
          f"ditahan (belum full) {held_partial}; cache {len(cache)}; "
          f"batas run {MAX_PER_RUN}", flush=True)

    translated, failed = [], []
    for h, a, content, chash in candidates[:MAX_PER_RUN]:
        raw_desc = (a.get("description") or "").strip()
        desc = (news_lib.html_to_text(raw_desc) if "<" in raw_desc
                else raw_desc)[:500]
        # Deskripsi feed bisa RUSAK sebagai ringkasan: mis. feed Geeky
        # Gadgets yang description-nya diawali tag <img> raksasa lalu
        # terpotong 500 char di tengah tag (tak ada teks sama sekali,
        # html_to_text tak bisa membuang tag tak tertutup). Bila hasil
        # bersih masih mengandung tag / terlalu pendek, ambil ringkasan
        # dari ISI artikel (paragraf pembuka — teks bersih dari enrich).
        if "<" in desc or len(desc) < 40:
            desc = content[:500]
        try:
            res, engine = translate_item(a.get("title", ""), desc,
                                         content, mode)
        except Exception as e:
            failed.append({"hash": h, "link": a.get("link", ""),
                           "reason": str(e)[:300]})
            print(f"[translate] TAHAN {a.get('link')}: {e}", flush=True)
            continue
        cache[h] = {
            "hash": h,
            "link": a.get("link", ""),
            "content_hash": chash,
            "title": res["title"],
            "original_title": a.get("title", ""),
            "description": res["description"],
            "content": res["content"],
            "chars_original": len(content),
            "chars_translated": len(res["content"]),
            "engine": engine,
            "translated_at": now_iso(),
        }
        translated.append(h)
        print(f"[translate] OK [{engine}] {len(content)} -> "
              f"{len(res['content'])} char: {res['title'][:60]}",
              flush=True)

    # Tulis ulang cache mengikuti urutan articles.jsonl, batasi MAX_CACHE.
    order = {url_hash(a.get("link", "")): i
             for i, a in enumerate(articles)}
    rows = sorted(cache.values(),
                  key=lambda r: order.get(r["hash"], -1))[-MAX_CACHE:]
    with open(TRANS_FILE, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    report = {"updated_at": now_iso(), "mode": mode,
              "translated": translated, "failed": failed,
              "held_partial": held_partial,
              "candidates": len(candidates)}
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"[translate] SELESAI: {len(translated)} diterjemahkan, "
          f"{len(failed)} ditahan-gagal, cache {len(rows)} baris")


if __name__ == "__main__":
    main()
