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
from itertools import islice
from pathlib import Path
from typing import Literal

__all__ = ["fetch", "parse_url", "normalize", "InstagramAccessError"]

_log = logging.getLogger(__name__)

MediaLabel = Literal["none", "downloaded", "partially_downloaded", "download_failed"]
CommentsLabel = Literal["not_requested", "collected", "login_required", "fetch_failed"]
# round-B: comments=True의 의미를 "가능한 댓글 수집"에서 "첫 댓글 1개만 수집"으로
# 축소한다(.handoff/rounds/round-B-first-comment-mode-contract.md). comments_label은
# 기존 의미를 유지(성공/실패/권한 판정) — comment_collection_mode가 "정책상 몇 개를
# 시도했는가"를 별도로 정직하게 표시한다.
CommentCollectionMode = Literal["not_requested", "first_only"]
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

# 미디어(이미지/영상) 다운로드 상한 — 자원 고갈 차단(naver_blog와 동일 값).
_MAX_MEDIA_BYTES = 300 << 20


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


def _media_label(
    media_paths: list[str], *, has_media: bool, downloaded: bool, expected: int = 1
) -> MediaLabel:
    if not downloaded:
        return "none"
    if not has_media:
        return "none"
    if not media_paths:
        return "download_failed"
    if expected > 0 and len(media_paths) < expected:
        return "partially_downloaded"  # round-34 §C: 캐러셀 일부만 받음 — 정직 노출
    return "downloaded"


def _collect_first_instagram_comment(post) -> list[dict]:
    """post.get_comments() lazy iterator에서 첫 1개만 소비 → 변환된 댓글 list.

    round-B(.handoff/rounds/round-B-first-comment-mode-contract.md): comments=True의
    의미를 "가능한 댓글 수집"에서 "첫 댓글 1개만 수집"으로 축소한다. instaloader
    4.15.1(설치 버전) 소스 확인 결과 `Post.get_comments()`는 케이스에 따라:
    - 댓글 수가 많으면 `NodeIterator`(GraphQL pagination) 또는
      `_get_comments_via_iphone_endpoint()`를 반환 — 순회할 때마다 다음 페이지를
      추가 네트워크 요청으로 가져오는 **진짜 lazy pagination iterator**.
    - 포스트 메타데이터가 이미 전체 댓글을 포함하면(`self.comments ==
      len(comment_edges) + answers_count`) 이미 메모리에 있는 plain `list`를 반환
      (이 경우는 추가 네트워크 비용이 원래 없다).
    `itertools.islice(..., 1)`로 첫 1개만 소비하면 앞의 pagination 케이스에서
    2페이지째 이후 HTTP 요청이 발생하지 않는다 — `list(post.get_comments())[:1]`은
    iterator를 전량 소비해 이 목적을 위반하므로 사용하지 않는다(계약 §주의).

    빈 iterator(댓글 0개)에도 IndexError 없이 빈 list를 반환한다(Amendment P2 —
    "댓글 0개 엣지케이스").
    """
    return [
        {
            "id": c.id,
            "author": c.owner.username if c.owner else None,
            "text": c.text or "",
            "likes_count": getattr(c, "likes_count", 0),
            "created_at_utc": c.created_at_utc.isoformat() if c.created_at_utc else None,
        }
        for c in islice(post.get_comments(), 1)
    ]


def _resolve_instagram_comments(
    post, comments: bool, instaloader_module,
) -> tuple[list[dict], CommentsLabel, CommentCollectionMode]:
    """comments 플래그 → (collected_comments, comments_label, comment_collection_mode).

    round-B Post-Review Fix(P2, Codex 지적): `fetch()`의 `if comments: ... else:
    ...` 분기를 이 헬퍼로 뽑아낸 이유는 순수히 테스트 가능성 때문이다 — 기존
    `test_comments_false_never_calls_get_comments`는 `fetch()`를 통하지 않고
    `collected_comments = []`를 직접 손으로 구성해 `normalize()`에 넘겼기 때문에
    "comments=False면 get_comments()가 호출되지 않는다"는 실제 코드 경로를
    검증하지 못하고 있었다(normalize()가 입력을 그대로 반영한다는 것만 증명).
    이 헬퍼가 생기면 테스트는 `_resolve_instagram_comments(post, comments=False,
    ...)`를 직접 호출해 poison iterator가 실제로 안 불린다는 것을 검증할 수 있다
    (facebook `_maybe_fetch_comments()`가 이미 이 패턴으로 올바르게 테스트되고
    있었음 — round-B 최초 구현에서 IG 쪽만 이 실수를 했다).

    instaloader_module은 `fetch()`가 이미 `_build_context()`로 얻은 `instaloader`
    모듈 객체를 그대로 받는다(순환 import 없이 예외 타입에 접근하기 위함 — 이
    헬퍼 자체는 instaloader를 top-level import하지 않는다, 모듈 전체의 지연
    임포트 원칙 유지).
    """
    collected_comments: list[dict] = []
    comments_label: CommentsLabel = "not_requested"
    comment_collection_mode: CommentCollectionMode = (
        "first_only" if comments else "not_requested"
    )
    if not comments:
        return collected_comments, comments_label, comment_collection_mode

    try:
        collected_comments = _collect_first_instagram_comment(post)
        comments_label = "collected"
    except instaloader_module.exceptions.LoginRequiredException:
        comments_label = "login_required"
    except instaloader_module.exceptions.ConnectionException:
        comments_label = "login_required"  # 익명 차단도 사실상 로그인 요구와 동일한 결과
    except Exception as e:  # 정직 degrade — 원인 로그는 남기되 fetch 자체는 죽이지 않음
        _log.warning("instagram: 댓글 수집 실패(%s): %s", type(e).__name__, e)
        comments_label = "fetch_failed"
    return collected_comments, comments_label, comment_collection_mode


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
      요청 방지 — 익명 상태에서 추가 403 유발을 피함). meta.comment_collection_mode
      ="not_requested".
    - comments=True(round-B: "첫 댓글 1개만"): `post.get_comments()` lazy iterator에서
      `itertools.islice(..., 1)`로 첫 1개만 소비한다(2페이지째 이후 IG 네트워크
      요청 발생 안 함 — 상세: `_collect_first_instagram_comment` docstring).
      성공 시 `comments`의 길이는 0(댓글이 원래 0개) 또는 1이다. 실패 시
      comments=[]로 두고 meta.comments_label="login_required"(또는
      "fetch_failed")로 정직 degrade — 절대 조용히 빈 리스트만 반환하지 않는다
      (라벨로 원인 명시). meta.comment_collection_mode="first_only"는 성공/실패와
      무관하게 "정책상 첫 댓글만 시도했다"는 사실 자체를 표시한다(계약 §고정 meta
      필드명 — comments_label과 comment_collection_mode는 서로 다른 축).
    - download=True: media_dir(기본 "downloads")에 다운로드 — 캐러셀은 전 항목,
      단일 포스트는 미디어 1건(round-34 §C — 이전엔 캐러셀도 대표 1건만 받았다).
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

    # round-B: comment_collection_mode는 comments_label과 별개 축 — "성공/실패"가
    # 아니라 "정책상 몇 개를 시도했는가"를 표시한다(계약 §고정 meta 필드명). 이
    # 블록은 _resolve_instagram_comments()로 위임한다(round-B Post-Review Fix(P2)
    # — 헬퍼 docstring 참조: comments=False 분기가 실제로 get_comments()를
    # 호출하지 않는다는 것을 fetch() 전체를 mocking하지 않고도 단위 테스트할 수
    # 있게 하기 위함).
    collected_comments, comments_label, comment_collection_mode = (
        _resolve_instagram_comments(post, comments, instaloader)
    )

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
        comment_collection_mode=comment_collection_mode,
        media_paths=media_paths,
        downloaded=download,
        has_media=has_media,
        access_label="ok",
    )


def _ext_from_url(url: str, default: str) -> str:
    path = url.split("?", 1)[0]
    m = re.search(r"\.(jpg|jpeg|png|webp|mp4|mov)$", path, re.IGNORECASE)
    return "." + m.group(1).lower() if m else default


class _SidecarEnumerationFailed(Exception):
    """캐러셀(GraphSidecar)로 확인됐으나 하위 노드 열거가 실패함(round-34 재게이트 P0)."""


def _sidecar_nodes(post) -> list:
    """캐러셀(GraphSidecar)이면 하위 노드 목록을, 캐러셀이 아니면 빈 리스트를 돌려준다.

    `typename`으로 먼저 "캐러셀인가"를 판정한다 — 이전엔 `get_sidecar_nodes()`의 모든
    예외를 삼켜 "캐러셀 아님"과 "캐러셀인데 열거 실패"를 구분하지 못했다(재게이트 실측:
    열거 실패 시 대표 1건만 받고도 `media_label="downloaded"`·`ocr_label="done"`으로
    성공을 위장 — 캐러셀 나머지가 조용히 사라짐). 진짜 열거 실패는
    `_SidecarEnumerationFailed`로 명시해 호출부가 "개수 불명"을 "0개"로 위장하지 않게 한다.
    """
    if getattr(post, "typename", None) != "GraphSidecar":
        return []
    try:
        return list(post.get_sidecar_nodes())
    except Exception as e:
        raise _SidecarEnumerationFailed(str(e)) from e


