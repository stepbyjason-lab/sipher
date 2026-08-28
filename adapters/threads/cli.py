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


def _write_progress(event: dict) -> None:
    """최종 JSON stdout 계약을 건드리지 않는 stderr JSONL progress sink."""
    print(json.dumps({"type": "progress", **event}, ensure_ascii=False), file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    # argparse가 --help를 출력하기 전에 UTF-8을 고정해야 Windows 기본 codepage에서도
    # 한국어 도움말과 이후 stderr progress 관찰이 깨지지 않는다.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="sipher-threads")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="단일 포스트 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--media-dir", default=None, help="다운로드 대상 디렉토리(기본 downloads)")
    pf.add_argument("--deep", action="store_true", help="fast pass 생략, 재귀 크롤부터")
    pf.add_argument("--auto", action="store_true", help="fast pass 불완전 시 자동 deep 승격")
    pf.add_argument("--all-comments", action="store_true", help="fast pass에서도 타인 댓글 포함 수집(기본: 저자 전용)")
    pf.add_argument("--download", action="store_true", help="이미지/영상 다운로드")
    pf.add_argument("--max-pages", type=int, default=100, dest="max_pages",
                    help="deep 크롤 최대 페이지 수(기본 100)")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        result = fetch(
            args.url, media_dir=args.media_dir, deep=args.deep, auto=args.auto, all_comments=args.all_comments,
            download=args.download, max_pages=args.max_pages, progress=_write_progress,
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
