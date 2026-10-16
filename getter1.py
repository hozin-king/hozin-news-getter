#!/usr/bin/env python3
"""Getter 1: detik.com, JawaPos, CNN Indonesia.

Runs first in the sequential chain. Shares seen.json/articles.jsonl
with getter2.py via common.py.
"""
from common import run_getter

SOURCES = [
    ("detik", "https://news.detik.com/googlenews/rss"),
    ("jawapos", "https://www.jawapos.com/rss.xml"),
    ("cnnindonesia", "https://www.cnnindonesia.com/nasional/rss"),
]

if __name__ == "__main__":
    n = run_getter("getter1", SOURCES)
    print(f"[getter1] done, {n} new articles")