def _media_counts(post) -> tuple[int | None, int | None]:
    """(image_count, video_count) — 다운로드 전 메타 조회만으로 계산(round-34 §C).

    캐러셀은 각 sidecar 노드의 is_video로 집계, 단일 포스트는 post.is_video로 판정.
    캐러셀 열거가 실패하면 `(None, None)`을 돌려준다 — "0개"(원래 없음)로 위장하지
    않는다. `enrich_ocr`이 이 값과 실제 다운로드 수를 대조해 not_downloaded/partial을
    구분한다.
    """
    try:
        nodes = _sidecar_nodes(post)
    except _SidecarEnumerationFailed as e:
        _log.warning("instagram: 캐러셀 열거 실패(%s) — 개수 불명으로 정직 표기", e)
        return None, None
    if nodes:
        videos = sum(1 for n in nodes if bool(getattr(n, "is_video", False)))
        return len(nodes) - videos, videos
    return (0, 1) if bool(getattr(post, "is_video", False)) else (1, 0)


def _download_media(post, *, out_dir: str, code: str) -> list[str]:
    """캐러셀은 전 항목을, 단일 포스트는 대표 미디어 1건을 다운로드(round-34 §C —
    이전엔 캐러셀도 대표 1건만 받아 나머지가 ocr_label="done" 뒤에 숨는 silent-loss였다).
    항목별 실패는 그 항목만 건너뛴다 — 부분 실패도 media_label(partially_downloaded)로
    상위에서 정직 노출한다. 캐러셀 열거 자체가 실패하면(재게이트 P0) 무엇을 받아야
    할지 모르므로 다운로드를 시도하지 않고 빈 리스트를 돌려준다 — 대표 1건을 받고
    "성공"으로 위장하지 않는다. 예외를 올리지 않고 성공분만 반환.

    round-33: 다운로드는 core.media_io.download_to_file(스트리밍+상한+원자적 rename)로
    수행한다 — 재fetch 실패 시(네트워크 중단 등) 이미 받아둔 캐시 파일을 절대 훼손하지
    않는다(0바이트로 자르는 직접 open(dest,"wb")를 더 이상 쓰지 않음).
    """
    from core.media_io import download_to_file

    os.makedirs(out_dir, exist_ok=True)
    try:
        nodes = _sidecar_nodes(post)
    except _SidecarEnumerationFailed as e:
        _log.warning("instagram: 캐러셀 열거 실패(%s) — 다운로드 시도 안 함(정직 실패)", e)
        return []
    items: list[tuple[int, str | None, bool]] = []
    if nodes:
        for idx, node in enumerate(nodes, start=1):
            is_video = bool(getattr(node, "is_video", False))
            url = getattr(node, "video_url", None) if is_video else None
            url = url or getattr(node, "display_url", None) or getattr(node, "url", None)
            items.append((idx, url, is_video))
    else:
        try:
            is_video = bool(post.is_video)
            url = post.video_url if is_video else post.url
        except Exception as e:
            _log.warning("instagram: 미디어 URL 조회 실패(%s): %s", type(e).__name__, e)
            return []
        items.append((1, url, is_video))

    paths: list[str] = []
    for idx, url, is_video in items:
        if not url:
            continue
        suffix = _ext_from_url(url, ".mp4" if is_video else ".jpg")
        dest = os.path.join(out_dir, f"ig_{code}_{idx:02d}{suffix}" if nodes else f"ig_{code}{suffix}")
        ok = download_to_file(
            url, Path(dest),
            headers={"User-Agent": "Mozilla/5.0"},
            max_bytes=_MAX_MEDIA_BYTES,
        )
        if ok:
            paths.append(dest)
    return paths


def normalize(
    post,
    *,
    source: str,
    code: str,
    comments: list[dict],
    comments_label: CommentsLabel,
    comment_collection_mode: CommentCollectionMode,
    media_paths: list[str],
    downloaded: bool,
    has_media: bool,
    access_label: AccessLabel,
) -> dict:
    """instaloader Post 객체 → sipher 정규화 스키마. 공개 API.

    OCR/전사는 이 단계에서 채우지 않는다(sipher 정규화 단계에서 opt-in enrich,
    core/__init__.py fetch(ocr=, transcribe=) 참조 — threads/naver_blog와 동일).

    round-B: `comment_collection_mode`는 `comments_label`을 대체하지 않는
    별도 축이다 — "정책상 몇 개를 시도했는가"("not_requested" | "first_only")를
    표시하고, 실제 성공/실패/권한 문제는 기존 `comments_label`이 표현한다.
    """
    image_count, video_count = _media_counts(post)
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
            "comment_collection_mode": comment_collection_mode,
            "is_video": bool(getattr(post, "is_video", False)),
            "image_count": image_count,   # round-34 §C: enrich_ocr 완전성 대조용 사전신호
            "video_count": video_count,
            "media_label": _media_label(
                media_paths, has_media=has_media, downloaded=downloaded,
                expected=(image_count + video_count) if image_count is not None else 0,
            ),
            "ig_access_label": access_label,
            "date_utc": post.date_utc.isoformat() if getattr(post, "date_utc", None) else None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }
