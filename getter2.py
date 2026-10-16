#!/usr/bin/env python3
"""Getter 2: Liputan6, Antara News.

Runs AFTER getter1 finishes (sequential chain in CI). Shares
seen.json/articles.jsonl with getter1.py via common.py, so anything
getter1 already grabbed is skipped here — no duplicates.
"""
from common import run_getter

SOURCES = [
    ("liputan6", "https://feed.liputan6.com/rss/news"),
    ("antara", "https://www.antaranews.com/rss/terkini"),
]

if __name__ == "__main__":
    n = run_getter("getter2", SOURCES)
    print(f"[getter2] done, {n} new articles")
