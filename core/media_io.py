r"""core 공통 미디어 write 유틸 — round-33.

instagram·threads가 `open(dest, "wb")` + `resp.read()`로 최종 경로에 직접 써서,
재fetch 중 네트워크가 죽으면 이미 받아둔 캐시 미디어(SNS URL은 수 시간 내 만료라
사실상 유일본)가 0바이트로 잘려 영구 파괴되는 문제가 있었다. naver_blog는
`_atomic_write`/`_download_raw`(tmp 쓰기 → 성공 시에만 rename)로 이미 이 문제를
막아뒀다 — 이 모듈은 그 검증된 패턴을 3어댑터 공통 정본으로 승격한 것이다.

계약:
- tmp 파일에 쓰고 **성공 시에만** `tmp.replace(out)`으로 원자적 치환한다.
- 실패(예외·상한 초과)하면 tmp를 정리하고 최종 경로(out)는 절대 건드리지 않는다
  — 기존 캐시가 있었다면 그대로 무손상 유지된다.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

_log = logging.getLogger(__name__)

_CHUNK = 1 << 16

# 로그용 URL redact — 쿼리스트링(서명 파라미터 등 비밀 가능)을 제거한다.
_QUERY_RE = re.compile(r"\?.*$")

# Content-Length 토큰 검증 — ASCII 십진 숫자만(부호·유니코드 숫자 배제, iter3 P2-1).
# `int()`는 `+10`·`１０`(전각 숫자)도 파싱해버려 fail-closed 명세와 어긋난다.
_ASCII_DIGITS_RE = re.compile(r"[0-9]+")


def _redact(url: str) -> str:
    """로그용: 쿼리(서명 파라미터) 제거 — 서명 URL 전체를 로그에 남기지 않는다."""
    return _QUERY_RE.sub("", url)


def _make_tmp_path(out: Path) -> Path:
    """out과 같은 디렉터리(같은 파일시스템 — rename 원자성 보장)에 **시도별 고유**
    tmp 경로를 만든다(round-33 iter2 P2 — 고정 `.tmp` 이름은 같은 out을 향한
    동시 write가 서로의 tmp를 덮어쓰거나 지울 수 있었다). mkstemp가 파일을 실제로
    만들어두므로 반환된 fd는 즉시 닫고, 이후 쓰기는 이 경로를 열어서 한다.
    """
    parent = out.parent if str(out.parent) else Path(".")
    fd, name = tempfile.mkstemp(
        dir=str(parent), prefix=out.stem + ".", suffix=out.suffix + ".tmp"
    )
    os.close(fd)
    return Path(name)


def _cleanup_tmp(tmp: Path) -> None:
    """실패 공통: 자기 tmp만 정리 — 고유 이름이라 남의 동시 write를 건드리지 않는다."""
    try:
        tmp.unlink(missing_ok=True)
    except OSError as ue:
        _log.debug("tmp 정리 실패 %s — %s", tmp.name, type(ue).__name__)


def atomic_write(out: Path, data: bytes) -> bool:
    """이미 확보한 bytes를 원자적으로 쓴다. tmp에 쓰고 성공 시에만 rename.

    실패(디스크 풀/권한·**tmp 생성 자체 실패** 등) 시 tmp를 정리하고 out(기존
    캐시가 있다면 그것)은 무손상으로 남는다. round-33 iter3 P1: `_make_tmp_path`가
    `try` 밖에 있으면 부모 디렉터리 부재 등으로 mkstemp가 던지는 예외가 계약(False
    반환)을 어기고 호출자까지 전파됐다 — tmp 생성도 try 안으로 옮겨 방어한다.
    """
    tmp: Path | None = None
    try:
        tmp = _make_tmp_path(out)
        tmp.write_bytes(data)
        tmp.replace(out)
        return True
    except OSError as exc:
        # 디스크 풀/권한·부모 디렉터리 부재 등 시스템 이슈 — 네트워크 실패와
        # 구분되게 warning(로컬 경로는 비밀이 아니므로 그대로 로그해도 안전).
        _log.warning("파일 쓰기 실패 %s — %s: %s", out.name, type(exc).__name__, exc)
    except Exception as exc:  # 예상 못 한 오류(코드 버그) — 가시화
        _log.warning("파일 쓰기 예외 %s — %s", out.name, type(exc).__name__)
        _log.debug("파일 쓰기 예외 상세", exc_info=True)
    if tmp is not None:  # tmp 생성 자체가 실패했으면(None) 정리할 것도 없음
        _cleanup_tmp(tmp)
    return False


class _DownloadTooLarge(Exception):
    """스트림이 max_bytes를 초과 — download_to_file 내부 신호(상한 시점에 이미 warning)."""


def download_to_file(
    url: str,
    out: Path,
    *,
    headers: dict[str, str],
    timeout: int = 60,
    max_bytes: int,
    host_pattern: re.Pattern[str] | None = None,
) -> bool:
    """URL을 chunk 스트리밍으로 받아 원자적으로 out에 쓴다.

    - `host_pattern`이 주어지면(compiled regex) URL이 매치하지 않으면 즉시 skip
      (SSRF 방어 — 호출 전 이미 검증됐어도 여기서 2중 방어).
    - `max_bytes` 초과 시 즉시 중단, 부분 tmp를 정리하고 out은 무손상.
    - **조기 EOF 방어(round-33 iter2 P0, iter3 P0 강화)**: `resp.read(n)`은 서버가
      Content-Length보다 짧게 보내고 정상 close해도 예외 없이 짧은 데이터 뒤 b""를
      반환한다 — 그대로 두면 부분 응답이 "성공"으로 커밋돼 기존 완전 캐시를 잘린
      파일로 덮어쓴다. 그래서 Content-Length 헤더를 3-state로 분류한다
      (`_content_length_state`):
        - **absent**(헤더 자체가 없음) — close-delimited 응답으로 간주, 검증 불가라
          기존대로 관대 커밋(스트림이 예외 없이 끝까지 오면 성공).
        - **valid**(모든 값이 동일한 음이 아닌 정수) — 그 정수를 선언 길이로 삼아
          (a) 읽기 전: 선언 길이가 max_bytes를 넘으면 한 바이트도 안 읽고 거부.
          (b) 스트림 완료 후: 실제 받은 바이트 수가 선언 길이와 다르면(조기 EOF)
          실패로 처리하고 tmp를 버린다.
        - **invalid**(헤더는 있는데 정수 아님·음수·서로 다른 다중값·빈 값 —
          예: `"10, 10"`을 그냥 int() 캐스팅하면 실패해 "헤더 없음"으로 강등되고
          조기 EOF 방어가 통째로 우회되는 회귀가 있었다) — 응답을 신뢰하지 않고
          **읽기 전에 즉시 거부**한다. 관대 커밋은 absent에 한정되고, 헤더가
          있는데 파싱 불가능하면 거부가 기본값이다.
      CDN 미디어는 보통 Content-Length를 정상적으로 주므로 실무 케이스는 valid
      경로가 커버한다.
    - 성공(HTTP 200 + 상한 이내로 스트림 완료 + 길이 검증 통과) 시에만 tmp를
      out으로 rename하고 True를 반환한다. 그 외 모든 경로는 False — out은 절대
      건드리지 않는다.
    """
    if host_pattern is not None and not host_pattern.match(url):
        _log.debug("다운로드 호스트 비허용 — skip: %s", _redact(url))
        return False

    req = urllib.request.Request(url, headers=headers)
    tmp: Path | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                _log.debug("미디어 HTTP %s — skip %s", resp.status, _redact(url))
                return False

            resp_headers = getattr(resp, "headers", None)
            cl_state, declared_length = _content_length_state(resp_headers)
            if cl_state == "invalid":
                # 헤더는 있는데 파싱 불가(비정수·음수·서로 다른 다중값·빈 값) —
                # "헤더 없음(관대 커밋)"으로 강등하면 조기 EOF 방어가 우회된다.
                # 응답을 신뢰하지 않고 읽기 전에 거부.
                _log.warning(
                    "Content-Length 비정상 — 커밋 거부 %s", _redact(url)
                )
                return False
            if cl_state == "valid" and declared_length > max_bytes:
                _log.warning(
                    "미디어 선언 크기(%dB)가 상한(%dB) 초과 — 읽기 전 거부 %s",
                    declared_length, max_bytes, _redact(url),
                )
                return False

            tmp = _make_tmp_path(out)
            total = 0
            with tmp.open("wb") as fh:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        _log.warning(
                            "미디어 상한(%dB) 초과 — 중단 %s", max_bytes, _redact(url)
                        )
                        raise _DownloadTooLarge
                    fh.write(chunk)

            if cl_state == "valid" and total != declared_length:
                _log.warning(
                    "미디어 조기 EOF(선언 %dB, 수신 %dB) — 부분본 커밋 거부 %s",
                    declared_length, total, _redact(url),
                )
                _cleanup_tmp(tmp)
                return False

        tmp.replace(out)
        return True
    except _DownloadTooLarge:
        pass  # 이미 warning 로그됨
    except urllib.error.HTTPError as exc:  # 4xx/5xx — 흔함, debug(노이즈 회피)
        _log.debug("미디어 HTTP %s — %s", exc.code, _redact(url))
    except OSError as exc:  # 네트워크(URLError)·디스크 풀/권한 — 운영 이슈는 가시화
        _log.warning("미디어 다운로드 오류 %s — %s", _redact(url), type(exc).__name__)
    except Exception as exc:  # 예상 못 한 오류(코드 버그) — 가시화
        _log.warning("미디어 다운로드 예외 %s — %s", _redact(url), type(exc).__name__)
        _log.debug("미디어 다운로드 예외 상세", exc_info=True)
    if tmp is not None:  # 실패 공통: 자기 tmp만 정리 — out(기존 캐시)은 절대 안 건드림
        _cleanup_tmp(tmp)
    return False


def _content_length_state(headers: object) -> tuple[str, int | None]:
    """Content-Length 헤더를 3-state로 분류한다: ('absent'|'valid'|'invalid', 선언길이).

    round-33 iter3 P0 — 예전엔 비정수·음수·다중값을 전부 `int()` 캐스팅 실패로
    처리해 "헤더 없음(absent, 관대 커밋)"으로 강등시켰다. 그 결과 `"10, 10"` 같은
    비정상 헤더가 오면 조기 EOF 방어가 통째로 우회됐다(캐스팅 실패 → absent 오판
    → 검증 스킵 → 부분 응답 그대로 커밋). 이제 "헤더가 아예 없음"과 "헤더는
    있는데 못 믿음"을 구분한다 — 후자는 관대 커밋 대상이 아니라 거부 대상이다.

    - absent: 헤더 자체가 없음(get()/get_all() 모두 None) → (None 선언길이)
      close-delimited로 간주, 관대 커밋.
    - valid: 헤더에 실린 모든 값(콤마로 분리한 토큰 포함)이 ASCII 십진 숫자
      토큰(`[0-9]+`)이고 전부 동일한 값 → 그 정수.
    - invalid: 그 외(빈 리스트·비-ASCII 숫자·`+`/`-` 부호·비정수·서로 다른
      다중값·빈 토큰) → 응답 신뢰 안 함, 호출부가 즉시 거부해야 한다.
    """
    if headers is None:
        return "absent", None

    get_all = getattr(headers, "get_all", None)
    raw_values = get_all("Content-Length", None) if get_all is not None else None
    if raw_values is None:  # get_all 미지원 fake 등 — 단일 get()으로 폴백
        single = headers.get("Content-Length")
        raw_values = [single] if single is not None else None
    if raw_values is None:  # iter3 P2-2: 진짜 "헤더 없음"만 absent.
        return "absent", None
    if not raw_values:  # get_all이 빈 리스트([]) — 헤더는 있는데 값이 없는 이상 상태.
        return "invalid", None

    tokens: list[str] = []
    for v in raw_values:
        tokens.extend(part.strip() for part in v.split(","))

    if not tokens:
        return "invalid", None

    parsed: set[int] = set()
    for t in tokens:
        # iter3 P2-1: int()는 `+10`(HTTP 문법상 무효)·유니코드 십진수(`１０`)도
        # 파싱해버린다 — ASCII 숫자만 허용하는 정규식으로 먼저 걸러낸다.
        if not _ASCII_DIGITS_RE.fullmatch(t):
            return "invalid", None
        parsed.add(int(t))

    if len(parsed) != 1:  # 서로 다른 다중값
        return "invalid", None
    return "valid", next(iter(parsed))
