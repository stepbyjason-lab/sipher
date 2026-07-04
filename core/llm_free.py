r"""
sipher 무료 비전 API 클라이언트 — Gemini OCR.

`docs/01-overview.md` §8 PoC(2026-07-01, `scratchpad/ocr_poc.py`)에서 무료 비전
5종(Gemini·Cloudflare·NIM·Mistral·OpenRouter) 중 **Gemini 2.5 Flash가 1위**로
확정됐다(무환각, 한국어 카드뉴스 ~95%+ 정확도). 이 모듈은 그 PoC 패턴을
core 레이어용으로 재작성·하드닝한 것이다 — PoC 스크립트를 그대로 import하지
않는다(1회성 실험 스크립트라 계약 표면이 없음).

설계 원칙:
- **graceful degradation**: `GEMINI_API_KEY`가 없으면 예외를 던지지 않고
  `is_available() -> False`로 명확히 신호한다(§10 "없는 provider graceful skip").
- **silent 폴백 금지**: 실제 응답에 쓰인 모델명을 항상 결과에 싣는다(§8 방법론
  교훈 — "최선 가용 모델 사용 + 실제 응답 모델 로깅").
- **키 값 절대 로그·출력 금지**: 로그·예외 메시지 어디에도 API 키 원문을 남기지
  않는다.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from .lang import resolve_lang

__all__ = ["ocr_image", "is_available", "OcrError"]

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MODEL = "gemini-2.5-flash"
_TIMEOUT_SECONDS = 60
_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 3

# ko는 round-03 PoC(한국어 카드뉴스 ~95%+)로 검증된 프롬프트를 그대로 보존한다.
# 그 외 언어는 언어중립 영어 프롬프트(round-20, SIPHER_LANG 자동감지).
_PROMPT_KO = (
    "이 이미지에 있는 모든 한국어 텍스트를 빠짐없이·정확히 추출해라. "
    "카드 내 순서/구조 유지. 설명·해석 없이 텍스트만 출력."
)
_PROMPT_GENERIC = (
    "Extract ALL text in this image exactly and completely. "
    "Preserve the original order and structure. "
    "Output only the extracted text, no commentary or interpretation."
)


def _build_prompt(lang: str) -> str:
    """사용자 언어(SIPHER_LANG)에 맞는 OCR 프롬프트. ko=검증본, 그 외=범용 영어."""
    return _PROMPT_KO if lang == "ko" else _PROMPT_GENERIC


class OcrError(RuntimeError):
    """OCR 호출 실패(네트워크·타임아웃·rate-limit·API 오류 등)."""


def _load_env_file(path: Path) -> dict[str, str]:
    """`.env.local` 형식(`KEY=VALUE`, `#` 주석)을 dict로 읽는다. 파일 없으면 빈 dict."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            env[key] = value
    return env


def _config() -> tuple[str | None, str]:
    """(api_key, model). `.env.local` → os.environ 순으로 단일 `GEMINI_API_KEY` 조회.

    ※ 멀티계정 무료한도 우회는 provider ToS 위반이라 지원하지 않는다 — 키는 1개만.
    """
    import os

    env = _load_env_file(_ROOT / ".env.local")
    key = env.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
    model = env.get("GEMINI_MODEL") or os.environ.get("GEMINI_MODEL") or _DEFAULT_MODEL
    return key, model


def is_available() -> bool:
    """Gemini OCR 사용 가능 여부(키 존재만 확인, 네트워크 호출 없음)."""
    key, _ = _config()
    return bool(key)


class _QuotaExhausted(OcrError):
    """**영구** quota 소진(일일 RPD / 크레딧 소진) 신호.

    앙상블이 이 예외를 "이 provider는 이 세션 동안 끝남"으로 보고 **dead 마킹**해
    다른 provider로 사다리를 내려간다.
    """


class _RateLimited(OcrError):
    """**일시** rate-limit(분당 RPM 초과 등, 곧 회복) 신호.

    dead 마킹 대상이 아니다 — 앙상블은 이 provider를 이번 이미지만 skip하고
    다음 이미지에서 다시 시도한다(RPM 창이 지나면 부활). 일시 rate-limit을 영구
    소진으로 오판해 성급히 유료로 넘어가지 않게 하는 것이 목적(토큰 절약 원칙).
    """


def _classify_429(resp) -> type[OcrError]:
    """429/403 응답 → `_QuotaExhausted`(영구) 또는 `_RateLimited`(일시).

    Google 응답의 QuotaFailure.violations[].quotaId에 'PerDay'가 있으면 일일 소진(영구),
    'PerMinute'거나 판별 불가면 일시(RPM)로 본다(모호할 땐 provider를 죽이지 않는 쪽).
    """
    try:
        for det in (resp.json().get("error", {}) or {}).get("details", []):
            if "QuotaFailure" in det.get("@type", ""):
                for v in det.get("violations", []):
                    qid = (v.get("quotaId", "") + v.get("quotaMetric", "")).lower()
                    if "perday" in qid:
                        return _QuotaExhausted
    except Exception:  # noqa: BLE001 — 파싱 실패 시 보수적으로 일시 취급
        pass
    return _RateLimited


