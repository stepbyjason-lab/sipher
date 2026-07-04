r"""
sipher-fb CLI.

  python -m adapters.facebook.cli login   --profile-dir DIR
  python -m adapters.facebook.cli fetch    <포스트URL>  [--media-dir DIR] [auth opts]
  python -m adapters.facebook.cli scrape   <프로필/페이지> [--media-dir DIR] [--max N] [--no-enrich] [auth opts]

login  : persistent 프로필에 1회 로그인(사용자 직접 — Gate 5). 이후 fetch/scrape가 재사용.
fetch  : 단일 포스트 → 정규화 JSON(stdout)
scrape : 프로필/페이지 타임라인 → 정규화 JSON 배열(stdout)

auth opts(공통): --mode {persistent,browser,cookies_txt} --profile-dir DIR
                 --cookies PATH --browser NAME --headful
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import auth, fetch, scrape_profile_normalized

_log = logging.getLogger(__name__)


def _add_auth_opts(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--mode", default="persistent",
                    choices=["persistent", "browser", "cookies_txt"],
                    help="인증 방식(기본 persistent 프로필)")
    ap.add_argument("--profile-dir", default=None, help="persistent 프로필 디렉토리")
    ap.add_argument("--cookies", default=None, help="cookies_txt 모드 Netscape 쿠키 경로")
    ap.add_argument("--browser", default="firefox", help="browser 모드 소스 브라우저")
    ap.add_argument("--headful", action="store_true", help="브라우저 창 표시(디버그)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sipher-fb")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug 로그")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="persistent 프로필 로그인(사용자 1회)")
    pl.add_argument("--profile-dir", required=True)
    pl.add_argument("--timeout", type=int, default=300, dest="timeout_sec")

    pf = sub.add_parser("fetch", help="단일 포스트 URL → 정규화 JSON")
    pf.add_argument("url")
    pf.add_argument("--media-dir", default=None, help="지정 시 미디어 다운로드")
    pf.add_argument("--no-video", action="store_true", help="영상 보강 생략(빠름)")
    pf.add_argument("--deep", action="store_true",
                    help="앨범 set 전체 확장(포스트 밖 사진까지 — 과수집 위험, 기본 off)")
    pf.add_argument("--comments", action="store_true",
                    help="댓글 본문 보강(round-14, 옵트인 — 추가 DOM 왕복, 기본 off)")
    _add_auth_opts(pf)

    ps = sub.add_parser("scrape", help="프로필/페이지 → 정규화 JSON 배열")
    ps.add_argument("target")
    ps.add_argument("--media-dir", default=None)
    ps.add_argument("--max", type=int, default=300, dest="max_scrolls")
    ps.add_argument("--no-enrich", action="store_true", help="풀사이즈/영상 재방문 보강 생략")
    ps.add_argument("--no-video", action="store_true", help="영상 보강 생략")
    ps.add_argument("--deep", action="store_true",
                    help="앨범 set 전체 확장(포스트 밖 사진까지 — 과수집 위험, 기본 off)")
    ps.add_argument("--comments", action="store_true",
                    help="댓글 본문 보강(round-14, 옵트인 — 각 포스트당 추가 DOM 왕복, 기본 off)")
    _add_auth_opts(ps)

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
        if args.cmd == "login":
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                auth.login(p, args.profile_dir, timeout_sec=args.timeout_sec)
            print("로그인 완료 — 이후 fetch/scrape가 이 프로필을 재사용합니다.", file=sys.stderr)
            return 0

        if args.cmd == "fetch":
            result = fetch(
                args.url, media_dir=args.media_dir, auth_mode=args.mode,
                profile_dir=args.profile_dir, cookies_path=args.cookies,
                browser_name=args.browser, headless=not args.headful,
                with_video=not args.no_video, deep=args.deep,
                comments=args.comments,
            )
        else:  # scrape
            result = scrape_profile_normalized(
                args.target, media_dir=args.media_dir, auth_mode=args.mode,
                profile_dir=args.profile_dir, cookies_path=args.cookies,
                browser_name=args.browser, headless=not args.headful,
                max_scrolls=args.max_scrolls, enrich=not args.no_enrich,
                with_video=not args.no_video, deep=args.deep,
                comments=args.comments,
            )
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130
    except auth.AuthError as e:
        print(f"인증 오류: {e}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
