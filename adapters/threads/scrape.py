"""Tiered Threads scraper: a fast single-page snapshot first, escalating to the
deep recursive crawl only when the fast pass looks incomplete.

Why: on simple threads the fast pass (~10s) returns the same posts as the deep
crawl (which can take 5-15min) — so deep is pure waste there. On large/nested
threads the deep crawl finds many more replies. This dispatcher picks per-post
using a data signal instead of guesswork.

Usage:
    python scrape.py <url> [out.json] [--download] [--auto] [--deep]

    --deep      skip the fast pass, go straight to the deep recursive crawl
    --auto      if the fast pass looks incomplete, run the deep crawl automatically
    --download  download media into downloads/<author>_<code>/ (final result only)

Without --auto, an incomplete fast result just prints a recommendation.
"""
import asyncio
import json
import re
import sys

from . import fast_scrape
from .threads_scraper_v2 import scrape_threads_recursive
from .media_utils import download_media


def _root_code(url: str) -> str:
    m = re.search(r"/post/([^/?]+)", url)
    return m.group(1) if m else None


def assess(posts, url):
    """Heuristic completeness check on a fast-pass result.

    The target post's reply_count is Threads' own tally of the discussion size;
    if the fast pass captured noticeably fewer replies than that, deep nested
    sub-threads were almost certainly missed.
    """
    code = _root_code(url)
    root = next((p for p in posts if p.get("code") == code), None)
    replies = [p for p in posts if p.get("code") != code]
    expected = (root or {}).get("reply_count", 0)
    captured = len(replies)
    incomplete = root is not None and captured < expected
    return {"root_found": root is not None, "expected": expected,
            "captured": captured, "incomplete": incomplete}


async def run_deep(url, out, do_download):
    posts = await scrape_threads_recursive(url, max_pages=100)
    return posts


async def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}

    if not args:
        print("Usage: python scrape.py <url> [out.json] [--download] [--auto] [--deep]")
        sys.exit(1)

    url = args[0]
    out = args[1] if len(args) > 1 else "out.json"
    do_download = "--download" in flags

    if "--deep" in flags:
        posts = await scrape_threads_recursive(url, max_pages=100)
    else:
        posts = await fast_scrape.scrape(url, out, do_download=False)
        a = assess(posts, url)
        sys.stderr.write(
            f"[fast] {len(posts)} posts | replies {a['captured']} of ~{a['expected']} "
            f"(per root reply_count)\n"
        )
        if a["incomplete"]:
            if "--auto" in flags:
                sys.stderr.write("[fast] looks incomplete -> escalating to deep crawl...\n")
                posts = await scrape_threads_recursive(url, max_pages=100)
            else:
                sys.stderr.write(
                    f"[fast] INCOMPLETE: got {a['captured']} of ~{a['expected']} replies. "
                    f"Re-run with --deep (or add --auto) for the full nested crawl.\n"
                )
        else:
            sys.stderr.write("[fast] looks complete -> deep crawl not needed.\n")

    if do_download:
        n = download_media(posts, out_dir="downloads")
        sys.stderr.write(f"[download] {n} file(s) -> ./downloads/\n")

    with open(out, "w", encoding="utf-8") as f:
        json.dump(posts, f, indent=2, ensure_ascii=False)
    sys.stderr.write(f"[done] {len(posts)} posts -> {out}\n")


if __name__ == "__main__":
    asyncio.run(main())
