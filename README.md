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

## Sumber otomatis (penemuan mandiri)

Selain sumber tetap di atas, pipa ini bisa menemukan sumbernya sendiri:

- `discover.py` — jalan **mingguan** (Senin 03:17 UTC, workflow
  `source-discovery.yml`). Berburu feed dari domain di
  `data/seed_domains.txt` + domain sumber artikel Google News RSS,
  memvalidasi ketat (feed ter-parse, >= 3 item, terbaru <= 7 hari,
  heuristik Bahasa Indonesia), lalu menulis `data/sources_auto.json`.
  Sumber baru berstatus `probation`; yang gagal 3x atau mati > 14
  hari di-drop otomatis. Maks 30 sumber, maks 10 baru per run.
- `getter_auto.py` — jalan setelah rantai getter utama selesai
  (workflow `getter-auto.yml`), mengambil artikel dari sumber
  otomatis (maks 10/sumber) ke `articles.jsonl` dengan
  `getter: "auto"`. Sumber `probation` yang sukses 3 run berturut
  naik jadi `active`.
