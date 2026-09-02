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
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urljoin, urlsplit

import requests

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
_SHARE_PATH = re.compile(r"^/share/(?P<code>[A-Za-z0-9_-]+)/?$", re.I)
_MAX_SHARE_REDIRECT_HOPS = 3
_SHARE_RESOLVE_TIMEOUT = 30
_AUTHOR_CONTINUATION_MAX_PAGES = 8
_AUTHOR_CONTINUATION_MAX_HOPS = 2
_AUTHOR_CONTINUATION_TIME_BUDGET_SECONDS = 45.0

ProgressCallback = Callable[[dict], None]


def _emit_progress(callback: ProgressCallback | None, event: str, *, started_at: float, **fields) -> None:
    """진행 관찰자에만 event를 전달한다. callback 오류는 수집을 실패시키지 않는다."""
    if callback is None:
        return
    payload = {"event": event, "elapsed_ms": int((time.monotonic() - started_at) * 1000), **fields}
    try:
        callback(payload)
    except Exception as exc:
        _log.debug("threads: progress callback 실패(무시): %s", type(exc).__name__)


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


def _is_share_url(url: str) -> bool:
    """URL의 query가 아니라 path 자체만 /share/<안전한 code>인지 확인한다."""
    absolute = url if "://" in url else f"https://{url}"
    return bool(_SHARE_PATH.fullmatch(urlsplit(absolute).path))


