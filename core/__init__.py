r"""
sipher 코어 라우터 — 단일 진입점.

`fetch(url, **kwargs) -> 정규화 JSON dict`. URL의 host를 코드로 판별해(§5 매트릭스,
LLM 판단 없음) 해당 어댑터 `adapters.<platform>.fetch`로 위임한다. 어댑터는 각자
자급자족 도구이며 라우터는 "어느 플랫폼에 어느 스크래퍼"를 고정하는 얇은 글루다
(docs/01-overview.md §0·§4·§5).

설계 원칙:
- **플랫폼 판별 = 코드(host 화이트리스트) = SSOT.** 이 모듈의 PLATFORM_HOSTS가
  라우팅 정본이다(§5 표). LLM에 URL 판별을 맡기지 않는다.
- **매칭된 어댑터만 지연 임포트(importlib).** 무관 플랫폼의 무거운 의존성
  (playwright 등)을 끌어오지 않는다.
- **공통 smart override는 라우터가 번역.** `download`/`comments`/`transcribe`는
  플랫폼별 인자명(예: youtube `with_video`/`with_comments`/`with_transcript`)으로
  바꾼 뒤 어댑터로 넘긴다. 그 밖의 플랫폼별 옵션은 **kwargs로 통과하며,
  잘못된 옵션은 어댑터가 TypeError로 거른다.
- **로컬 파일 직접 입력(round-08 §C, round-08 리뷰 Post-Review Fix로 순서 정정)**:
  로컬 파일 존재 확인이 URL 매칭보다 **먼저**다. `PLATFORM_HOSTS`의 모든 패턴은
  스킴이 옵션(`^(?:https?://)?...`)이라 `youtube.com/watch` 같은 스킴 없는
  상대경로도 URL로 매칭될 수 있다 — 따라서 "URL 매칭 우선"은 존재하는 동명
  로컬 파일을 은폐하는 오판을 낳는다(round-08 독립 리뷰 P1, 실행 재현됨).
  실제 순서: 입력이 로컬에 실존하는 **파일**(`Path.exists() and Path.is_file()`)
  이면 무조건 로컬로 확정 — Windows에서 `:`는 파일명에 쓸 수 없어 `https://...`
  형태의 실존 경로는 원천적으로 없으므로 모호성이 없다. 존재하지 않으면(또는
  디렉토리면) 기존 URL 매칭 흐름으로 넘어간다.
- **웹 아티클 범용 폴백(round-10 §①②)**: `detect_platform()`이 PLATFORM_HOSTS 중
  어디에도 매칭되지 않아 ValueError를 던지면, `fetch()`는 그 URL이 http(s)로
  보이는 경우에만(`_looks_like_http_url`) `adapters.web`(범용 폴백 어댑터)으로
  위임한다. **`detect_platform()` 자체와 PLATFORM_HOSTS는 이 폴백을 위해 바뀌지
  않는다** — web은 특정 host 화이트리스트가 아니라 "그 외 전부"이므로 SSOT 표에
  넣으면 표의 기존 의미(호스트 판별)와 충돌한다. 따라서 `python -m core detect`는
  여전히 기존 6플랫폼만 인식하고, 웹 URL에는 ValueError를 던진다(의도적 비대칭 —
  `fetch()`만 폴백을 안다). round-10 contract §4에서 명시적으로 결정됐다.
"""
from __future__ import annotations

import importlib
import logging
import re
from pathlib import Path
from typing import Callable

__all__ = ["fetch", "detect_platform", "PLATFORM_HOSTS", "SUPPORTED_PLATFORMS"]

_log = logging.getLogger(__name__)

# §5 라우팅 SSOT: (platform, host 정규식). 위에서부터 첫 매칭이 이긴다.
# host만 본다 — 경로/식별자 검증은 각 어댑터 parse_url이 (SSRF·인젝션 방어까지) 담당.
PLATFORM_HOSTS: list[tuple[str, re.Pattern[str]]] = [
    ("youtube", re.compile(r"^(?:https?://)?(?:[\w-]+\.)?(?:youtube\.com|youtu\.be)/", re.I)),
    ("threads", re.compile(r"^(?:https?://)?(?:www\.)?(?:threads\.net|threads\.com)/", re.I)),
    ("facebook", re.compile(r"^(?:https?://)?(?:[\w-]+\.)?(?:facebook\.com|fb\.watch|fb\.com)/", re.I)),
    ("naver_blog", re.compile(r"^(?:https?://)?(?:m\.)?blog\.naver\.com/", re.I)),
    ("instagram", re.compile(r"^(?:https?://)?(?:www\.)?instagram\.com/", re.I)),
    ("tiktok", re.compile(r"^(?:https?://)?(?:www\.|vt\.|vm\.)?tiktok\.com/", re.I)),
]

