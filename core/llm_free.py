r"""
sipher 무료 비전 API 클라이언트 — Gemini OCR.

`docs/01-overview.md` §8 PoC(2026-07-01, `scratchpad/ocr_poc.py`)에서 무료 비전
5종(Gemini·Cloudflare·NIM·Mistral·OpenRouter) 중 당시 **Gemini 2.5 Flash가 1위**로
확정됐다(무환각, 한국어 카드뉴스 ~95%+ 정확도). 현재 기본 모델은 Google의 안정
멀티모달 후속 모델인 Gemini 3.6 Flash다. 이 모듈은 그 PoC 패턴을
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
import math
import time
from pathlib import Path

import requests

from .lang import resolve_lang

__all__ = ["ocr_image", "is_available", "OcrError"]

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_MODEL = "gemini-3.6-flash"
_TIMEOUT_SECONDS = 60
_MAX_RETRIES = 2
_RETRY_DELAY_SECONDS = 3

# OCR 추출 범위는 사용자 언어(SIPHER_LANG)와 무관하게 항상 "언어 불문 전량"이다 —
# resolve_lang()은 지시문의 언어(가독성)만 정하지, 추출 대상 언어를 한정하지 않는다.
# (2026-07-13 실측 수정: 이전 ko 프롬프트가 "모든 한국어 텍스트"로 범위를 한정해
#  다국어 카드뉴스의 영어 프롬프트 박스를 통째로 떨궜다 — 동일 이미지·동일 모델
#  (Gemini)에서 KO 프롬프트 eng_alpha=0 vs 언어중립 eng_alpha=253으로 실증.
#  round-03 ko 검증은 한국어 단일 카드 대상이었고 영어 병기 슬라이드는 미커버였다.)
_PROMPT_KO = (
    "이 이미지에 있는 모든 텍스트를 언어에 상관없이(한국어·영어·숫자·기호 모두) "
    "빠짐없이·정확히 추출해라. 카드 내 순서/구조 유지. 설명·해석 없이 텍스트만 출력."
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

    round-28: `PerDay` quotaId만으로는 더 이상 영구 소진의 충분조건이 아니다
    (2026-07-04 실측 — `quotaId=...PerDay...`와 `RetryInfo.retryDelay=44s`가
    공존했고 수십 분 뒤 정상화됨). `RetryInfo.retryDelay`가 없는 `PerDay`만
    이 클래스의 후보로 남긴다(`_classify_429` 참조).
    """


class _RateLimited(OcrError):
    """**일시** rate-limit(분당 RPM 초과, 또는 retryDelay가 명시된 PerDay 포함) 신호.

    dead 마킹 대상이 아니다 — OCR 사다리(`core/ocr_ensemble.py`)가 provider별
    쿨다운 상태로 관리하며, `retry_after`(초)가 있으면 그 시간만큼, 없으면 지수
    백오프로 쉬었다가 자동 부활시킨다. 일시 rate-limit을 영구 소진으로 오판해
    성급히 유료로 넘어가지 않게 하는 것이 목적(토큰 절약 원칙).

    retry_after 소비(대기/재시도)는 `core/ocr_ensemble.py` 단일 지점에서만
    일어난다 — 이 모듈(`_call_gemini`)은 429를 즉시 raise하고 자체 재시도를
    추가하지 않는다(이중 재시도 금지).
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _clamp_delay(value: float | None) -> float | None:
    """파싱된 delay(초)를 검증: 0 이하이거나 유한하지 않으면(NaN/inf) None 폴백.

    음수 delay는 `cooling_until = now + 음수`로 즉시 만료(쿨다운 무효화)를,
    NaN은 모든 비교를 False로 만들어 쿨다운을 아예 우회시킨다 — 둘 다 사다리의
    지수 백오프로 대체하는 것이 안전하다(리뷰 CORR-R1).
    """
    if value is None:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _parse_retry_delay(raw: str | int | float | None) -> float | None:
    """Google `RetryInfo.retryDelay` 값(예: `"44s"`, `"3.5s"`, `"1m"`)을 초(float)로 파싱.

    실패·인식 불가·비정상 값(음수/0/NaN/inf)이면 예외를 던지지 않고 `None`을
    반환한다 — 그러면 호출측(OCR 사다리)이 지수 백오프로 대신 처리한다.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return _clamp_delay(float(raw))
        except (TypeError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        if text.endswith("ms"):
            parsed = float(text[:-2]) / 1000.0
        elif text.endswith("s"):
            parsed = float(text[:-1])
        elif text.endswith("m"):
            parsed = float(text[:-1]) * 60.0
        elif text.endswith("h"):
            parsed = float(text[:-1]) * 3600.0
        else:
            parsed = float(text)
    except (TypeError, ValueError):
        return None
    return _clamp_delay(parsed)


def _classify_429(resp) -> tuple[type[OcrError], float | None]:
    """429/403 응답 → `(_QuotaExhausted, None)` 또는 `(_RateLimited, retry_after)`.

    round-28 개정: Google 응답의 QuotaFailure.violations[].quotaId에 'PerDay'가
    있어도, 같은 응답에 `RetryInfo.retryDelay`가 실려 있으면 일시(`_RateLimited`)로
    분류한다(2026-07-04 실측 — PerDay + retryDelay=44s 공존 후 정상화 관측).
    **`RetryInfo.retryDelay` 필드 자체가 없는 `PerDay`만** 영구 소진
    (`_QuotaExhausted`) 후보로 남긴다 — retryDelay가 raw로 **존재**하는데 파싱만
    실패한 경우는 일시(`_RateLimited(retry_after=None)`)로 분류해 사다리의 지수
    백오프를 타게 한다("존재"와 "파싱 성공"을 분리, 사후 리뷰 P1-1).
    'PerMinute'거나 판별 불가면 일시(RPM)로 본다(모호할 땐 provider를 죽이지 않는 쪽).

    반환은 `(예외 클래스, retry_after)` 튜플이다 — `_QuotaExhausted`는 항상
    `retry_after=None`, `_RateLimited`는 파싱된 초 또는 `None`(백오프로 대체).
    """
    retry_after: float | None = None
    has_retry_delay = False  # raw 필드 존재 여부(파싱 성공 여부와 별개)
    is_perday = False
    try:
        body = resp.json().get("error", {}) or {}
        for det in body.get("details", []):
            at = det.get("@type", "")
            if "QuotaFailure" in at:
                for v in det.get("violations", []):
                    qid = (v.get("quotaId", "") + v.get("quotaMetric", "")).lower()
                    if "perday" in qid:
                        is_perday = True
            if "RetryInfo" in at:
                if "retryDelay" in det:
                    has_retry_delay = True
                retry_after = _parse_retry_delay(det.get("retryDelay"))
    except Exception:  # noqa: BLE001 — 파싱 실패 시 보수적으로 일시 취급
        pass

    if is_perday and not has_retry_delay:
        return _QuotaExhausted, None
    return _RateLimited, retry_after


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
                 prompt: str | None = None) -> tuple[str, bool]:
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
                # 일시(RPM/retryDelay 있는 PerDay)와 영구(retryDelay 없는 PerDay) 구분 —
                # 일시면 dead 마킹 안 함(OCR 사다리가 쿨다운 후 재시도). 재시도로 안
                # 풀리므로 즉시 승격하되, retry_after 소비(대기)는 ocr_ensemble 몫이다
                # — 여기서는 자체 재시도를 추가하지 않는다(이중 재시도 금지).
                exc_cls, retry_after = _classify_429(resp)
                msg = f"Gemini rate/quota(HTTP {resp.status_code}): {image_path.name}"
                if exc_cls is _RateLimited:
                    raise _RateLimited(msg, retry_after=retry_after)
                raise exc_cls(msg)
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
                # round-38A: requests의 JSONDecodeError는 RequestException 하위라 이 호출을
                # 바깥 네트워크 try에 넣으면 재시도 정책이 바뀐다. JSON 실패는 즉시 정규화.
                try:
                    data = resp.json()
                except ValueError as e:
                    raise OcrError(
                        f"Gemini OCR JSON 파싱 실패: {_redact(str(e), api_key)}"
                    ) from e
                try:
                    candidate = data["candidates"][0]
                    text = candidate["content"]["parts"][0]["text"]
                except (KeyError, IndexError, TypeError) as e:
                    raise OcrError(f"Gemini 응답 형식 이상: {e}") from e
                # round-36 F4: finishReason="MAX_TOKENS"면 절단(truncated) — 필드 부재는
                # False(정상 완료)로 간주한다(오분류 금지, 계약 §절단 판정 기준).
                truncated = candidate.get("finishReason") == "MAX_TOKENS"
                return text, truncated

        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_DELAY_SECONDS)

    raise OcrError(
        f"Gemini OCR 재시도 소진({_MAX_RETRIES}회): {_redact(str(last_err), api_key)}"
    ) from last_err


def ocr_image(path: str | Path, *, prompt: str | None = None) -> dict:
    """이미지 파일 → `{"text": str, "model": str, "truncated": bool}`.

    `truncated`(round-36 F4)는 `finishReason == "MAX_TOKENS"`로 응답이 잘렸는지
    신호한다 — 앙상블(`core/ocr_ensemble.py`)이 candidate 완전성 비교·`partial`
    라벨에 이 값을 배선한다.

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
    text, truncated = _call_gemini(image_path, api_key=api_key, model=model, prompt=prompt)
    _log.info("Gemini OCR 완료: %s (model=%s, truncated=%s)", image_path.name, model, truncated)
    return {"text": text, "model": model, "truncated": truncated}
