r"""
sipher-threads 어댑터 — 독립 도구(패키지). vendored(이식) 케이스.

공개 API: `fetch(url) -> 정규화 JSON dict` (sipher 라우터/단독 CLI 공용).
sipher 내부를 import 하지 않는 깨끗한 경계 → 나중에 `git subtree split`로 추출 가능.

정규화 스키마: { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }
설계: docs/00-overview.md. 참조 어댑터: adapters/naver_blog, adapters/youtube (구조·CLI·docs 패턴).

Threads의 고유값은 **중첩 댓글**이다 — naver_blog/facebook과 달리 `comments[]`를 실제로 채운다.
내부는 vendored 원본(`scrape.py` 티어 디스패처)을 그대로 호출한다 — 스크래퍼 로직은
이식 시 리팩터하지 않았다(출처: _SOURCE.md). playwright는 fetch 실행 시에만 필요
(parse_url/normalize는 playwright 없이 import·테스트 가능).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from . import scrape as _dispatcher
from .fast_scrape import COOKIE_FILE
from .media_utils import download_media

__all__ = ["fetch", "parse_url", "normalize"]

_log = logging.getLogger(__name__)

MediaLabel = Literal["none", "downloaded", "partially_downloaded", "download_failed"]

# threads.net / threads.com 만 허용(SSRF·인자 인젝션 방어) + @username/post/<code> 형식.
# code는 Threads 짧은 영숫자 식별자(관측: [A-Za-z0-9_-]) — youtube parse_url의
# "호스트 화이트리스트 + 정규식으로 안전한 식별자만 추출" 패턴을 그대로 따른다.
_HOST = re.compile(r"^(?:https?://)?(?:www\.)?(?:threads\.net|threads\.com)/", re.I)
_POST_PATH = re.compile(r"/@(?P<author>[A-Za-z0-9_.]+)/post/(?P<code>[A-Za-z0-9_-]+)", re.I)


def parse_url(url: str) -> tuple[str, str]:
    """Threads 포스트 URL → (author, code). 실패 시 ValueError.

    호스트를 threads.net/threads.com으로 제한(SSRF 방어)하고, @author/post/code
    경로만 통과시킨다. 비-threads URL이나 프로필/홈 URL(code 없음)은 거부한다.
    """
    if not isinstance(url, str):
        raise ValueError("URL은 문자열이어야 합니다")
    s = url.strip()
    if len(s) > 2048:
        raise ValueError("URL이 너무 깁니다")
    if not _HOST.match(s):
        raise ValueError(f"Threads URL이 아닙니다: {s.split('?', 1)[0]!r}")
    m = _POST_PATH.search(s)
    if not m:
        raise ValueError(f"포스트 code를 찾을 수 없습니다(예: /@user/post/ABC123): {s.split('?', 1)[0]!r}")
    author = m.group("author")
    if ".." in author or author.startswith(".") or author.endswith("."):
        raise ValueError(f"올바르지 않은 author입니다: {author!r}")
    return author, m.group("code")


def _media_counts(posts: list[dict]) -> tuple[int, int]:
    """posts 전체의 (전체 미디어 개수, 실제 다운로드된 개수)."""
    total = sum(len(p.get("images") or []) + len(p.get("videos") or []) for p in posts)
    got = sum(len(p.get("downloaded") or []) for p in posts)
    return total, got


def _media_label(posts: list[dict], *, downloaded: bool) -> MediaLabel:
    if not downloaded:
        return "none"
    total, got = _media_counts(posts)
    if total == 0:
        return "none"
    if got == 0:
        return "download_failed"
    if got < total:
        return "partially_downloaded"
    return "downloaded"


def fetch(url: str, *, media_dir: str | Path | None = None, deep: bool = False,
          auto: bool = False, download: bool = False, max_pages: int = 100) -> dict:
    """단일 Threads 포스트 URL → 정규화 JSON dict.

    - deep=False(기본): 빠른 단일 패스(fast_scrape). 완전성 휴리스틱은 vendored
      scrape.assess()가 stderr 로그로만 알려줌 — 어댑터 레벨에서 재계산해 meta에 싣는다.
    - deep=True: 처음부터 재귀 크롤(threads_scraper_v2, 최대 max_pages 페이지).
    - auto=True: fast pass가 불완전해 보이면 자동으로 deep 크롤 승격(vendored dispatcher 위임).
    - download=True: media_dir(기본 "downloads")에 이미지/영상 다운로드. CDN URL은
      서명·시간제한이라 스크랩 직후 받지 않으면 만료된다(원본 media_utils.py docstring 근거).

    보안 경고(trusted input): media_dir/max_pages는 로컬 사용자가 지정하는 신뢰 입력이다.
    이 함수는 경로 containment를 하지 않는다 — youtube 어댑터와 동일한 경계 원칙.
    """
    author, code = parse_url(url)
    out_dir = str(media_dir) if media_dir else "downloads"
    # 검증된 author/code로 canonical URL을 재구성해 스크래퍼에 넘긴다 — 원본 url을
    # playwright goto로 그대로 전달하지 않는다(youtube 어댑터의 인자 인젝션 방어 패턴과 동일).
    # query/fragment는 이 재구성으로 자연히 제거된다.
    canonical = f"https://www.threads.net/@{author}/post/{code}"

    posts = _run_scrape(canonical, deep=deep, auto=auto, max_pages=max_pages)
    assessment = _dispatcher.assess(posts, canonical)

    if not assessment.get("root_found"):
        raise RuntimeError(
            "threads: root 포스트를 찾지 못함 — 스크랩 실패 가능(네트워크/쿠키만료/차단/잘못된 URL)"
        )

    if download:
        download_media(posts, out_dir=out_dir)

    return normalize(posts, source=url, author=author, code=code,
                     assessment=assessment, downloaded=download,
                     deep=deep, max_pages=max_pages)


def _run_scrape(url: str, *, deep: bool, auto: bool, max_pages: int) -> list[dict]:
    """asyncio 이벤트 루프 기동 + vendored 티어 디스패처 호출을 감싸는 동기 경계.

    vendored fast_scrape.scrape()/scrape_threads_recursive()/assess()를 그대로
    위임한다(로직 미변경). deep 경로는 디스패처의 run_deep()이 max_pages=100을
    하드코딩하므로 우회해 scrape_threads_recursive()를 직접 호출한다 — CLI의
    --max-pages/fetch(max_pages=)가 실제 크롤에 반영되도록(meta 표기와 일치).
    fast_scrape.scrape()는 결과를 JSON 파일로도 쓰는 시그니처라 os.devnull을
    out 경로로 넘겨 어댑터 호출 시 잔여 파일을 남기지 않는다.
    """
    import asyncio
    import os

    if deep:
        return asyncio.run(_dispatcher.scrape_threads_recursive(url, max_pages=max_pages))

    async def _fast_then_maybe_deep() -> list[dict]:
        posts = await _dispatcher.fast_scrape.scrape(url, os.devnull, do_download=False)
        a = _dispatcher.assess(posts, url)
        if a["incomplete"] and auto:
            posts = await _dispatcher.scrape_threads_recursive(url, max_pages=max_pages)
        return posts

    return asyncio.run(_fast_then_maybe_deep())


def normalize(posts: list[dict], *, source: str, author: str, code: str,
              assessment: dict | None = None, downloaded: bool = False,
              deep: bool = False, max_pages: int | None = None) -> dict:
    """vendored 스크래퍼 결과(flat post list) → sipher 정규화 스키마. 공개 API.

    posts는 원 스레드 글(code == 요청 code) + 댓글(그 외)이 뒤섞인 flat list다
    (vendored 스크래퍼가 중첩 트리가 아니라 id→post map으로 수집하므로). root를
    분리해 body_text로, 나머지를 댓글 순서 보존 없이 comments[]로 채운다 —
    reply_count/likes 등 원본 메타는 각 댓글 dict에 그대로 보존해 상위 계층이
    필요 시 재구성할 수 있게 한다.
    """
    root = next((p for p in posts if p.get("code") == code), None)
    replies = [p for p in posts if p.get("code") != code]

    body_text = (root or {}).get("text") or ""
    root_media_paths = [*(root or {}).get("downloaded", [])] if root else []
    all_media_paths = []
    for p in posts:
        all_media_paths.extend(p.get("downloaded") or [])

    comments = [
        {
            "id": p.get("id"),
            "code": p.get("code"),
            "author": p.get("author"),
            "text": p.get("text") or "",
            "likes": p.get("likes", 0),
            "reply_count": p.get("reply_count", 0),
            "media_paths": p.get("downloaded") or [],
        }
        for p in replies
    ]

    assessment = dict(assessment or {})
    assessment["scrape_mode"] = "deep" if deep else "fast"
    if deep:
        assessment["max_pages"] = max_pages

    total_media, got_media = _media_counts(posts)

    return {
        "source": source,
        "platform": "threads",
        "body_text": body_text,
        "comments": comments,
        "ocr_text": [],          # sipher 정규화 단계(어댑터 밖)에서 채움
        "transcript": None,      # threads는 영상이어도 자체 전사 없음 — 다운스트림 whisper
        "media_paths": all_media_paths or root_media_paths,
        "meta": {
            "author": author,
            "code": code,
            "post_id": (root or {}).get("id"),
            "likes": (root or {}).get("likes", 0),
            "reply_count": (root or {}).get("reply_count", 0),
            "comment_count_captured": len(comments),
            "image_count": sum(len(p.get("images") or []) for p in posts),
            "video_count": sum(len(p.get("videos") or []) for p in posts),
            "media_label": _media_label(posts, downloaded=downloaded),
            "media_complete": got_media == total_media and total_media > 0,
            "cookies_available": os.path.exists(COOKIE_FILE),
            "completeness": assessment,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }
