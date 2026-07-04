r"""
sipher-tiktok CLI.

  python -m adapters.tiktok.cli fetch <URL> [--media-dir DIR] [--download]

fetch : 단일 TikTok 영상 URL → 정규화 JSON(stdout)

옵션:
  --media-dir DIR   미디어 다운로드 대상 디렉토리(기본 "downloads", --download와 함께 사용)
  --download        영상 파일을 media_dir에 다운로드(메타만 필요하면 생략)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import fetch

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sipher-tiktok")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="단일 영상 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--media-dir", default=None, help="다운로드 대상 디렉토리(기본 downloads)")
    pf.add_argument("--download", action="store_true", help="영상 파일 다운로드")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        result = fetch(args.url, media_dir=args.media_dir, download=args.download)
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
