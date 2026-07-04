r"""
sipher 코어 라우터 CLI — 단일 진입점.

  python -m core fetch <URL|로컬파일> [플랫폼별 옵션...]

fetch : 어떤 지원 플랫폼 URL이든, 또는 존재하는 로컬 파일 경로든 → 사람용
        Markdown(기본, stdout). 플랫폼을 host로 판별해 해당 어댑터로 위임하거나
        (core.fetch), 로컬 파일이면 core.local로 위임한다. 옵션은 플랫폼마다
        다르므로 라우터는 공통 옵션만 명시하고 나머지는 각 어댑터 전용 CLI를
        쓰도록 안내한다.

출력 옵션:
  (플래그 없음)     사람용 Markdown을 stdout에 출력(미디어 경로는 절대경로)
  --json            정규화 JSON을 stdout에 출력(기존과 바이트 동일, 하위 호환)
  --out FILE        Markdown(기본) 또는 --json과 함께면 JSON을 FILE에 저장.
                    stdout에는 저장 경로 한 줄만 출력. Markdown 저장 시 미디어
                    경로는 FILE 위치 기준 상대경로(크로스 드라이브면 절대경로).

공통 옵션:
  --media-dir DIR   미디어 다운로드 대상 디렉토리
  --download        이미지/영상 다운로드(threads/facebook 등 지원 어댑터)
  --deep            (threads) fast pass 생략, 재귀 크롤부터
  --auto            (threads) fast pass 불완전 시 자동 deep 승격
  --max-pages N     (threads) deep 크롤 최대 페이지 수
  --from-start      (youtube) 라이브를 처음부터
  --ocr             media_paths[] 이미지를 무료 비전 OCR(Gemini)로 인리치
                    (opt-in, 기본 off — 외부 API 호출 비용/프라이버시 때문)
  --transcribe      media_paths[] 오디오/영상을 로컬 whisper로 전사해 transcript 채움
                    (opt-in, 기본 off — subprocess 실행 비용/전사 소요 시간 때문.
                    로컬 영상/음성 파일 입력은 예외로 자동 적용된다)
  --whisper-model M (--transcribe와 함께) whisper 모델명(미지정 시 도구 기본값 large-v3)
  --whisper-device D (--transcribe와 함께) whisper 디바이스(미지정 시 도구 기본값 cuda)
  --whisper-compute C (--transcribe와 함께) whisper compute type(미지정 시 도구 기본값
                    float16). CPU 디바이스는 int8 필요, float16은 GPU 전용.

facebook 인증/옵션 (facebook):
  --profile-dir DIR       persistent 인증 모드의 브라우저 프로필 디렉토리
  --auth-mode MODE        persistent/browser/cookies_txt 중 하나(기본: persistent)
  --cookies-path PATH     cookies_txt 인증 모드의 쿠키 파일 경로
  --browser NAME          브라우저 엔진 이름(기본: firefox)
  --no-headless           headful로 브라우저 실행(기본: headless)
  --no-video              영상(T4) 보강 생략(기본: 영상 포함)

web 옵션 (범용 폴백, 6플랫폼 host 미매칭 http(s) URL 대상 — round-10):
  --js auto|true|false    auto(기본, SSR 껍데기 의심 시 자동 JS 렌더 승격)/
                          true(항상 JS 렌더 강제)/false(정적 tier1만)
  --timeout N             tier1 요청 타임아웃 초(기본 25)

플랫폼 전용 옵션 전체는 각 어댑터 CLI 참조(python -m adapters.<platform>.cli).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import SUPPORTED_PLATFORMS, detect_platform, fetch
from .render import render_markdown

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    # CLI help는 영어 우선 + 한국어 병기(round-21, 공개 대비).
    ap = argparse.ArgumentParser(prog="sipher")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="debug logging (debug 로그)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser(
        "fetch",
        help="URL/local file → human Markdown (default) or JSON; platform auto-detected "
             "(URL/로컬파일 → 사람용 Markdown(기본) 또는 JSON, 플랫폼 자동 판별)")
    pf.add_argument("url", help="URL or local file path (URL 또는 로컬 파일 경로)")
    pf.add_argument("--json", action="store_true", dest="json_output",
                     help="print normalized JSON to stdout (정규화 JSON을 stdout에 출력)")
    pf.add_argument("--out", default=None, dest="out_file",
                     help="write result to FILE — Markdown by default, JSON with --json; "
                          "stdout prints the saved path only "
                          "(결과를 FILE에 저장, stdout엔 저장 경로만 출력)")
    pf.add_argument("--media-dir", default=None, dest="media_dir",
                     help="directory for downloaded media (다운로드 미디어 저장 디렉토리)")
    pf.add_argument("--download", action="store_true",
                     help="(threads/tiktok) download media files (미디어 파일 다운로드)")
    pf.add_argument("--deep", action="store_true",
                     help="(threads) force deep crawl (deep 크롤 강제)")
    pf.add_argument("--auto", action="store_true",
                     help="(threads) fast pass, auto-escalate to deep when incomplete "
                          "(fast 후 불완전하면 deep 자동 승격)")
    pf.add_argument("--max-pages", type=int, default=None, dest="max_pages",
                     help="(threads) deep-crawl page cap (deep 크롤 페이지 상한)")
    pf.add_argument("--from-start", action="store_true", dest="from_start",
                     help="(youtube) capture a live stream from the beginning "
                          "(라이브를 처음부터)")
    pf.add_argument("--with-transcript", action="store_true", dest="with_transcript",
                     help="(youtube) fill transcript from native subtitles — optional pip "
                          "youtube-transcript-api, skipped if missing; separate from whisper "
                          "--transcribe (네이티브 자막을 transcript로, 미설치 시 skip)")
    pf.add_argument("--sub-langs", default=None, dest="sub_langs",
                     help="(youtube) subtitle language priority CSV, default ko,en "
                          "(자막 언어 우선순위 CSV)")
    pf.add_argument("--ocr", action="store_true",
                     help="enrich media_paths[] images via Gemini OCR "
                          "(media_paths[] 이미지를 Gemini OCR로 인리치)")
    pf.add_argument("--transcribe", action="store_true",
                     help="transcribe media_paths[] audio/video with local whisper "
                          "(오디오/영상을 로컬 whisper로 전사)")
    pf.add_argument("--whisper-model", default=None, dest="whisper_model",
                     help="(--transcribe) whisper model, tool default large-v3 "
                          "(whisper 모델명, 미지정 시 도구 기본값)")
    pf.add_argument("--whisper-device", default=None, dest="whisper_device",
                     help="(--transcribe) whisper device, tool default cuda "
                          "(whisper 디바이스)")
    pf.add_argument("--whisper-compute", default=None, dest="whisper_compute",
                     help="(--transcribe) whisper compute type, tool default float16 — "
                          "CPU needs int8 (compute type, CPU는 int8 필요)")
    pf.add_argument("--profile-dir", default=None, dest="profile_dir",
                     help="(facebook) browser profile dir for persistent auth "
                          "(persistent 인증 모드의 브라우저 프로필 디렉토리)")
    pf.add_argument("--auth-mode", default=None, dest="auth_mode",
                     choices=["persistent", "browser", "cookies_txt"],
                     help="(facebook) auth mode, default persistent (인증 모드)")
    pf.add_argument("--cookies-path", default=None, dest="cookies_path",
                     help="(facebook) cookie file for cookies_txt mode "
                          "(cookies_txt 인증 모드의 쿠키 파일 경로)")
    pf.add_argument("--browser", default=None, dest="browser_name",
                     help="(facebook) browser engine, default firefox (브라우저 엔진 이름)")
    pf.add_argument("--no-headless", action="store_false", dest="headless", default=None,
                     help="(facebook) run the browser headful, default headless "
                          "(headful로 브라우저 실행)")
    pf.add_argument("--no-video", action="store_false", dest="with_video", default=None,
                     help="(facebook) skip video enrichment, default on "
                          "(영상 보강 생략, 기본은 포함)")
    pf.add_argument("--js", default=None, choices=["auto", "true", "false"], dest="js_mode",
                     help="(web) auto = escalate to JS render when the page looks like an "
                          "SSR shell (default) / true = force render / false = static tier1 "
                          "only (auto=SSR 껍데기 의심 시 자동 승격/true=강제 렌더/false=정적만)")
    pf.add_argument("--timeout", type=int, default=None, dest="web_timeout",
                     help="(web) tier1 request timeout in seconds, default 25 "
                          "(tier1 요청 타임아웃 초)")

    pd = sub.add_parser("detect",
                        help="URL → print platform name only, no fetching "
                             "(플랫폼 이름만 출력, 위임 없음)")
    pd.add_argument("url", help="URL to classify (판별할 URL)")

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.cmd == "detect":
        try:
            print(detect_platform(args.url))
        except ValueError as e:
            print(f"오류: {e}", file=sys.stderr)
            return 1
        return 0

    # 라우터는 어댑터가 실제로 받는 옵션만 추려서 넘긴다 — None/False 기본값을
    # 그대로 전달하면 옵션 미지원 어댑터(youtube에 --deep 등)가 TypeError를
    # 내므로, 사용자가 명시한 것만 kwargs로 구성한다.
    kwargs: dict = {}
    if args.media_dir is not None:
        kwargs["media_dir"] = args.media_dir
    if args.download:
        kwargs["download"] = True
    if args.deep:
        kwargs["deep"] = True
    if args.auto:
        kwargs["auto"] = True
    if args.max_pages is not None:
        kwargs["max_pages"] = args.max_pages
    if args.from_start:
        kwargs["from_start"] = True
    if args.with_transcript:
        kwargs["with_transcript"] = True
    if args.sub_langs is not None:
        kwargs["sub_langs"] = args.sub_langs
    if args.profile_dir is not None:
        kwargs["profile_dir"] = args.profile_dir
    if args.auth_mode is not None:
        kwargs["auth_mode"] = args.auth_mode
    if args.cookies_path is not None:
        kwargs["cookies_path"] = args.cookies_path
    if args.browser_name is not None:
        kwargs["browser_name"] = args.browser_name
    if args.headless is not None:
        kwargs["headless"] = args.headless
    if args.with_video is not None:
        kwargs["with_video"] = args.with_video
    if args.js_mode is not None:
        kwargs["js"] = {"auto": "auto", "true": True, "false": False}[args.js_mode]
    if args.web_timeout is not None:
        kwargs["timeout"] = args.web_timeout

    try:
        result = fetch(
            args.url,
            ocr=args.ocr,
            transcribe=args.transcribe,
            whisper_model=args.whisper_model,
            whisper_device=args.whisper_device,
            whisper_compute=args.whisper_compute,
            **kwargs,
        )
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except TypeError as e:
        # 옵션이 해당 플랫폼 어댑터 계약에 없음.
        platform = None
        try:
            platform = detect_platform(args.url)
        except ValueError:
            pass
        hint = f" — {platform} 어댑터가 받지 않는 옵션일 수 있음" if platform else ""
        print(f"오류: 잘못된 옵션{hint}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"오류[{type(e).__name__}]: {e}", file=sys.stderr)
        return 1

    try:
        if args.out_file:
            out_path = Path(args.out_file)
            if args.json_output:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                    f.write("\n")
            else:
                md = render_markdown(
                    result, path_mode="relative", relative_to=out_path.resolve().parent
                )
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(md)
            print(f"저장됨: {os.path.abspath(out_path)}")
            return 0

        if args.json_output:
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(render_markdown(result, path_mode="absolute"))
        return 0
    except Exception as e:
        print(f"오류[{type(e).__name__}]: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
