r"""
sipher-x CLI.

  python -m adapters.x.cli fetch <URL> [--download] [--media-dir DIR] [--max-replies N]

쿠키: `.env.local`(또는 환경변수)의 `X_COOKIES_FILE=<넷스케이프 쿠키 파일 경로>`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import DEFAULT_MAX_REPLIES, fetch


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sipher-x")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("fetch", help="X 포스트 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--media-dir", default=None, help="다운로드 대상 디렉토리(기본 downloads)")
    pf.add_argument("--download", action="store_true", help="영상·사진 원본 다운로드")
    pf.add_argument("--max-replies", type=int, default=DEFAULT_MAX_REPLIES,
                    help=f"타인 답글 상한(기본 {DEFAULT_MAX_REPLIES}, 저자 글은 자르지 않음)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = fetch(args.url, media_dir=args.media_dir, download=args.download,
                       max_replies=args.max_replies)
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
