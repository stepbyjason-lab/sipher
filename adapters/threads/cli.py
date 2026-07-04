r"""
sipher-threads CLI.

  python -m adapters.threads.cli fetch <URL> [--media-dir DIR] [--deep] [--download]

fetch : 단일 Threads 포스트 URL → 정규화 JSON(stdout)

옵션:
  --media-dir DIR   미디어 다운로드 대상 디렉토리(기본 "downloads", --download와 함께 사용)
  --deep            fast pass 생략, 처음부터 재귀 크롤(threads_scraper_v2)
  --auto            fast pass가 불완전해 보이면 자동으로 deep 크롤 승격
  --download        이미지/영상을 media_dir에 다운로드(CDN URL 서명 만료 전에)
  --max-pages N     deep 크롤 최대 페이지 수(기본 100)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import fetch

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sipher-threads")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="단일 포스트 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--media-dir", default=None, help="다운로드 대상 디렉토리(기본 downloads)")
    pf.add_argument("--deep", action="store_true", help="fast pass 생략, 재귀 크롤부터")
    pf.add_argument("--auto", action="store_true", help="fast pass 불완전 시 자동 deep 승격")
    pf.add_argument("--download", action="store_true", help="이미지/영상 다운로드")
    pf.add_argument("--max-pages", type=int, default=100, dest="max_pages",
                    help="deep 크롤 최대 페이지 수(기본 100)")

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
        result = fetch(
            args.url, media_dir=args.media_dir, deep=args.deep, auto=args.auto,
            download=args.download, max_pages=args.max_pages,
        )
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"오류[{type(e).__name__}]: {e}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
