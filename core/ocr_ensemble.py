r"""
sipher OCR 앙상블 사다리 — round-24 (사용자 확정 설계, MemKraft `sipher-ocr-ensemble-arch`).

기본 = 무료 앙상블: 살아있는 무료 provider(gemini·nim_gemma4·nim_nemotron)로 후보를
수집하고, judge(gemma-4 우선, 없으면 gemini)가 이미지를 직접 보며 후보를 교정한다.
실측(2026-07-03, 카드 8장 원본대조): gemini 단독 6/8 → 앙상블 ~8/8, gemma-4 judge는
Gemini judge와 동급(4/4)이면서 Gemini quota를 아낀다. 다수결은 오답다수 케이스가
실존해 금지 — judge 방식만.

사다리:
  [1] 앙상블(후보 ≥2 + 무료 judge) → [2] 잔존 provider solo → [3] 전부 소진 시
  TTY 1회 질문으로 유료 Claude 옵트인(비TTY/거절 → 정직 degrade).

judge-pluggable 규약(사용자 통찰): judge 자리에 무료든 유료(Claude)든 같은
인터페이스로 꽂힌다 — 유료 judge도 "이미지+짧은 후보 → 짧은 교정"이라 토큰 절약.

키 값은 로그·예외에 절대 노출하지 않는다(인덱스/모델명만).
"""
from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path

import requests

from . import llm_free
from .llm_free import OcrError, _QuotaExhausted, _RateLimited

__all__ = ["ocr_image_ensemble", "is_available"]

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_NIM_BASE_DEFAULT = "https://integrate.api.nvidia.com/v1"
_NIM_GEMMA4 = "google/gemma-4-31b-it"
_NIM_NEMOTRON = "nvidia/nemotron-nano-12b-v2-vl"
_CLAUDE_DEFAULT_MODEL = "claude-sonnet-4-5"
_TIMEOUT = 120

# OCR 프롬프트: llm_free와 동일 소스(ko=PoC 검증본, 그 외 언어중립).
# judge 프롬프트: 실측(ocr_poc2)에서 4/4 확인된 문구 고정.
_JUDGE_PROMPT_HEADER = (
    "아래는 이 이미지에 대한 여러 OCR 결과다. 이미지를 직접 보고 오독을 교정해 "
    "가장 정확한 최종 텍스트만 출력하라. 배경(사진 속 간판·가격·라벨 등)은 무시하고 "
    "오버레이/카드 텍스트만. 설명 없이 텍스트만."
)

# 프로세스 내 dead 마킹(quota류만) — 죽은 provider를 이미지마다 재시도하지 않는다.
_dead: set[str] = set()
# 유료 동의 상태: None=미질문, True/False=답변 캐시(프로세스당 1회 질문)
_paid_consent: bool | None = None


def _env() -> dict[str, str]:
    env = llm_free._load_env_file(_ROOT / ".env.local")
    # os.environ 폴백(파일 우선 — llm_free._config와 동일 원칙)
    merged = dict(env)
    for k, v in os.environ.items():
        merged.setdefault(k, v)
    return merged


def _nim_key() -> str | None:
    return _env().get("NVIDIA_NIM_API_KEY") or None


def _anthropic_key() -> str | None:
    return _env().get("ANTHROPIC_API_KEY") or None


def is_available() -> bool:
    """무료 OCR provider가 1개라도 구성돼 있으면 True(네트워크 호출 없음)."""
    return llm_free.is_available() or bool(_nim_key())


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _mime(path: Path) -> str:
    return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"


# ── provider 호출부 ──────────────────────────────────────────────────────────

def _call_nim(image_path: Path, *, model: str, prompt: str) -> str:
    """NIM chat/completions(비전, data URI) — 실측 포맷(ocr_poc2). quota류는 _QuotaExhausted."""
    key = _nim_key()
    if not key:
        raise OcrError("NVIDIA_NIM_API_KEY 없음")
    data_url = f"data:{_mime(image_path)};base64,{_b64(image_path)}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}],
        "max_tokens": 2048,
        "temperature": 0,
    }
    r = requests.post(f"{_env().get('NVIDIA_NIM_BASE_URL', _NIM_BASE_DEFAULT)}/chat/completions",
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json=body, timeout=_TIMEOUT)
    if r.status_code == 402 or (
        r.status_code == 403 and ("credit" in r.text.lower() or "quota" in r.text.lower())
    ):
        raise _QuotaExhausted(f"NIM credit 소진(HTTP {r.status_code})")  # 영구 → dead
    if r.status_code == 429:
        raise _RateLimited(f"NIM rate-limit(HTTP {r.status_code})")      # 일시 → dead 아님
    if r.status_code != 200:
        raise OcrError(f"NIM HTTP {r.status_code}")
    return r.json()["choices"][0]["message"]["content"]


def _call_claude(image_path: Path, *, prompt: str) -> tuple[str, str]:
    """Anthropic messages(유료, 옵트인 전용). (text, model) 반환. SDK 없이 requests 직접."""
    key = _anthropic_key()
    if not key:
        raise OcrError("ANTHROPIC_API_KEY 없음(유료 폴백 불가)")
    model = _env().get("CLAUDE_OCR_MODEL", _CLAUDE_DEFAULT_MODEL)
    body = {
        "model": model,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": _mime(image_path),
                                         "data": _b64(image_path)}},
            {"type": "text", "text": prompt},
        ]}],
    }
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                               "Content-Type": "application/json"},
                      json=body, timeout=_TIMEOUT)
    if r.status_code != 200:
        raise OcrError(f"Claude HTTP {r.status_code}")
    return r.json()["content"][0]["text"], model


# 후보 provider: (이름, 우선순위용 순서, 호출 람다) — 호출은 OCR 프롬프트 사용.
def _candidate_providers() -> list[tuple[str, object]]:
    from .lang import resolve_lang
    prompt = llm_free._build_prompt(resolve_lang())
    provs: list[tuple[str, object]] = []
    if llm_free.is_available():
        provs.append(("gemini", lambda p: llm_free.ocr_image(p)["text"]))
    if _nim_key():
        provs.append(("nim_gemma4", lambda p: _call_nim(p, model=_NIM_GEMMA4, prompt=prompt)))
        provs.append(("nim_nemotron", lambda p: _call_nim(p, model=_NIM_NEMOTRON, prompt=prompt)))
    return provs


def _ask_paid_consent() -> bool:
    """전 무료 소진 시 유료 Claude 전환 여부. env 설정 > TTY 1회 질문 > 기본 거절."""
    global _paid_consent
    if _env().get("OCR_PAID_FALLBACK", "").strip().lower() == "claude":
        return True
    if _paid_consent is not None:
        return _paid_consent
    if not sys.stdin.isatty():
        _paid_consent = False
        return False
    try:
        ans = input("[sipher] 무료 OCR provider가 모두 소진되었습니다. "
                    "Claude(유료, ANTHROPIC_API_KEY)로 진행할까요? [y/N/always] ").strip().lower()
    except EOFError:
        ans = ""
    if ans == "always":
        try:  # append-only — 기존 내용 보존(.env.local, gitignore)
            with (_ROOT / ".env.local").open("a", encoding="utf-8") as f:
                f.write("\n# OCR 유료 폴백(사용자 always 응답, round-24)\nOCR_PAID_FALLBACK=claude\n")
        except OSError:
            pass
        _paid_consent = True
    else:
        _paid_consent = ans == "y"
    return _paid_consent


# ── 메인 진입점 ──────────────────────────────────────────────────────────────

def ocr_image_ensemble(path: str | Path) -> dict:
    """이미지 → {"text","model","mode"}. 사다리 문서는 모듈 docstring 참조.

    "model"은 최종 결정 주체를 정직 표기 — ensemble이면 "ensemble(judge=<모델>)",
    solo면 해당 provider 모델명. normalize의 ocr_provider로 그대로 흐른다.
    """
    image_path = Path(path)
    if not image_path.exists():
        raise OcrError(f"이미지 파일이 없습니다: {image_path}")

    mode_env = _env().get("OCR_MODE", "ensemble").strip().lower()

    # 1) 후보 수집. 영구(_QuotaExhausted)=세션 dead 마킹, 일시(_RateLimited)=이번 이미지만
    #    skip(provider 살려둠 — 다음 이미지에서 RPM 창 지나면 부활). registry는 dead 아닌 것.
    registry = _candidate_providers()
    candidates: list[tuple[str, str]] = []  # (provider명, 텍스트)
    rate_limited_now = False
    for name, call in registry:
        if name in _dead:
            continue
        if mode_env == "solo" and candidates:
            break  # solo 모드: 첫 성공에서 종료(사다리 최소 동작)
        try:
            candidates.append((name, call(image_path)))
        except _RateLimited:
            rate_limited_now = True  # 일시 — dead 마킹 안 함
            _log.info("OCR provider %s 일시 rate-limit — 이번 이미지 skip(부활 대기)", name)
        except _QuotaExhausted:
            _dead.add(name)
            _log.warning("OCR provider %s 영구 소진 — 세션 skip(사다리 degrade)", name)
        except OcrError as e:
            _log.warning("OCR 후보 %s 실패(계속): %s", name, e)
        except Exception as e:  # 네트워크 등 — 후보만 포기
            _log.warning("OCR 후보 %s 예외(계속): %s", name, type(e).__name__)

    # 아직 살아있는(dead 아닌) provider가 있나 — 유료 escalation 여부 판단용
    _alive_providers = any(n not in _dead for n, _ in registry)

    # 2) 앙상블: 후보 ≥2면 judge 교정
    if len(candidates) >= 2 and mode_env != "solo":
        judge_prompt = _JUDGE_PROMPT_HEADER + "\n\n" + "\n\n".join(
            f"[후보{i+1}]\n{t}" for i, (_, t) in enumerate(candidates))
        # 무료 judge: gemma-4 우선(실측 4/4, Gemini quota 절약) → gemini 폴백
        for jname in ("nim_gemma4", "gemini"):
            if jname in _dead:
                continue
            try:
                if jname == "nim_gemma4":
                    if not _nim_key():
                        continue
                    text = _call_nim(image_path, model=_NIM_GEMMA4, prompt=judge_prompt)
                    return {"text": text.strip(), "model": f"ensemble(judge={_NIM_GEMMA4})",
                            "mode": "ensemble"}
                if llm_free.is_available():
                    jr = llm_free.ocr_image(image_path, prompt=judge_prompt)
                    return {"text": jr["text"].strip(),
                            "model": f"ensemble(judge={jr['model']})", "mode": "ensemble"}
            except _RateLimited:
                _log.info("judge %s 일시 rate-limit — 다음 judge/폴백", jname)  # dead 아님
            except _QuotaExhausted:
                _dead.add(jname)
            except OcrError as e:
                _log.debug("judge %s 실패(다음/폴백): %s", jname, e)
        # judge 전멸 → 유료 judge 시도(동의 시) → 아니면 최상위 후보로 폴백(전체 실패 금지)
        if _ask_paid_consent() and _anthropic_key():
            try:
                text, cmodel = _call_claude(image_path, prompt=judge_prompt)
                return {"text": text.strip(), "model": f"paid_judge({cmodel})", "mode": "paid_judge"}
            except OcrError as e:
                _log.warning("유료 judge 실패(후보 폴백): %s", e)
        best = candidates[0]  # 수집 순서 = 우선순위(gemini > gemma4 > nemotron)
        return {"text": best[1].strip(), "model": f"solo({best[0]})", "mode": "solo"}

    # 3) 후보 1개 → solo
    if candidates:
        name, text = candidates[0]
        return {"text": text.strip(), "model": f"solo({name})", "mode": "solo"}

    # 4) 후보 0개.
    #    - 일시 rate-limit 때문(살아있는 provider 존재) → 유료로 넘어가지 않는다(토큰 절약
    #      원칙). 이번 이미지만 정직 실패시키고 다음 이미지에서 무료가 부활한다.
    if rate_limited_now and _alive_providers:
        raise _RateLimited("무료 OCR provider 일시 rate-limit — 이번 이미지 skip(부활 대기)")
    #    - 진짜 전부 영구 소진/미구성일 때만 유료 옵트인.
    if _ask_paid_consent() and _anthropic_key():
        from .lang import resolve_lang
        text, cmodel = _call_claude(image_path, prompt=llm_free._build_prompt(resolve_lang()))
        return {"text": text.strip(), "model": f"paid_solo({cmodel})", "mode": "paid_solo"}
    raise OcrError("무료 OCR provider 전부 소진/실패(유료 폴백 미동의)")