def _b64_of(path: Path) -> str:
    import base64

    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _redact(text: str, secret: str | None) -> str:
    """`text`에서 `secret`(API 키 등) 부분 문자열을 마스킹한다.

    방어심층(defense-in-depth) 유틸 — 근본 픽스는 키를 URL에 아예 싣지 않는
    것(헤더 전달)이지만, 그래도 예외/응답 문자열 어딘가에 키가 섞여 들어올
    가능성을 차단한다. `secret`이 없거나 빈 문자열이면 원문을 그대로 반환한다.
    """
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


def _call_gemini(image_path: Path, *, api_key: str, model: str,
                 prompt: str | None = None) -> str:
    # 키는 URL 쿼리스트링이 아니라 `x-goog-api-key` 헤더로 전달한다 — URL은
    # requests 예외 문자열(Timeout/ConnectionError 등)에 그대로 실리기 때문에,
    # 쿼리스트링에 키를 넣으면 실패 로그에 키가 상시 유출된다.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    body = {
        "contents": [
            {
                "parts": [
                    {"text": prompt if prompt is not None else _build_prompt(resolve_lang())},
                    {"inline_data": {"mime_type": mime, "data": _b64_of(image_path)}},
                ]
            }
        ]
    }

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=_TIMEOUT_SECONDS)
        except requests.exceptions.Timeout as e:
            last_err = e
            _log.warning("Gemini OCR 타임아웃(시도 %d/%d): %s", attempt + 1, _MAX_RETRIES, image_path.name)
        except requests.exceptions.RequestException as e:
            last_err = e
            _log.warning("Gemini OCR 네트워크 오류(시도 %d/%d): %s", attempt + 1, _MAX_RETRIES, image_path.name)
        else:
            if resp.status_code == 429 or (
                resp.status_code == 403 and "RESOURCE_EXHAUSTED" in resp.text
            ):
                # 일시(RPM)와 영구(RPD 일일) 구분 — 일시면 dead 마킹 안 함(앙상블이 다음
                # 이미지에서 재시도). 재시도로 안 풀리므로 즉시 승격.
                raise _classify_429(resp)(f"Gemini rate/quota(HTTP {resp.status_code}): {image_path.name}")
            elif resp.status_code >= 500:
                last_err = OcrError(f"Gemini 서버 오류(HTTP {resp.status_code}): {image_path.name}")
                _log.warning(
                    "Gemini OCR 서버 오류(시도 %d/%d) HTTP %d: %s",
                    attempt + 1, _MAX_RETRIES, resp.status_code, image_path.name,
                )
            elif resp.status_code != 200:
                # 4xx(429 제외) — 키/요청 형식 문제일 가능성이 높아 재시도해도 소용없다.
                raise OcrError(f"Gemini OCR 실패(HTTP {resp.status_code}): {_redact(resp.text[:300], api_key)}")
            else:
                data = resp.json()
                try:
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                except (KeyError, IndexError) as e:
                    raise OcrError(f"Gemini 응답 형식 이상: {e}") from e

        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_DELAY_SECONDS)

    raise OcrError(
        f"Gemini OCR 재시도 소진({_MAX_RETRIES}회): {_redact(str(last_err), api_key)}"
    ) from last_err


def ocr_image(path: str | Path, *, prompt: str | None = None) -> dict:
    """이미지 파일 → `{"text": str, "model": str}`.

    `prompt` 지정 시 기본 OCR 프롬프트 대신 사용(앙상블 judge가 이 통로로 재사용 —
    기본 None이면 기존 동작과 동일).

    단일 `GEMINI_API_KEY`만 쓴다(멀티계정 우회 없음). quota 소진 시 `_QuotaExhausted`를
    던져 앙상블이 다른 provider로 사다리를 내려가게 한다. provider(키) 없으면 `OcrError`
    — 호출자가 `is_available()`로 먼저 확인해 degrade하는 것을 전제로 한다. 파일 부재도 `OcrError`.
    """
    image_path = Path(path)
    if not image_path.exists():
        raise OcrError(f"이미지 파일이 없습니다: {image_path}")

    api_key, model = _config()
    if not api_key:
        raise OcrError("GEMINI_API_KEY가 설정되지 않았습니다")

    # quota 소진(429/RESOURCE_EXHAUSTED)은 _call_gemini가 _QuotaExhausted로 던진다 —
    # 여기서 잡지 않고 그대로 전파해 앙상블이 provider dead 마킹하게 한다. 키 값은 로그에 안 남김.
    text = _call_gemini(image_path, api_key=api_key, model=model, prompt=prompt)
    _log.info("Gemini OCR 완료: %s (model=%s)", image_path.name, model)
    return {"text": text, "model": model}
