r"""
sipher-naver-blog 이미지 회수 — 호스트별 분기 (2026-06-30 spike 반영).

자작 수집 도구(비공개)는 모든 호스트에 ?type=w966을 강제해 (a) blogfiles 원본을 404로 놓치고
(b) 모바일 mblogthumb 캡에 묶였다. spike 실측 결과 콘텐츠 호스트가 정반대로 동작:

| 호스트                | bare(쿼리 제거) | w966   | w3840          | 회수 전략              |
|-----------------------|-----------------|--------|----------------|------------------------|
| postfiles  (SE3 본문) | placeholder     | 천장   | =w966          | w966 (원본 미노출)     |
| blogfiles  (배너·첨부)| **원본**        | 404    | **원본**       | bare → w3840           |
| blogthumb/mblogthumb  | placeholder/캡  | 캡     | -              | w966                   |

따라서 호스트별로 시도 순서를 다르게 둔다. 설계: adapters/naver-blog/docs/00-overview.md §1·§4.

실패 구분(silent failure 방지): fetch_image는 '없음(404/placeholder)'과 '오류(타임아웃·차단·SSL)'를
서로 다른 label("failed:notfound" vs "failed:error")로 돌려준다 — 호출 측이 조용히 누락하지 않게.
"""
from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable

_log = logging.getLogger(__name__)

_CDN_RE = re.compile(
    r"^https?://(?P<host>(?:postfiles|blogfiles|blogthumb|mblogthumb)(?:-phinf)?)"
    r"\.pstatic\.net/",
    re.I,
)

# placeholder(작은 stub) 거르는 하한. postfiles bare placeholder ≈ 8.5KB(100px)이라
# 크기만으론 못 거른다 → 호스트별 plan으로 애초에 bare를 회피하는 게 1차 방어.
_MIN_BYTES = 1500
# 응답 크기 상한 — 악성/오작동 CDN의 대용량 응답으로 인한 메모리 고갈 차단.
_MAX_BYTES = 64 << 20  # 64 MB
_CHUNK = 1 << 16

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36"
)

# 시도 status 분류: 진짜 오류로 볼 코드(차단·서버·네트워크)
_ERROR_STATUSES = {-1, 403, 408, 429, 500, 502, 503, 504}


def cdn_host(url: str) -> str | None:
    """blog 이미지 CDN 호스트 prefix 또는 None.

    반환값엔 `-phinf` suffix가 붙을 수 있다(예: 'mblogthumb-phinf'). 비교는 startswith로 한다.
    `re.I` + `.lower()`로 대소문자 무관 정규화.
    """
    m = _CDN_RE.match(url)
    return m.group("host").lower() if m else None


def is_blog_image(url: str) -> bool:
    return cdn_host(url) is not None


def _apply_type(url: str, type_q: str | None) -> str:
    base = url.split("?", 1)[0]
    return base if type_q is None else f"{base}?type={type_q}"


def _redact(url: str) -> str:
    """로그용: 쿼리(서명 파라미터) 제거 — 불변식 2(서명 URL 전체를 로그에 안 남김)."""
    return url.split("?", 1)[0]


def size_plan(url: str) -> list[tuple[str | None, str]]:
    """호스트별 (type_param, size_label) 시도 순서. 첫 성공을 채택. 항상 1개 이상 반환."""
    host = cdn_host(url) or ""
    if host.startswith("blogfiles"):
        # 원본 노출 호스트: bare=원본, w3840 폴백(원본>3840이면 3840)
        # 라벨을 분리해 어느 변형으로 회수됐는지 호출 측이 구분 가능하게.
        return [(None, "original"), ("w3840", "original_w3840")]
    if host.startswith(("postfiles", "blogthumb", "mblogthumb")):
        # 인라인/썸네일: w966이 천장, bare=placeholder → bare 회피
        return [("w966", "w966_ceiling")]
    # 알 수 없는 호스트: bare(placeholder 위험) 회피, w966 단일 시도만
    return [("w966", "thumbnail_fallback")]


def _http_get(url: str, timeout: int = 30) -> tuple[int, bytes]:
    """(status, body). 네트워크/타임아웃 오류는 status=-1로 표시하되 로그를 남긴다(silent 금지)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Referer": "https://blog.naver.com/",
        "Accept": "*/*", "Accept-Language": "ko,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            chunks: list[bytes] = []
            total = 0
            truncated = False
            while True:
                chunk = r.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_BYTES:  # 상한 초과(메모리 고갈 차단) — 절단 이미지는 손상
                    _log.warning("이미지 응답 상한(%dB) 초과 — 손상, 폐기 %s",
                                 _MAX_BYTES, _redact(url))
                    truncated = True
                    break
                chunks.append(chunk)
            if truncated:  # 손상 이미지를 유효로 채택하지 않게 실패 처리(다음 변형 시도)
                return -2, b""
            return r.status, b"".join(chunks)  # r.status: HTTPResponse(3.0+)
    except urllib.error.HTTPError as e:
        # (이 분기에 URL 로그 추가 시 반드시 _redact(e.url) 사용 — 불변식 2)
        try:
            e.read()  # body 소비해 연결 정상 종료(403/429 keep-alive 보호)
        except Exception as body_err:  # 're'(import re) 섀도잉 회피
            _log.debug("HTTPError body 소비 실패 — %s", type(body_err).__name__)
        return e.code, b""
    except urllib.error.URLError as e:
        _log.warning("이미지 요청 네트워크 오류 %s — %s", _redact(url), type(e).__name__)
        _log.debug("네트워크 오류 상세 — %s", e.reason)  # reason은 인프라 정보 가능 → debug
        return -1, b""
    except Exception as exc:  # 예상 못 한 오류 (코드 버그 식별용)
        # warning 라인은 redact url + type만(로그 집계 유출 방지),
        # 전체 traceback(메시지 포함)은 debug에만 → 보안·진단 동시 충족
        _log.warning("이미지 요청 예외 %s — %s", _redact(url), type(exc).__name__)
        _log.debug("이미지 요청 예외 상세", exc_info=True)
        return -1, b""


def fetch_image(url: str, *, min_bytes: int = _MIN_BYTES,
                getter: Callable[[str], tuple[int, bytes]] | None = None
                ) -> tuple[bytes | None, str, str | None]:
    """호스트별 plan 순서로 시도해 첫 유효 이미지를 회수.

    반환: (bytes|None, label, 사용한 URL|None).
      성공 → (data, size_label, url)
      실패 → (None, "failed:error", None)   진짜 오류(차단·타임아웃·서버) 1회 이상
             (None, "failed:notfound", None) 전부 404/placeholder(정상 '없음')
    getter: (url) -> (status, bytes) 콜백(테스트/세션 주입용). 기본 urllib.
    """
    get = getter or _http_get
    if cdn_host(url) is None:
        _log.warning("알 수 없는 CDN 호스트 — 폴백 plan 사용: %s", _redact(url))
    statuses: list[int] = []
    for type_q, label in size_plan(url):
        candidate = _apply_type(url, type_q)
        status, data = get(candidate)
        statuses.append(status)
        if status == 200 and len(data) >= min_bytes:
            return data, label, candidate
        if status == 200 and len(data) < min_bytes:
            _log.debug("placeholder/소형 응답 무시 (%d bytes): %s", len(data), _redact(candidate))
    had_error = any(s in _ERROR_STATUSES for s in statuses)
    reason = "failed:error" if had_error else "failed:notfound"
    _log.debug("이미지 회수 실패 %s — 시도 status=%s → %s", _redact(url), statuses, reason)
    return None, reason, None


def best_url(url: str) -> str:
    """다운로드 없이 '가장 큰 변형 URL'만 필요할 때(예: 메타 기록). plan 첫 항목."""
    plan = size_plan(url)
    if not plan:  # 불변식 위반 — -O 빌드에서도 무음 실패 안 하게 명시 raise
        raise RuntimeError(f"size_plan이 빈 리스트 반환: {_redact(url)}")
    type_q, _label = plan[0]
    return _apply_type(url, type_q)
