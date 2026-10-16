# Hozin News Getter

Auto-polling berita Indonesia dari RSS, jalan sekuensial tiap 10 menit via GitHub Actions.

## Cara kerja (rantai sekuensial)

Setiap run terjadwal:

1. **Getter 1** jalan dulu → `detik.com`, `JawaPos`, `CNN Indonesia`
2. Selesai → **Getter 2** jalan → `Liputan6`, `Antara News`
3. Hasil di-commit balik ke `data/`

Nggak ada yang jalan bareng:
- `concurrency.group` bikin run baru **nunggu** kalau run sebelumnya belum selesai
- `seen.json` dishare kedua getter → artikel yang udah diambil Getter 1 otomatis di-skip Getter 2 (nol duplikat)

Repo ini **publik** supaya GitHub Actions minutes-nya unlimited.

## Data

- `data/articles.jsonl` — 1 JSON per baris: `source`, `getter`, `title`, `link`, `pubDate`, `description`, `fetched_at`
- `data/seen.json` — hash URL yang udah diambil (dibatasi 5000 terakhir)

## Tambah sumber

Edit `SOURCES` di `getter1.py` / `getter2.py`. Format: `("nama_sumber", "https://...rss")`.
Pakai stdlib Python aja (tanpa pip install) biar run CI cepat.