def _resolve_share_url(url: str) -> str:
    """Threads /share/ 단축 URL을 정식 URL로 해석한다.

    parse_url()은 playwright 없이 테스트 가능한 순수 함수로 유지한다. 네트워크가
    필요한 share 리다이렉트만 이 경계에 격리하고, 매 홉의 호스트를 재검증한다.
    """
    if not isinstance(url, str):
        return url
    s = url.strip()
    if len(s) > 2048:
        raise ValueError("URL이 너무 깁니다")
    if not _is_share_url(s):
        return url
    if not _HOST.match(s):
        raise ValueError(f"Threads URL이 아닙니다: {s.split('?', 1)[0]!r}")

    current = s if "://" in s else f"https://{s}"
    # round-38C: requests 자동 리다이렉트·off-host body fetch를 금지하고, Location을
    # 한 홉씩 검증한다. share 재귀는 3홉에서 종료해 loop/hang을 막는다.
    for _ in range(_MAX_SHARE_REDIRECT_HOPS):
        try:
            response = requests.get(
                current,
                allow_redirects=False,
                timeout=_SHARE_RESOLVE_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ValueError("Threads share URL을 해석할 수 없습니다") from exc

        # round-38C: 3xx 리다이렉트일 때만 Location을 신뢰한다 — 200 응답에 실린
        # Location 헤더를 리다이렉트로 오인해 정상 콘텐츠를 건너뛰지 않게 한다.
        location = response.headers.get("Location")
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            raise ValueError("Threads share URL 리다이렉트를 찾을 수 없습니다")
        target = urljoin(current, location)
        if not _HOST.match(target):
            raise ValueError(f"Threads URL이 아닙니다: {target.split('?', 1)[0]!r}")
        if not _is_share_url(target):
            return target
        current = target

    raise ValueError(f"Threads share URL 리다이렉트가 {_MAX_SHARE_REDIRECT_HOPS}홉을 초과했습니다")


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


def _post_key(post: dict) -> str | None:
    """원저자 continuation 병합용 안정 키. id가 없을 때 code로 degrade한다."""
    value = post.get("id") or post.get("code")
    return str(value) if value else None


def _has_snippet_attachment(post: dict) -> bool:
    return any(
        block.get("source") == "snippet_attachment" and block.get("text")
        for block in (post.get("text_blocks") or [])
        if isinstance(block, dict)
    )


def _resolve_author_continuations(
    posts: list[dict], *, author: str, root_code: str,
    progress: ProgressCallback | None = None, started_at: float | None = None,
) -> tuple[list[dict], dict]:
    """기본 fast 결과에서 공개된 원저자 후속글만 제한적으로 해소한다.

    전체 댓글 tree를 deep crawl하지 않는다. rich-text 전문이 아직 없는 원저자 post 중
    reply_count가 있는 것만 각 post URL로 fast 재조회하고, 그 결과에서도 원저자 post만
    병합한다. page/hop 상한은 결과 metadata로 정직하게 노출한다.
    """
    continuation_started_at = time.monotonic()
    started_at = started_at if started_at is not None else continuation_started_at
    deadline = continuation_started_at + _AUTHOR_CONTINUATION_TIME_BUDGET_SECONDS
    merged = list(posts)
    known = {_post_key(post) for post in merged if _post_key(post)}
    queue: list[tuple[str, int]] = []
    queued: set[str] = set()
    attempted_codes: set[str] = set()
    hop_limit_reached = False
    failed_pages: list[dict[str, str]] = []
    time_budget_exhausted = False

    def enqueue(post: dict, depth: int) -> None:
        nonlocal hop_limit_reached
        post_code = post.get("code")
        if (
            not post_code or post_code == root_code or post.get("author") != author
            or not post.get("reply_count") or _has_snippet_attachment(post)
            or post_code in attempted_codes or post_code in queued
        ):
            return
        if depth > _AUTHOR_CONTINUATION_MAX_HOPS:
            hop_limit_reached = True
            return
        queued.add(post_code)
        queue.append((post_code, depth))

    for post in merged:
        enqueue(post, 1)

    attempted_pages = 0
    discovered_posts = 0
    while queue and attempted_pages < _AUTHOR_CONTINUATION_MAX_PAGES:
        if time.monotonic() >= deadline:
            time_budget_exhausted = True
            break
        post_code, depth = queue.pop(0)
        attempted_codes.add(post_code)
        attempted_pages += 1
        child_url = f"https://www.threads.net/@{author}/post/{post_code}"
        _emit_progress(
            progress, "continuation_started", started_at=started_at,
            post_code=post_code, attempted_pages=attempted_pages,
            unresolved_candidates=len(queue),
        )
        try:
            fetched, actual_deep = _run_scrape(
                child_url, deep=False, auto=False, max_pages=100,
                progress=lambda event, **fields: _emit_progress(
                    progress, event, started_at=started_at, post_code=post_code, **fields,
                ),
            )
        except Exception as exc:  # 원 post는 이미 확보했으므로 후속 해소 실패는 fail-open metadata로 남긴다.
            _log.warning("threads: 원저자 후속글 해소 실패(%s): %s", post_code, type(exc).__name__)
            failed_pages.append({"code": post_code, "reason": type(exc).__name__})
            _emit_progress(progress, "continuation_complete", started_at=started_at,
                           post_code=post_code, result="failed")
            continue
        if actual_deep:  # 방어: 이 경로는 기본 fast-only 계약을 절대 깨지 않는다.
            _log.warning("threads: continuation resolver가 deep 결과를 반환해 무시함: %s", post_code)
            failed_pages.append({"code": post_code, "reason": "unexpected_deep_result"})
            _emit_progress(progress, "continuation_complete", started_at=started_at,
                           post_code=post_code, result="unexpected_deep")
            continue
        child_assessment = _dispatcher.assess(fetched, child_url)
        if not child_assessment.get("root_found"):
            failed_pages.append({"code": post_code, "reason": "root_not_found"})
            _emit_progress(progress, "continuation_complete", started_at=started_at,
                           post_code=post_code, result="root_not_found")
            continue
        for child in fetched:
            if child.get("author") != author:
                continue
            key = _post_key(child)
            if key and key not in known:
                known.add(key)
                merged.append(child)
                discovered_posts += 1
                enqueue(child, depth + 1)

        _emit_progress(
            progress, "continuation_complete", started_at=started_at,
            post_code=post_code, result="collected", discovered_posts=discovered_posts,
            unresolved_candidates=len(queue),
        )

    if attempted_pages and time.monotonic() >= deadline:
        time_budget_exhausted = True
    page_budget_exhausted = bool(queue and attempted_pages >= _AUTHOR_CONTINUATION_MAX_PAGES)
    partial_reasons = []
    if time_budget_exhausted:
        partial_reasons.append("time_budget_exhausted")
    if page_budget_exhausted:
        partial_reasons.append("page_budget_exhausted")
    if hop_limit_reached:
        partial_reasons.append("hop_limit_reached")
    if failed_pages:
        partial_reasons.append("continuation_failed")
    status = "partial" if partial_reasons else "complete"
    elapsed_ms = int((time.monotonic() - continuation_started_at) * 1000)
    if status == "partial":
        _emit_progress(
            progress, "continuation_partial", started_at=started_at,
            partial_reason=partial_reasons[0], partial_reasons=partial_reasons,
            unresolved_candidates=len(queue), elapsed_ms_continuation=elapsed_ms,
        )

    return merged, {
        "mode": "fast_author_continuation",
        "status": status,
        "partial_reason": partial_reasons[0] if partial_reasons else None,
        "partial_reasons": partial_reasons,
        "attempted_pages": attempted_pages,
        "discovered_posts": discovered_posts,
        "page_budget": _AUTHOR_CONTINUATION_MAX_PAGES,
        "hop_limit": _AUTHOR_CONTINUATION_MAX_HOPS,
        "page_budget_exhausted": page_budget_exhausted,
        "hop_limit_reached": hop_limit_reached,
        "unresolved_candidates": len(queue),
        "failed_pages": failed_pages,
        "time_budget_seconds": _AUTHOR_CONTINUATION_TIME_BUDGET_SECONDS,
        "time_budget_exhausted": time_budget_exhausted,
        "elapsed_ms": elapsed_ms,
    }


def fetch(url: str, *, media_dir: str | Path | None = None, deep: bool = False,
          auto: bool = False, all_comments: bool = False, download: bool = False,
          max_pages: int = 100, progress: ProgressCallback | None = None) -> dict:
    """단일 Threads 포스트 URL → 정규화 JSON dict.

    - 기본(deep=False, auto=False): 빠른 단일 패스(fast_scrape) + 저자 전용(author-only).
      원글과 원글 작성자의 연속 포스트만 수집하고 타인 댓글은 제외한다. 자동 deep 승격 없음.
    - auto=True: fast pass가 불완전해 보이면 자동으로 deep 크롤 승격(vendored dispatcher 위임).
    - deep=True: 처음부터 재귀 크롤(threads_scraper_v2, 최대 max_pages 페이지, 전체 댓글 수집).
    - all_comments=True: fast pass에서도 타인 댓글을 제외하지 않고 수집한다.
    - download=True: media_dir(기본 "downloads")에 이미지/영상 다운로드. CDN URL은
      서명·시간제한이라 스크랩 직후 받지 않으면 만료된다(원본 media_utils.py docstring 근거).
    - progress: 선택 callback. `share_resolved`, `fast_started`, `fast_waiting`,
      `fast_complete`, `continuation_started`, `continuation_complete`,
      `continuation_partial`, `collection_complete` event dict를 받는다. callback 예외는
      수집 결과를 바꾸지 않고 무시한다.

    root post를 찾은 뒤 continuation 시간 예산이 소진되면 예외 대신 현재 확보분을 반환한다.
    이 정상 부분 결과는 `meta.author_thread.resolution.status == "partial"`이며,
    `partial_reason`/`partial_reasons`, `unresolved_candidates`, `elapsed_ms`를 함께
    확인한다. root post 자체를 찾지 못한 경우는 partial이 아니라 RuntimeError다.

    보안 경고(trusted input): media_dir/max_pages는 로컬 사용자가 지정하는 신뢰 입력이다.
    이 함수는 경로 containment를 하지 않는다 — youtube 어댑터와 동일한 경계 원칙.
    """
    # round-38C: share URL만 별도 네트워크 경계에서 해석한 뒤, parse_url()을 최종
    # 화이트리스트·post path 게이트로 다시 통과시킨다.
    started_at = time.monotonic()
    original_url = url
    url = _resolve_share_url(url)
    author, code = parse_url(url)
    _emit_progress(progress, "share_resolved", started_at=started_at,
                   share_url=original_url if _is_share_url(original_url) else None,
                   author=author, post_code=code)
    out_dir = str(media_dir) if media_dir else "downloads"
    # 검증된 author/code로 canonical URL을 재구성해 스크래퍼에 넘긴다 — 원본 url을
    # playwright goto로 그대로 전달하지 않는다(youtube 어댑터의 인자 인젝션 방어 패턴과 동일).
    # query/fragment는 이 재구성으로 자연히 제거된다.
    canonical = f"https://www.threads.net/@{author}/post/{code}"

    _emit_progress(progress, "fast_started", started_at=started_at, post_code=code)
    posts, actual_deep = _run_scrape(
        canonical, deep=deep, auto=auto, max_pages=max_pages, author=author, code=code,
        progress=lambda event, **fields: _emit_progress(progress, event, started_at=started_at, **fields),
    )
    assessment = _dispatcher.assess(posts, canonical)
    _emit_progress(progress, "fast_complete", started_at=started_at,
                   post_code=code, post_count=len(posts), root_found=bool(assessment.get("root_found")))
    if not assessment.get("root_found"):
        _emit_progress(progress, "collection_failed", started_at=started_at,
                       reason="root_not_found", post_code=code)
        raise RuntimeError(
            "threads: root 포스트를 찾지 못함 — 스크랩 실패 가능(네트워크/쿠키만료/차단/잘못된 URL)"
        )
    continuation_resolution = {"mode": "not_run", "status": "not_run", "reason": "explicit_collection_mode"}
    if not deep and not actual_deep and not all_comments:
        root = next((p for p in posts if p.get("code") == code), None)
        root_author = (root or {}).get("author") or author
        raw_post_count = len(posts)
        posts = [
            p for p in posts
            if p.get("code") == code or (root_author and p.get("author") == root_author)
        ]
        assessment = {
            **assessment,
            "author_only": True,
            "comments_filtered": raw_post_count - len(posts),
        }
        if not auto and root_author:
            posts, continuation_resolution = _resolve_author_continuations(
                posts, author=root_author, root_code=code, progress=progress, started_at=started_at,
            )

    if download:
        download_media(posts, out_dir=out_dir)

    # actual_deep(=_run_scrape가 실제로 어느 경로를 탔는지)을 normalize에 넘긴다 — 원래
    # 호출 인자 deep을 그대로 넘기면 §5 기본-승격 경로로 deep 크롤이 실제 실행됐는데도
    # meta.author_thread.deep/scrape_mode가 거짓으로 "fast"를 보고하는 버그가 있었다
    # (post-review P1, Codex meta review로 발견·확인).
    result = normalize(posts, source=url, author=author, code=code,
                       assessment=assessment, downloaded=download,
                       deep=actual_deep, max_pages=max_pages,
                       continuation_resolution=continuation_resolution)
    _emit_progress(progress, "collection_complete", started_at=started_at,
                   status=continuation_resolution.get("status", "not_run"),
                   author_thread_count=len(result["author_thread"]),
                   comments_count=len(result["comments"]))
    return result


def _run_scrape(url: str, *, deep: bool, auto: bool, max_pages: int,
                author: str = "", code: str = "", progress=None) -> tuple[list[dict], bool]:
    """asyncio 이벤트 루프 기동 + vendored 티어 디스패처 호출을 감싸는 동기 경계.

    vendored fast_scrape.scrape()/scrape_threads_recursive()/assess()를 그대로
    위임한다(로직 미변경). deep 경로는 디스패처의 run_deep()이 max_pages=100을
    하드코딩하므로 우회해 scrape_threads_recursive()를 직접 호출한다 — CLI의
    --max-pages/fetch(max_pages=)가 실제 크롤에 반영되도록(meta 표기와 일치).
    fast_scrape.scrape()는 결과를 JSON 파일로도 쓰는 시그니처라 os.devnull을
    out 경로로 넘겨 어댑터 호출 시 잔여 파일을 남기지 않는다.

    기본 경로는 fast pass에서 멈춘다. auto=True일 때만 assess().incomplete를 보고
    명시적으로 deep으로 승격한다.

    반환값: (posts, actual_deep). actual_deep은 호출자가 넘긴 `deep` 인자가 아니라
    "실제로 deep 크롤(scrape_threads_recursive)이 이 호출에서 실행됐는지"를 뜻한다
    — explicit deep=True든, explicit auto=True+incomplete든 True. fetch()는 이 실제 값을 normalize()에 넘겨야
    meta.author_thread.deep/scrape_mode가 정직하다(post-review P1 픽스 — 이전에는
    fetch()가 원래 `deep` 인자를 그대로 normalize에 넘겨, 기본-승격 경로로 deep이
    실제 실행돼도 meta가 거짓으로 "fast"를 보고했다).
    """
    import asyncio
    import os

    if deep:
        posts = asyncio.run(_dispatcher.scrape_threads_recursive(url, max_pages=max_pages))
        return posts, True

    async def _fast_then_maybe_deep() -> tuple[list[dict], bool]:
        posts = await _dispatcher.fast_scrape.scrape(
            url, os.devnull, do_download=False, progress=progress,
        )
        if auto:
            a = _dispatcher.assess(posts, url)
            if a.get("incomplete"):
                posts = await _dispatcher.scrape_threads_recursive(url, max_pages=max_pages)
                return posts, True
        return posts, False

    return asyncio.run(_fast_then_maybe_deep())


def _item_shape(p: dict) -> dict:
    """comments[]/author_thread[] 공용 item shape (계약 §1 — 최대한 comments와 맞춤)."""
    return {
        "id": p.get("id"),
        "code": p.get("code"),
        "author": p.get("author"),
        "text": p.get("text") or "",
        "likes": p.get("likes", 0),
        "reply_count": p.get("reply_count", 0),
        "media_paths": p.get("downloaded") or [],
        "text_blocks": list(p.get("text_blocks") or []),
    }


def _taken_at_value(post: dict) -> float | None:
    """raw `taken_at`을 정렬 가능한 수치로, 판정 불가면 None(=시각 미상).

    Threads 응답의 `taken_at`은 Unix epoch 초 정수지만, 응답 스키마 변화나
    파서 실패로 없거나 다른 타입일 수 있다. 그런 값은 예외를 올리지 않고
    None으로 떨어뜨려 수집 자체를 깨지 않는다.
    """
    raw = post.get("taken_at")
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            value = float(raw)
        except OverflowError:                     # float 범위를 넘는 임의정밀 int
            return None
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None      # NaN·±무한 배제


def _ordered_by_taken_at(posts: list[dict]) -> list[dict]:
    """원저자 연속글을 원문 게시 시각 오름차순으로 되돌린다(R43-H1).

    fast 수집은 GraphQL/HTML 응답 도착 순서로 포스트를 발견하고 continuation은
    나중에 찾은 포스트를 목록 끝에 append하므로, 입력 순서는 화면의 시간 흐름과
    다를 수 있다. `taken_at`을 아는 항목끼리만 시간순으로 재배치하고, 시각을
    모르는 항목은 발견 순서 그대로 원래 자리에 남긴다 — 동률은 stable sort가
    발견 순서를 보존한다. 시각 미상 항목에 대해 "언제 게시됐다"를 추측하지 않는다.
    """
    known = [(i, p) for i, p in enumerate(posts) if _taken_at_value(p) is not None]
    if len(known) < 2:
        return list(posts)
    ordered = sorted((p for _, p in known), key=_taken_at_value)
    result = list(posts)
    for (index, _), post in zip(known, ordered):
        result[index] = post
    return result


def normalize(posts: list[dict], *, source: str, author: str, code: str,
              assessment: dict | None = None, downloaded: bool = False,
              deep: bool = False, max_pages: int | None = None,
              continuation_resolution: dict | None = None) -> dict:
    """vendored 스크래퍼 결과(flat post list) → sipher 정규화 스키마. 공개 API.

    posts는 원 스레드 글(code == 요청 code) + 댓글(그 외)이 뒤섞인 flat list다
    (vendored 스크래퍼가 중첩 트리가 아니라 id→post map으로 수집하므로). root를
    분리해 body_text로 담는다.

    [Round A] non-root 중 author가 root author와 같은 포스트는 Threads 특유의
    "저자 본인이 자기 글을 이어 쓰는 연속 포스트" 문화를 반영해 `author_thread[]`로
    분리한다 — comments[]에는 실제 타인 댓글만 남긴다.

    [R43-H1] `author_thread[]`는 원저자가 실제로 게시한 시각(raw `taken_at`)
    오름차순으로 반환한다. 시각이 같거나 미상인 항목은 입력 발견 순서를 그대로
    유지한다. `comments[]`는 종전대로 입력 순서를 보존한다(재정렬하지 않음).

    알려진 한계(silent 아님): author == root_author 규칙은 "자기 연속글"과
    "저자 본인이 타인 댓글에 남긴 답글"을 구분하지 못한다 — vendored
    parse_post()(fast_scrape.py/threads_scraper_v2.py)가 반환하는 post dict에는
    parent/reply-target 필드가 없다(id/code/text/author/likes/reply_count/
    images/videos뿐, text_post_app_info에서도 direct_reply_count만 추출됨).
    이 한계는 meta.author_thread.self_reply_ambiguous로 정직하게 라벨링한다.
    """
    root = next((p for p in posts if p.get("code") == code), None)

    root_author = (root or {}).get("author") or author or None
    author_match_available = bool(root_author)

    author_thread_posts: list[dict] = []
    comment_posts: list[dict] = []
    unknown_author_count = 0

    for p in posts:
        if p is root or p.get("code") == code:
            continue
        p_author = p.get("author")
        if not p_author:
            unknown_author_count += 1
        if author_match_available and p_author == root_author:
            author_thread_posts.append(p)
        else:
            comment_posts.append(p)

    body_text = (root or {}).get("text") or ""
    root_media_paths = [*(root or {}).get("downloaded", [])] if root else []
    all_media_paths = []
    for p in posts:
        all_media_paths.extend(p.get("downloaded") or [])

    # 출력 정규화 직전에 원저자 연속글만 원문 시간순으로 되돌린다 — comments[]는
    # 손대지 않는다(parent/reply-target이 없어 대화 트리를 복원할 수 없음).
    author_thread_posts = _ordered_by_taken_at(author_thread_posts)

    comments = [_item_shape(p) for p in comment_posts]
    author_thread = [_item_shape(p) for p in author_thread_posts]

    assessment = dict(assessment or {})
    assessment["scrape_mode"] = "deep" if deep else "fast"
    if deep:
        assessment["max_pages"] = max_pages

    total_media, got_media = _media_counts(posts)

    # possible_more: deep이면서 assess()가 incomplete가 아니라고 판단하면 False.
    # 그 외(=fast 결과이거나 여전히 incomplete)에는 보수적으로 True — silent false 금지.
    incomplete = assessment.get("incomplete")
    if deep and incomplete is False:
        possible_more: bool | None = False
    elif incomplete is None:
        possible_more = None
    else:
        possible_more = True

    return {
        "source": source,
        "platform": "threads",
        "body_text": body_text,
        "author_thread": author_thread,
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
            "author_thread": {
                "count": len(author_thread),
                "root_author": root_author,
                "author_match_available": author_match_available,
                "deep": deep,
                "max_pages": max_pages,
                "possible_more": possible_more,
                "unknown_author_count": unknown_author_count,
                "codes": [p.get("code") for p in author_thread_posts],
                "self_reply_ambiguous": True,
                "self_reply_ambiguous_reason": (
                    "vendored parse_post()에 parent/reply-target 필드가 없어 "
                    "'자기 연속글'과 '자기가 타인 댓글에 남긴 답글'을 구분할 수 없음"
                ),
                "resolution": dict(continuation_resolution or {"mode": "not_run", "reason": "not_provided"}),
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }
