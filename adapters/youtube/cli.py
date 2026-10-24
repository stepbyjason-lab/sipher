r"""
sipher-youtube CLI.

  python -m adapters.youtube.cli fetch <URL> [옵션]

fetch : 단일 YouTube 영상 URL → 정규화 JSON(stdout)

옵션:
  --media-dir DIR     지정해야 미디어/자막/채팅 다운로드(미지정=메타만)
  --from-start        라이브를 처음부터(--live-from-start)
  --no-video          미디어 다운로드 생략
  --no-subs           자막 파일 생략
  --sub-langs ko,en   자막/전사 언어
  --with-chat         라이브 채팅(live_chat.json) — --media-dir 필요
  --with-transcript   정제 전사(youtube-transcript-api)
  --with-comments     상위/고정 댓글(youtube-comment-downloader)
  --max-comments N    댓글 개수(기본 20)
  --timeout SEC       다운로드 상한(라이브 무한대기 방지)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import fetch
from .scrape import YtdlpError

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sipher-youtube")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="단일 영상 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--media-dir", default=None, help="지정 시 미디어/자막/채팅 다운로드")
    pf.add_argument("--from-start", action="store_true", help="라이브를 처음부터(--live-from-start)")
    pf.add_argument("--no-video", action="store_true", help="미디어 다운로드 생략")
    pf.add_argument("--no-subs", action="store_true", help="자막 파일 생략")
    pf.add_argument("--sub-langs", default="ko,en", help="자막/전사 언어(쉼표 구분)")
    pf.add_argument("--with-chat", action="store_true", help="라이브 채팅 replay(media-dir 필요)")
    pf.add_argument("--with-transcript", action="store_true", help="정제 전사(youtube-transcript-api)")
    pf.add_argument("--with-comments", action="store_true", help="상위/고정 댓글")
    pf.add_argument("--max-comments", type=int, default=20, help="댓글 개수(기본 20)")
    pf.add_argument("--timeout", type=int, default=None, help="다운로드 상한(초)")
    pf.add_argument("--sections", default=None,
                    help='시간 구간만 다운(yt-dlp --download-sections, 예 "*0-300"=0~5분, ffmpeg 필요)')

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # description/전사/댓글에 비-BMP 문자(이모지 등)가 섞여도 stdout 출력이
    # 콘솔 기본 codepage(Windows cp949 등)에서 깨지지 않도록 UTF-8 고정.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        result = fetch(
            args.url, media_dir=args.media_dir, from_start=args.from_start,
            with_video=not args.no_video, with_subs=not args.no_subs,
            sub_langs=args.sub_langs, with_chat=args.with_chat,
            with_transcript=args.with_transcript, with_comments=args.with_comments,
            max_comments=args.max_comments, timeout=args.timeout,
            sections=args.sections,
        )
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130
    except YtdlpError as e:
        print(f"yt-dlp 오류: {e}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
