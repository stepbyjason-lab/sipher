r"""
sipher-instagram CLI.

  python -m adapters.instagram.cli fetch <URL> [--media-dir DIR] [--download]
                                                [--session-file PATH] [--comments]

fetch : 단일 Instagram 포스트/릴스 URL → 정규화 JSON(stdout)

옵션:
  --media-dir DIR     미디어 다운로드 대상 디렉토리(기본 "downloads", --download와 함께 사용)
  --download          대표 이미지/영상 1건을 media_dir에 다운로드
  --session-file PATH instaloader 로그인 세션 파일 경로(opt-in — 미지정 시 익명 접근)
  --comments          댓글 수집 시도(익명에서 막히면 meta.comments_label="login_required")
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import InstagramAccessError, fetch

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sipher-instagram")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="단일 포스트 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--media-dir", default=None, help="다운로드 대상 디렉토리(기본 downloads)")
    pf.add_argument("--download", action="store_true", help="대표 이미지/영상 다운로드")
    pf.add_argument("--session-file", default=None, dest="session_file",
                    help="instaloader 로그인 세션 파일(opt-in, 미지정 시 익명)")
    pf.add_argument("--comments", action="store_true", help="댓글 수집 시도")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        result = fetch(
            args.url, media_dir=args.media_dir, download=args.download,
            session_file=args.session_file, comments=args.comments,
        )
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130
    except InstagramAccessError as e:
        print(f"오류[access_label={e.access_label}]: {e}", file=sys.stderr)
        return 1
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