SUPPORTED_PLATFORMS: tuple[str, ...] = tuple(p for p, _ in PLATFORM_HOSTS)

# round-29 S1(smart-content-detection): 플랫폼별 "전량 추출" capability 맵.
# smart=True(기본)일 때 core.fetch가 어댑터에 주입할 kwargs — 어댑터마다 미디어
# 다운로드·댓글 트리거 kwarg가 제각각(S3 감사 2026-07-13)이라 이 맵이 SSOT다.
# 규약: 사용자가 명시한 kwarg가 항상 이긴다(주입은 미지정 항목만 채운다 — override 금지).
#   media   : 미디어(이미지/영상) 다운로드를 켜는 kwargs.
#   comments: 첫 댓글 수집을 켜는 kwargs(None=이 플랫폼은 core 레벨 토글 없음).
#   media_dir: True면 smart가 기본 다운로드 경로를 채워야 다운로드가 일어난다(naver 등).
_SMART_CAPS: dict[str, dict] = {
    "tiktok":     {"media": {"download": True},  "comments": None},   # 댓글=R31(정직 미지원 라벨)
    "threads":    {"media": {"download": True},  "comments": None},   # 저자연속글=deep/auto 기본
    "instagram":  {"media": {"download": True},  "comments": {"comments": True}},
    "facebook":   {"media": {"with_video": True}, "comments": {"comments": True}},  # 이미지 자동
    "youtube":    {"media": {"with_video": True, "with_subs": True, "with_transcript": True},
                   "comments": {"with_comments": True}},
    "naver_blog": {"media": {}, "comments": None, "media_dir": True},  # media_dir 존재로 다운로드
    "web":        {"media": {}, "comments": None},                    # 미디어 다운로드 비목표
}

# smart 다운로드 기본 경로(사용자 media_dir 미지정 시). 기존 tiktok 기본("downloads")과 일치.
_SMART_DEFAULT_MEDIA_DIR = "downloads"


def detect_platform(url: str) -> str:
    """URL host → 플랫폼 이름. 지원하지 않는 host면 ValueError.

    host 화이트리스트만 검사한다(SSRF 방어의 1차선). 경로/식별자 형식 검증은
    어댑터 parse_url이 fetch 내부에서 다시 수행하므로, 여기서 통과해도 어댑터가
    최종적으로 거부할 수 있다.
    """
    if not isinstance(url, str):
        raise ValueError("URL은 문자열이어야 합니다")
    s = url.strip()
    if not s:
        raise ValueError("빈 URL")
    for platform, pat in PLATFORM_HOSTS:
        if pat.match(s):
            return platform
    host = s.split("/", 3)[2] if "//" in s else s.split("/", 1)[0]
    raise ValueError(
        f"지원하지 않는 플랫폼입니다: {host!r} "
        f"(지원: {', '.join(SUPPORTED_PLATFORMS)}) 또는 존재하는 로컬 파일 경로"
    )


def _adapter_fetch(platform: str) -> Callable[..., dict]:
    """매칭된 어댑터 모듈을 지연 임포트해 공개 fetch를 돌려준다."""
    mod = importlib.import_module(f"adapters.{platform}")
    fn = getattr(mod, "fetch", None)
    if not callable(fn):
        raise RuntimeError(f"어댑터 adapters.{platform}에 공개 fetch가 없습니다")
    return fn


def _resolve_local(url_or_path: str) -> Path | None:
    """로컬에 실존하는 **파일**이면 해석된 Path를, 아니면 None을 돌려준다.

    로컬 파일 존재 확인이 URL 매칭보다 우선이다(round-08 리뷰 Post-Review Fix
    — 원래 "URL 매칭 우선" 순서는 PLATFORM_HOSTS 정규식이 스킴 옵션
    (`^(?:https?://)?...`)이라는 사실과 모순되어, `youtube.com/watch` 같은
    스킴 없는 상대경로가 존재하는 동명 로컬 파일을 은폐하는 오판을 일으켰다).

    Windows에서 `:`는 파일명에 사용할 수 없으므로 `https://...` 형태의 문자열이
    로컬 파일로 실존할 일은 없다 — 로컬 우선 확정에 모호성이 생기지 않는다.
    디렉토리는 이번 스코프 밖이라 여기서 로컬로 잡지 않고 그대로 통과시켜
    (기존) URL 매칭/에러 흐름으로 넘긴다.
    """
    p = Path(url_or_path)
    if p.exists() and p.is_file():
        return p
    return None


_HTTP_SCHEME = re.compile(r"^https?://", re.I)


def _looks_like_http_url(url: str) -> bool:
    """느슨한 http(s) URL 판별(라우터 레벨의 "값싼 대략적 체크"). 세부 스킴/호스트
    검증은 `adapters.web.parse_url`이 다시 한다(round-10 §④, `_resolve_local`과
    동일하게 "모호성 없는 값싼 체크만 라우터가, 세부 검증은 어댑터가" 패턴).
    로컬 우선 판별(`_resolve_local`)을 통과하지 못하고(=로컬 파일 아님)
    `detect_platform()`도 실패한 경우에만 호출되므로, 여기서 True가 나오면 곧
    web 폴백으로 위임된다.
    """
    return bool(_HTTP_SCHEME.match(url.strip()))


def fetch(
    url: str,
    *,
    smart: bool = True,
    ocr: bool | None = None,
    transcribe: bool | None = None,
    download: bool | None = None,
    comments: bool | None = None,
    whisper_model: str | None = None,
    whisper_device: str | None = None,
    whisper_compute: str | None = None,
    **kwargs,
) -> dict:
    """어떤 지원 플랫폼 URL이든, 또는 로컬 파일 경로든 → sipher 정규화 JSON dict
    (단일 진입점).

    로컬에 실존하는 파일이면 먼저 core.local.fetch_local로 위임한다
    (round-08 §C, Post-Review Fix로 판별 순서 정정 — platform="local").
    그렇지 않으면 URL로 간주해 플랫폼을 판별하고 해당 어댑터 fetch로
    **kwargs를 그대로 위임한다. 반환은 sipher 정규화 스키마:
    { source, platform, body_text, comments[], ocr_text[], transcript,
    media_paths[], meta }.

    smart=True(기본, round-29 S1): 주소만 넣으면 전량 자동추출한다 — 플랫폼별 미디어
    다운로드·첫댓글 kwargs를 capability 맵(_SMART_CAPS, S3 감사)에서 주입하고,
    ocr/transcribe 미지정(None)을 True로 해석한다. **사용자가 명시한 kwarg·플래그가
    항상 이긴다**(주입은 미지정 항목만 채운다 — override 금지). smart=False면 현행
    opt-in 시맨틱(ocr/transcribe 기본 False, 미디어는 어댑터 kwarg 명시 필요) 그대로다.
    ocr/transcribe는 이제 bool|None — None=smart 위임, True/False=명시 override.

    플랫폼별 kwargs 예:
      threads : deep=, auto=, download=, max_pages=, media_dir=
      youtube : from_start=, with_video=, with_subs=, sub_langs=, media_dir=
      web     : js=("auto"/True/False, 기본 "auto"), timeout=(초, 기본 25)
                — 6플랫폼 host 미매칭 http(s) URL의 범용 폴백(round-10 §④)
    해당 어댑터가 받지 않는 kwarg를 넘기면 어댑터가 TypeError를 낸다(라우터는
    옵션을 검열하지 않는다 — 어댑터 계약을 SSOT로 존중). 로컬 흐름에서는
    kwargs가 의미가 없으므로 core.local.fetch_local이 조용히 무시한다.

    ocr=None/transcribe=None은 smart에 위임한다. smart=True(기본)에서는 이미지 OCR과
    전사를 자동 시도하고, smart=False에서는 `ocr=True`/`transcribe=True` 명시 때만
    실행한다. `ocr=False`/`transcribe=False`는 항상 이긴다. 로컬 영상/음성은 무비용
    전사 기본 자동을 유지하지만, 명시 `transcribe=False`면 실행하지 않는다. 이미
    transcript나 어댑터 실패 라벨이 있으면 smart 폴백이 덮어쓰지 않는다.

    ocr과 transcribe를 동시에 True로 주면 순차 적용한다(서로 다른 필드를
    건드리므로 순서 무관).

    whisper_model/whisper_device/whisper_compute: transcribe=True일 때만 의미
    있음. 미지정 시 core.transcribe.transcribe_media가 도구 자체 기본값
    (large-v3/cuda/float16)에 위임. compute는 디바이스와 짝을 맞춰야 한다 —
    CPU는 int8 필요, float16은 GPU 전용.
    """
    local_path = _resolve_local(url)
    if local_path is not None:
        from . import local

        result = local.fetch_local(str(local_path), **kwargs)
        # 로컬 AV는 무비용 전사를 기본 적용하되, 명시 transcribe=False는 존중한다.
        if transcribe is None:
            transcribe = True
    else:
        try:
            platform = detect_platform(url)
        except ValueError:
            # PLATFORM_HOSTS 어디에도 안 걸림 — http(s)로 보이면 웹 아티클 범용
            # 폴백(round-10 §④). detect_platform() 자체는 바뀌지 않는다(SSOT 표
            # 의미 보존) — 이 폴백은 fetch() 레벨에만 존재한다.
            if _looks_like_http_url(url):
                platform = "web"
            else:
                raise
        kwargs = _apply_smart_kwargs(
            platform, kwargs, smart=smart, download=download, comments=comments, transcribe=transcribe
        )
        result = _adapter_fetch(platform)(url, **kwargs)
        result = _apply_common_labels(platform, result, smart=smart, comments=comments)
    # 인리치먼트 미지정(None)은 smart 위임: smart=True→전량추출(True), False→현행 opt-in(False).
    # 로컬은 위에서 transcribe=None일 때만 True로 확정됐으므로 명시 False는 보존된다.
    if ocr is None:
        ocr = smart
    if transcribe is None:
        transcribe = smart
    if ocr or transcribe:
        from . import normalize

        if ocr:
            try:
                result = normalize.enrich_ocr(result)
            except Exception:
                _log.exception("OCR enrichment 실패 — 어댑터 산출물 보존")
                meta = dict(result.get("meta") or {})
                meta["ocr_label"] = "failed"
                result = {**result, "meta": meta}
        if transcribe:
            try:
                result = normalize.enrich_transcribe(
                    result, model=whisper_model, device=whisper_device, compute=whisper_compute
                )
            except Exception:
                _log.exception("transcript enrichment 실패 — 어댑터 산출물 보존")
                meta = dict(result.get("meta") or {})
                meta["transcript_label"] = "failed"
                result = {**result, "meta": meta}
    return result


def _apply_smart_kwargs(
    platform: str,
    kwargs: dict,
    *,
    smart: bool = True,
    download: bool | None = None,
    comments: bool | None = None,
    transcribe: bool | None = None,
) -> dict:
    """Resolve smart defaults and common overrides into adapter-specific kwargs.

    Common controls (download/comments/transcribe) are router-level intent. They
    are never passed through directly to adapters that do not accept those names;
    instead they are translated to each platform's public fetch signature.
    """
    caps = _SMART_CAPS.get(platform)
    if not caps:
        return dict(kwargs)

    resolved = dict(kwargs)

    def put(values: dict | None, *, override: bool = False) -> None:
        for key, value in (values or {}).items():
            if override or key not in resolved:
                resolved[key] = value

    media_defaults = caps.get("media") or {}
    comment_defaults = caps.get("comments") or {}

    if smart:
        put(media_defaults)
        put(comment_defaults)
        if platform == "youtube" and comment_defaults:
            resolved.setdefault("max_comments", 1)

    if download is not None:
        if platform in {"tiktok", "threads", "instagram"}:
            put({"download": download}, override=True)
        elif platform == "facebook":
            put({"with_video": download}, override=True)
        elif platform == "youtube":
            put({"with_video": download, "with_subs": download}, override=True)
        elif platform == "naver_blog":
            if not download:
                resolved.pop("media_dir", None)
            # download=True for naver is represented by media_dir below.

    if comments is not None:
        if platform in {"instagram", "facebook"}:
            put({"comments": comments}, override=True)
        elif platform == "youtube":
            put({"with_comments": comments}, override=True)
            if comments:
                resolved.setdefault("max_comments", 1)

    if transcribe is not None and platform == "youtube":
        put({"with_transcript": transcribe}, override=True)

    wants_media_dir = False
    if platform == "naver_blog":
        wants_media_dir = (smart and download is not False) or download is True
    elif caps.get("media"):
        media_keys = set(media_defaults)
        wants_media_dir = any(resolved.get(key) for key in media_keys)

    if wants_media_dir:
        resolved.setdefault("media_dir", _SMART_DEFAULT_MEDIA_DIR)
    elif download is False:
        # A common download opt-out should not leave smart's default download
        # directory behind as an accidental download trigger.
        resolved.pop("media_dir", None)

    return resolved


def _apply_common_labels(
    platform: str, result: dict, *, smart: bool, comments: bool | None
) -> dict:
    """Add router-level honesty labels for common capabilities adapters lack."""
    meta = dict(result.get("meta") or {})
    comments_requested = comments is True or (comments is None and smart)
    if (
        comments_requested
        and platform in {"naver_blog", "web"}
        and "comments_label" not in meta
        and not result.get("comments")
    ):
        meta["comments_label"] = "unsupported"
    if meta is not result.get("meta"):
        return {**result, "meta": meta}
    return result
