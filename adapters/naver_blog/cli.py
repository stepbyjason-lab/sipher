r"""
sipher-naver-blog CLI.

  python -m adapters.naver_blog.cli fetch <포스트URL> [--media-dir DIR]
  python -m adapters.naver_blog.cli scrape <blog_id> [--media-dir DIR] [--max N]

fetch  : 단일 포스트 → 정규화 JSON(stdout)
scrape : 블로그 전체(또는 --max) → 정규화 JSON 배열(stdout)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import fetch, normalize
from .scrape import scrape_blog

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sipher-naver-blog")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="단일 포스트 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--media-dir", default=None, help="지정 시 미디어 다운로드")

    ps = sub.add_parser("scrape", help="블로그 전체 → 정규화 JSON 배열")
    ps.add_argument("blog_id")
    ps.add_argument("--media-dir", default=None)
    ps.add_argument("--max", type=int, default=10000, dest="max_posts")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # 본문/댓글에 비-BMP 문자(이모지 등)가 섞여도 stdout 출력이 콘솔 기본
    # codepage(Windows cp949 등)에서 깨지지 않도록 UTF-8 고정.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        if args.cmd == "fetch":
            result = fetch(args.url, media_dir=args.media_dir)
        else:  # scrape
            posts = scrape_blog(args.blog_id, max_posts=args.max_posts,
                                media_dir=args.media_dir)
            result = []  # 한 건 normalize 실패가 전체 출력을 버리지 않게 per-item 처리
            for p in posts:
                try:
                    result.append(normalize(p, source=p["url"]))
                except Exception as e:
                    _log.warning("normalize 실패 logNo=%s — %s",
                                 p.get("log_no"), type(e).__name__)
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
