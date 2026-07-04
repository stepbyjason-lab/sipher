r"""
sipher-instagram 어댑터 — 독립 도구(패키지). pip 라이브러리 직접 호출(벤더링 아님).

공개 API: `fetch(url) -> 정규화 JSON dict` (sipher 라우터/단독 CLI 공용).
sipher 내부를 import 하지 않는 깨끗한 경계(threads/naver_blog와 동일 원칙).

정규화 스키마: { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }
설계: docs/00-overview.md. round-09 contract(.handoff/rounds/round-09-sns-adapters-contract.md),
round-10 정정(.handoff/rounds/round-10-absorb-web-contract.md §④).

**로그인 세션 필수 (round-10 정정).** round-09는 "익명 우선"으로 설계됐으나,
round-09/round-10 두 라운드에 걸친 실측(instaloader 4.15.1, 2026-07-02)이 일관되게
보여준 사실은: **IG 서버가 익명 graphql/query 요청을 거의 항상 403 Forbidden으로
차단한다**(instaloader GitHub #2682/#2678, 2026-03~04 활성 이슈 — 우리 설치 버전과
동일 증상, 우연한 일회성 장애가 아니라 IG 측 anti-scraping 정책의 현재 상태로 판단).
즉 "익명 우선"은 설계 의도였을 뿐 실제로는 **거의 항상 실패하는 경로**였다 — 이
docstring은 그 실측을 반영해 "로그인 세션이 사실상 필수"로 재포지셔닝한다.

이 어댑터는 그 사실을 은폐하지 않는다 — 익명 접근이 403으로 막히면 명확한
`InstagramAccessError`(RuntimeError 서브클래스)로 "로그인 세션 필요 — session_file
지정 또는 브라우저 프로필 쿠키 필요"를 안내한다(빈 결과를 성공처럼 반환하지 않음).
댓글만 막힌 경우는 `meta.comments_label`로 정직 degrade한다. 로그인 세션 opt-in
(`session_file`)은 인터페이스만 열어둔다 — 실계정 로그인 라이브 검증은 이번
스코프 밖(Pre-Action Documentation Rule 대상 — 사용자 자격증명, round-10 §④ 명시).
쿠키 재사용 경로 노트는 docs/00-overview.md §9 참조.

instaloader는 fetch 실행 시에만 필요 — parse_url/normalize는 instaloader 없이도
import·단위 테스트 가능(threads의 playwright 지연 임포트와 동일 원칙).
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

__all__ = ["fetch", "parse_url", "normalize", "InstagramAccessError"]

_log = logging.getLogger(__name__)

MediaLabel = Literal["none", "downloaded", "partially_downloaded", "download_failed"]
CommentsLabel = Literal["not_requested", "collected", "login_required", "fetch_failed"]
# round-10 Post-Review Fix(P2): 로그인 세션(session_file 지정) 사용 중 실패해도
# "anonymous_blocked"로 고정돼 있어 호출자가 "익명이라 차단됐다"고 오판할 수 있던
# 문제(round-10 독립 리뷰 P2). "session_failed"를 추가해 익명 차단과 로그인 세션
# 실패(세션 만료 등)를 구조화된 라벨 레벨에서 구분한다.
AccessLabel = Literal["ok", "anonymous_blocked", "session_failed"]


class InstagramAccessError(RuntimeError):
    """IG 포스트 조회 실패 시 발생 — round-10 §④ 정정(round-09 P2 팔로우업).

    round-09에서는 `meta.ig_access_label`로 상위 계층이 접근 상태를 판별하게
    설계했으나, 실패 경로가 예외로 즉시 튀는 구조라 `meta` 자체가 반환되지
    않아 그 라벨이 항상 죽어있었다(round-09 리뷰 P2). 이 예외 클래스가 그
    간극을 메운다 — `access_label` 속성으로 상위 계층이 `except
    InstagramAccessError as e: e.access_label`로 실제 접근 상태를 판별할 수
    있다. 일반 `except RuntimeError`로도 그대로 잡히므로(서브클래스) 기존
    호출자(threads 패턴을 따르는 `RuntimeError` catch)와 호환된다.
    """

    def __init__(self, message: str, *, access_label: AccessLabel):
        super().__init__(message)
        self.access_label: AccessLabel = access_label


# instagram.com 만 허용(SSRF·인자 인젝션 방어). /p/, /reel/, /tv/ 3종 경로가 모두
# 동일한 shortcode 스킴을 쓴다(instaloader Post.from_shortcode가 셋 다 처리).
_HOST = re.compile(r"^(?:https?://)?(?:www\.)?instagram\.com/", re.I)
_POST_PATH = re.compile(r"/(?:p|reel|tv)/(?P<code>[A-Za-z0-9_-]+)", re.I)


def parse_url(url: str) -> str:
    """IG 포스트/릴스/IGTV URL → shortcode. 실패 시 ValueError.

    호스트를 instagram.com으로 제한(SSRF 방어)하고, /p/<code>, /reel/<code>,
    /tv/<code> 경로만 통과시킨다. 프로필/홈/스토리 URL(code 없음)은 거부한다
    (threads parse_url과 동일 패턴 — docs/00-overview.md §비목표).
    """
    if not isinstance(url, str):
        raise ValueError("URL은 문자열이어야 합니다")
    s = url.strip()
    if len(s) > 2048:
        raise ValueError("URL이 너무 깁니다")
    if not _HOST.match(s):
        raise ValueError(f"Instagram URL이 아닙니다: {s.split('?', 1)[0]!r}")
    m = _POST_PATH.search(s)
    if not m:
        raise ValueError(
            f"포스트 shortcode를 찾을 수 없습니다(예: /p/ABC123/, /reel/ABC123/): "
            f"{s.split('?', 1)[0]!r}"
        )
    code = m.group("code")
    if ".." in code:
        raise ValueError(f"올바르지 않은 shortcode입니다: {code!r}")
    return code


def _build_context(session_file: str | Path | None):
    """instaloader.InstaloaderContext 준비. session_file 미지정 시 익명 컨텍스트.

    지연 임포트 — fetch 호출 전에는 instaloader 의존성이 필요 없다.
    """
    import instaloader

    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
    )
    if session_file:
        session_path = Path(session_file)
        if not session_path.is_file():
            raise ValueError(f"session_file이 존재하지 않습니다: {session_path}")
        # instaloader 세션 파일명은 관례상 "session-<username>" — 파일명에서
        # username을 복원할 수 없는 경우를 대비해 stem 전체를 넘기고 실패 시
        # 사용자에게 명확한 에러를 올린다(로그인 세션 실계정 테스트는 스코프 밖이라
        # 여기서는 인터페이스만 정확히 제공한다).
        username = session_path.name.split("session-", 1)[-1] or session_path.stem
        try:
            L.load_session_from_file(username, str(session_path))
        except Exception as e:  # instaloader가 다양한 예외를 던짐 — 정직하게 감싸서 재발생
            raise RuntimeError(f"instagram: session_file 로드 실패({session_path}): {e}") from e
    return L, instaloader


def _media_label(media_paths: list[str], *, has_media: bool, downloaded: bool) -> MediaLabel:
    if not downloaded:
        return "none"
    if not has_media:
        return "none"
    return "downloaded" if media_paths else "download_failed"


def fetch(
    url: str,
    *,
    media_dir: str | Path | None = None,
    download: bool = False,
    session_file: str | Path | None = None,
    comments: bool = False,
) -> dict:
    """단일 Instagram 포스트/릴스 URL → 정규화 JSON dict.

    - session_file 미지정(기본): 익명 컨텍스트. round-09/round-10 spike 실측대로,
      IG 서버가 익명 graphql/query를 거의 항상 403으로 차단한다(docstring §로그인
      세션 필수 참조) — 이 경우 `InstagramAccessError`(access_label=
      "anonymous_blocked")로 "로그인 세션 필요"를 명확히 안내한다(빈 dict를
      성공처럼 반환하지 않음).
    - session_file 지정(opt-in): 로그인 세션으로 시도(실계정 테스트는 스코프 밖 —
      인터페이스만 제공, threads의 cookie 파일 패턴과 동일 경계).
    - comments=False(기본): get_comments()를 아예 호출하지 않는다(불필요한 IG
      요청 방지 — 익명 상태에서 추가 403 유발을 피함).
    - comments=True: get_comments() 시도. 실패 시 comments=[]로 두고
      meta.comments_label="login_required"(또는 "fetch_failed")로 정직 degrade —
      절대 조용히 빈 리스트만 반환하지 않는다(라벨로 원인 명시).
    - download=True: media_dir(기본 "downloads")에 이미지/영상 1건 다운로드.
      CDN URL은 서명·시간제한이라 스크랩 직후 받지 않으면 만료된다(threads와 동일).

    보안 경고(trusted input): media_dir/session_file은 로컬 사용자가 지정하는 신뢰
    입력이다. 이 함수는 경로 containment를 하지 않는다(threads/youtube와 동일 경계).
    """
    code = parse_url(url)

    L, instaloader = _build_context(session_file)

    # 로그인 세션 유무에 따라 실패 시 안내 메시지·access_label을 다르게 준다
    # (round-10 §④ — "session_file 지정 또는 브라우저 프로필 쿠키 필요"를
    # 익명 실패 시에만 안내하고, 로그인 세션으로도 실패했다면 다른 원인일
    # 가능성이 높으므로 "로그인 필요" 안내를 반복하지 않는다).
    is_anonymous = session_file is None

    try:
        post = instaloader.Post.from_shortcode(L.context, code)
    except instaloader.exceptions.ConnectionException as e:
        if is_anonymous:
            raise InstagramAccessError(
                f"instagram: Instagram은 로그인 세션이 필요합니다 — 익명 접근이 IG "
                f"서버에 의해 차단됐습니다(round-09/round-10 spike에서 일관 재현, "
                f"instaloader#2682/#2678 참조). session_file 지정 또는 브라우저 "
                f"프로필 쿠키(docs/00-overview.md §9)를 사용하세요: {e}",
                access_label="anonymous_blocked",
            ) from e
        raise InstagramAccessError(
            f"instagram: 포스트 조회 실패(로그인 세션 사용 중에도 접근 차단 — "
            f"세션 만료 또는 다른 IG 서버 정책 가능성): {e}",
            access_label="session_failed",
        ) from e
    except instaloader.exceptions.LoginRequiredException as e:
        if is_anonymous:
            raise InstagramAccessError(
                f"instagram: 이 포스트는 로그인이 필요합니다 — session_file 지정 또는 "
                f"브라우저 프로필 쿠키(docs/00-overview.md §9)를 사용하세요: {e}",
                access_label="anonymous_blocked",
            ) from e
        # 로그인 세션(session_file)을 이미 사용 중인데도 LoginRequiredException이면
        # "익명이라 차단"이 아니라 세션 자체의 문제(만료·권한 부족 등)다 —
        # round-10 Post-Review Fix(P2): access_label을 재사용하지 않고 구분한다.
        raise InstagramAccessError(
            f"instagram: 로그인 세션 사용 중에도 로그인이 필요하다는 응답을 받았습니다 "
            f"(세션 만료 또는 이 포스트에 대한 권한 부족 가능성): {e}",
            access_label="session_failed",
        ) from e
    except TypeError as e:
        # round-09 spike로 실측: instaloader 4.15.1은 graphql/query가 403을 반환하며
        # 재시도(max_connection_attempts)를 모두 소진하면 ConnectionException을 올리지
        # 않고 내부적으로 None을 반환한 뒤 그 None을 subscript해 TypeError를 낸다
        # (instaloader GitHub #2682/#2683에 보고된 라이브러리 레벨 증상과 일치). 이
        # TypeError를 삼키지 않고 동일한 원인으로 정직하게 재포장해 올린다 — 우회
        # 시도 없음(원칙: IG 서버 정책을 코드로 뚫지 않는다).
        if is_anonymous:
            raise InstagramAccessError(
                f"instagram: Instagram은 로그인 세션이 필요합니다 — 익명 graphql/query "
                f"403이 재시도 소진 후 instaloader 내부에서 TypeError로 leak"
                f"(round-09/round-10 spike·instaloader#2682/#2683과 동일 증상, IG 서버 "
                f"측 익명 접근 차단으로 판단). session_file 지정 또는 브라우저 프로필 "
                f"쿠키(docs/00-overview.md §9)를 사용하세요: {e}",
                access_label="anonymous_blocked",
            ) from e
        raise InstagramAccessError(
            f"instagram: 포스트 조회 실패(로그인 세션 사용 중에도 동일 TypeError leak "
            f"증상 — 세션 만료 가능성): {e}",
            access_label="session_failed",
        ) from e

    collected_comments: list[dict] = []
    comments_label: CommentsLabel = "not_requested"
    if comments:
        try:
            collected_comments = [
                {
                    "id": c.id,
                    "author": c.owner.username if c.owner else None,
                    "text": c.text or "",
                    "likes_count": getattr(c, "likes_count", 0),
                    "created_at_utc": c.created_at_utc.isoformat() if c.created_at_utc else None,
                }
                for c in post.get_comments()
            ]
            comments_label = "collected"
        except instaloader.exceptions.LoginRequiredException:
            comments_label = "login_required"
        except instaloader.exceptions.ConnectionException:
            comments_label = "login_required"  # 익명 차단도 사실상 로그인 요구와 동일한 결과
        except Exception as e:  # 정직 degrade — 원인 로그는 남기되 fetch 자체는 죽이지 않음
            _log.warning("instagram: 댓글 수집 실패(%s): %s", type(e).__name__, e)
            comments_label = "fetch_failed"

    out_dir = str(media_dir) if media_dir else "downloads"
    media_paths: list[str] = []
    has_media = True
    if download:
        media_paths = _download_media(post, out_dir=out_dir, code=code)

    return normalize(
        post,
        source=url,
        code=code,
        comments=collected_comments,
        comments_label=comments_label,
        media_paths=media_paths,
        downloaded=download,
        has_media=has_media,
        access_label="ok",
    )


def _ext_from_url(url: str, default: str) -> str:
    path = url.split("?", 1)[0]
    m = re.search(r"\.(jpg|jpeg|png|webp|mp4|mov)$", path, re.IGNORECASE)
    return "." + m.group(1).lower() if m else default


def _download_media(post, *, out_dir: str, code: str) -> list[str]:
    """post의 대표 미디어(이미지 또는 영상) 1건을 다운로드. 캐러셀은 첫 항목만
    (round-09 비목표 — 캐러셀 심화 처리는 범위 밖, docs/00-overview.md §비목표).
    실패해도 예외를 올리지 않고 빈 리스트를 반환(media_label로 상위에서 판별).
    """
    import urllib.request

    os.makedirs(out_dir, exist_ok=True)
    try:
        url = post.video_url if post.is_video else post.url
    except Exception as e:
        _log.warning("instagram: 미디어 URL 조회 실패(%s): %s", type(e).__name__, e)
        return []
    if not url:
        return []
    dest = os.path.join(out_dir, f"ig_{code}{_ext_from_url(url, '.jpg' if not post.is_video else '.mp4')}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
            f.write(resp.read())
        return [dest]
    except Exception as e:
        _log.warning("instagram: 다운로드 실패(%s): %s", type(e).__name__, e)
        return []


def normalize(
    post,
    *,
    source: str,
    code: str,
    comments: list[dict],
    comments_label: CommentsLabel,
    media_paths: list[str],
    downloaded: bool,
    has_media: bool,
    access_label: AccessLabel,
) -> dict:
    """instaloader Post 객체 → sipher 정규화 스키마. 공개 API.

    OCR/전사는 이 단계에서 채우지 않는다(sipher 정규화 단계에서 opt-in enrich,
    core/__init__.py fetch(ocr=, transcribe=) 참조 — threads/naver_blog와 동일).
    """
    return {
        "source": source,
        "platform": "instagram",
        "body_text": post.caption or "",
        "comments": comments,
        "ocr_text": [],
        "transcript": None,
        "media_paths": media_paths,
        "meta": {
            "shortcode": code,
            "author": post.owner_username if hasattr(post, "owner_username") else None,
            "post_id": getattr(post, "mediaid", None),
            "likes": getattr(post, "likes", 0),
            "comment_count": getattr(post, "comments", 0),
            "comment_count_captured": len(comments),
            "comments_label": comments_label,
            "is_video": bool(getattr(post, "is_video", False)),
            "media_label": _media_label(media_paths, has_media=has_media, downloaded=downloaded),
            "ig_access_label": access_label,
            "date_utc": post.date_utc.isoformat() if getattr(post, "date_utc", None) else None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }
