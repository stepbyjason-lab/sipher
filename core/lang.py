r"""
sipher 사용자 언어 결정(SIPHER_LANG) — round-20.

첫 실행 시 OS locale을 감지해 `.env.local`에 `SIPHER_LANG=<code>`로 저장하고,
이후에는 그 값을 쓴다. 사용자는 파일을 열어 언제든 바꿀 수 있다.

우선순위: os.environ SIPHER_LANG → .env.local SIPHER_LANG → OS locale 감지
(감지 성공 시 .env.local에 1회 기록) → "en" fallback.

설계 원칙(계약 round-20):
- 저장 실패(권한 등)가 기능을 막지 않는다 — 감지값을 그대로 반환(degrade).
- `.env.local` 기록은 append-only — 기존 내용(API 키 등)을 절대 훼손하지 않는다.
- locale 값은 `[a-z]{2,3}` primary subtag로 검증 — 임의 문자열이 프롬프트/CLI
  인자로 흘러들지 않게 한다.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

__all__ = ["resolve_lang"]

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _ROOT / ".env.local"
_FALLBACK = "en"
_SUBTAG_RE = re.compile(r"^[a-z]{2,3}$")

_cached: str | None = None  # 프로세스당 1회 결정


def _normalize(raw: str | None) -> str | None:
    """locale 문자열(`ko-KR`/`en_US.UTF-8`/`Korean_Korea` 등) → primary subtag 또는 None."""
    if not raw:
        return None
    token = raw.strip().split(".")[0]  # 인코딩 접미(.UTF-8) 제거
    token = re.split(r"[-_]", token)[0].strip().lower()
    if _SUBTAG_RE.fullmatch(token):
        return token
    # Windows 구형 표기("Korean_Korea" → "korean") 방어 — 알려진 영어 이름 최소 매핑
    known = {"korean": "ko", "english": "en", "japanese": "ja", "chinese": "zh",
             "german": "de", "french": "fr", "spanish": "es"}
    return known.get(token)


def _read_env_file() -> str | None:
    """`.env.local`의 SIPHER_LANG 값(정규화 후) 또는 None. 파일 없거나 파싱 실패 시 None."""
    try:
        if not _ENV_FILE.exists():
            return None
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "SIPHER_LANG":
                return _normalize(value.strip().strip('"').strip("'"))
    except OSError as exc:
        _log.debug(".env.local 읽기 실패(무시) — %s", type(exc).__name__)
    return None


def _detect_os_locale() -> str | None:
    """OS locale → primary subtag 또는 None. 실패해도 예외를 밖으로 안 던진다."""
    # 1) win32: GetUserDefaultLocaleName("ko-KR" 류) — deprecation 없는 현대 API
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(85)
            if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
                lang = _normalize(buf.value)
                if lang:
                    return lang
        except Exception as exc:  # ctypes 부재/권한 등 — 다음 방법으로
            _log.debug("win32 locale 감지 실패(다음 방법) — %s", type(exc).__name__)
    # 2) POSIX/공통 env
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        lang = _normalize(os.environ.get(var))
        if lang:
            return lang
    # 3) 최후: locale 모듈(3.13+ deprecated — 경고 무시하고 시도)
    try:
        import locale
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            raw = locale.getdefaultlocale()[0]  # noqa: B028
        return _normalize(raw)
    except Exception as exc:
        _log.debug("locale 모듈 감지 실패 — %s", type(exc).__name__)
    return None


def _persist(lang: str) -> None:
    """감지된 lang을 `.env.local`에 append 기록(첫 실행 1회). 실패는 degrade — 예외 안 던짐."""
    try:
        prefix = ""
        if _ENV_FILE.exists():
            existing = _ENV_FILE.read_text(encoding="utf-8")
            if "SIPHER_LANG" in existing:  # 경합 방어 — 이미 있으면 손대지 않음
                return
            prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with _ENV_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{prefix}# sipher 언어(OS locale 자동감지, 첫 실행 시 기록 — 자유롭게 수정)\n"
                    f"SIPHER_LANG={lang}\n")
        _log.info("SIPHER_LANG=%s 를 .env.local에 기록(첫 실행 감지)", lang)
    except OSError as exc:
        _log.debug(".env.local 기록 실패(감지값은 그대로 사용) — %s", type(exc).__name__)


def resolve_lang() -> str:
    """사용자 언어 코드(primary subtag, 예: "ko"/"en"). 우선순위는 모듈 docstring 참조."""
    global _cached
    if _cached is not None:
        return _cached
    lang = _normalize(os.environ.get("SIPHER_LANG"))
    if lang is None:
        lang = _read_env_file()
    if lang is None:
        lang = _detect_os_locale()
        if lang is not None:
            _persist(lang)
    if lang is None:
        lang = _FALLBACK
    _cached = lang
    return lang
