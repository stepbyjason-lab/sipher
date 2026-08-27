"""llm_free OCR 단일키 동작 — 멀티키 로테이션 제거 후(2026-07-03, ToS).

네트워크 없이 `_call_gemini`를 monkeypatch로 대체. 검증: 단일키 성공, quota→
_QuotaExhausted 전파(앙상블 dead 마킹용), 키 값 로그 비노출, 키 없으면 OcrError.
"""
from __future__ import annotations

import logging
import os
import sys

import pytest
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pathlib import Path  # noqa: E402

from core import llm_free as L  # noqa: E402


def _env(env_file: Path, **kv):
    L._ROOT = env_file.parent
    for k in list(os.environ):
        if k.startswith("GEMINI"):
            os.environ.pop(k, None)
    env_file.write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n", encoding="utf-8")


def _img(tmp_path) -> Path:
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    return p


def test_single_key_config(tmp_path):
    _env(tmp_path / ".env.local", GEMINI_API_KEY="k1")
    key, model = L._config()
    assert key == "k1" and model == L._DEFAULT_MODEL


def test_extra_numbered_keys_are_ignored(tmp_path):
    # 멀티키 규약 제거 — _2/_3는 이제 무시된다(단일 GEMINI_API_KEY만)
    _env(tmp_path / ".env.local", GEMINI_API_KEY="k1", GEMINI_API_KEY_2="k2", GEMINI_API_KEY_3="k3")
    key, _ = L._config()
    assert key == "k1"
    assert not hasattr(L, "_configs")       # 멀티키 함수 제거됨
    assert not hasattr(L, "_active_idx")     # sticky 인덱스 제거됨


def test_ocr_success(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GEMINI_API_KEY="k1")
    monkeypatch.setattr(L, "_call_gemini", lambda p, *, api_key, model, prompt=None: ("OK", False))
    r = L.ocr_image(_img(tmp_path))
    assert r["text"] == "OK" and r["model"] == L._DEFAULT_MODEL
    assert r["truncated"] is False


def test_quota_propagates(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GEMINI_API_KEY="k1")
    def boom(p, *, api_key, model, prompt=None):
        raise L._QuotaExhausted("429")
    monkeypatch.setattr(L, "_call_gemini", boom)
    try:
        L.ocr_image(_img(tmp_path))
        assert False, "_QuotaExhausted expected"
    except L._QuotaExhausted:
        pass  # 앙상블이 이 예외로 provider dead 마킹


def test_classify_429_perday_without_retry_delay_is_permanent():
    # round-28: retryDelay 없는 PerDay만 영구 소진 후보로 남긴다
    class R:
        def json(self):
            return {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    cls, retry_after = L._classify_429(R())
    assert cls is L._QuotaExhausted
    assert retry_after is None


def test_classify_429_perday_with_retry_delay_is_transient():
    # round-28 핵심 개정: PerDay + RetryInfo.retryDelay=44s 공존 → 일시(_RateLimited)
    class R:
        def json(self):
            return {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                 "retryDelay": "44s"}]}}
    cls, retry_after = L._classify_429(R())
    assert cls is L._RateLimited
    assert retry_after == 44.0


def test_classify_429_perday_with_unparseable_retry_delay_is_transient():
    # 사후 리뷰 P1-1: retryDelay 필드가 raw로 "존재"하면 파싱 실패라도 일시다 —
    # _RateLimited(retry_after=None)로 분류돼 사다리의 지수 백오프를 탄다.
    class R:
        def json(self):
            return {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                 "retryDelay": "weird"}]}}
    cls, retry_after = L._classify_429(R())
    assert cls is L._RateLimited
    assert retry_after is None


def test_classify_429_perminute_is_transient():
    class R:
        def json(self):
            return {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}
    cls, retry_after = L._classify_429(R())
    assert cls is L._RateLimited
    assert retry_after is None


def test_classify_429_unknown_is_transient():
    # 판별 불가(details 없음) → 보수적으로 일시(provider를 죽이지 않음)
    class R:
        def json(self): return {"error": {}}
    cls, retry_after = L._classify_429(R())
    assert cls is L._RateLimited
    assert retry_after is None


def test_parse_retry_delay_seconds_and_fractional():
    assert L._parse_retry_delay("44s") == 44.0
    assert L._parse_retry_delay("3.5s") == 3.5


def test_parse_retry_delay_minutes():
    assert L._parse_retry_delay("1m") == 60.0


def test_parse_retry_delay_unparseable_returns_none():
    assert L._parse_retry_delay("not-a-delay") is None
    assert L._parse_retry_delay("") is None
    assert L._parse_retry_delay(None) is None


def test_parse_retry_delay_clamps_negative_zero_and_nonfinite(tmp_path=None):
    # 리뷰 CORR-R1: 음수/0/NaN/inf는 None 폴백(→ 사다리 지수 백오프). 경계 검증.
    assert L._parse_retry_delay("-5s") is None      # 음수 → None(즉시 만료 방지)
    assert L._parse_retry_delay("0s") is None        # 0 → None(무의미한 쿨다운 방지)
    assert L._parse_retry_delay("nan") is None       # NaN → None(비교 우회 방지)
    assert L._parse_retry_delay("inf") is None       # inf → None
    assert L._parse_retry_delay(float("nan")) is None
    assert L._parse_retry_delay(-3.0) is None
    assert L._parse_retry_delay(0.0) is None
    # 정상 경계는 그대로 통과
    assert L._parse_retry_delay("1m") == 60.0
    assert L._parse_retry_delay("44s") == 44.0


def test_no_key_raises(tmp_path):
    _env(tmp_path / ".env.local")  # 빈 파일
    try:
        L.ocr_image(_img(tmp_path))
        assert False
    except L.OcrError as e:
        assert "GEMINI_API_KEY" in str(e)


def test_key_value_not_logged(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GEMINI_API_KEY="SECRETKEY_ABC")
    recs = []
    h = logging.Handler(); h.emit = lambda r: recs.append(h.format(r))
    L._log.addHandler(h); L._log.setLevel(logging.DEBUG)
    try:
        monkeypatch.setattr(L, "_call_gemini", lambda p, *, api_key, model, prompt=None: ("OK", False))
        L.ocr_image(_img(tmp_path))
    finally:
        L._log.removeHandler(h)
    assert "SECRETKEY_ABC" not in "\n".join(recs)


class _GeminiResponse:
    status_code = 200
    text = "OK"

    def __init__(self, data=None, json_error=None):
        self.data = data
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.data


@pytest.mark.parametrize("error_cls", [requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError])
def test_r38a_gemini_network_exceptions_raise_ocr_error(tmp_path, monkeypatch, error_cls):
    image = _img(tmp_path)
    monkeypatch.setattr(L.requests, "post", lambda *a, **kw: (_ for _ in ()).throw(error_cls("network")))

    with pytest.raises(L.OcrError):
        L._call_gemini(image, api_key="key", model="model")


def test_r38a_gemini_json_decode_error_raises_without_retry(tmp_path, monkeypatch):
    image = _img(tmp_path)
    calls = []
    sleeps = []
    monkeypatch.setattr(L.requests, "post", lambda *a, **kw: calls.append(1) or _GeminiResponse(
        json_error=requests.exceptions.JSONDecodeError("bad json", "<html>", 0)))
    monkeypatch.setattr(L.time, "sleep", lambda seconds: sleeps.append(seconds))

    with pytest.raises(L.OcrError):
        L._call_gemini(image, api_key="key", model="model")

    assert len(calls) == 1  # 1-a: JSON 파싱 실패는 네트워크 재시도 경로가 아니다.
    assert sleeps == []


def test_r38a_gemini_wrong_json_shape_raises_ocr_error(tmp_path, monkeypatch):
    monkeypatch.setattr(L.requests, "post", lambda *a, **kw: _GeminiResponse(data=[]))

    with pytest.raises(L.OcrError):
        L._call_gemini(_img(tmp_path), api_key="key", model="model")


def test_ko_prompt_is_not_language_scoped():
    """회귀 가드: KO OCR 프롬프트가 추출 범위를 '한국어 텍스트'로 한정하면
    다국어 카드뉴스(한국어 캡션 + 영어 프롬프트 박스)에서 영어를 통째로 떨군다.

    2026-07-13 실측 근거: 동일 이미지·동일 모델(Gemini)에서 KO 프롬프트는
    영어 알파벳 0자(영어 프롬프트 박스 전면 누락), 언어중립 프롬프트는 253자로
    영어 프롬프트를 완전 캡처. OCR 추출 범위는 사용자 언어와 무관하게 항상
    '언어 불문 전량'이어야 한다(resolve_lang은 지시문 언어만 결정).
    """
    p = L._build_prompt("ko")
    # 추출 범위를 특정 언어로 한정하는 표현이 없어야 한다(우회 문구까지 폭넓게 차단 —
    # 외부 리뷰 Codex#7/Gemini5.1: 'not in "한국어 텍스트를"'만으론 '한국어만'·'영어 제외'
    # 같은 등가 한정이 통과함).
    import re
    banned = [
        "한국어 텍스트",      # 원 버그 문구
        "한국어만", "한글만",  # "~만" 한정
        "영어는 제외", "영어 제외", "영어를 제외",  # 특정언어 배제
    ]
    for phrase in banned:
        assert phrase not in p, f"OCR 프롬프트가 언어를 한정함('{phrase}') — 타언어 누락 유발"
    # "N개 언어만" 류 배타 한정도 차단.
    assert not re.search(r"(한국어|영어|한글)\s*(?:만|만을|텍스트만)", p), \
        "OCR 프롬프트가 특정 언어로 배타 한정됨 — 타언어 누락 유발"
    # 언어 불문 전량 추출을 명시해야 한다.
    assert "모든 텍스트" in p
    assert ("언어와 상관없이" in p) or ("언어에 상관없이" in p) or ("어떤 언어" in p) or ("모든 언어" in p)


def test_generic_prompt_extracts_all_text():
    """비-ko 언어 경로도 언어 한정 없이 전량 추출을 지시해야 한다(회귀 가드)."""
    p = L._build_prompt("en")
    assert "ALL text" in p


# ── round-36 F4: OCR 절단 감지 — HTTP 응답 mock을 `_call_gemini` 경계에 직접 주입
# (게이트 P1-4: 파서만 격리 테스트하면 상위 소비처가 신호를 버려도 green이 되는
# 구현동형 함정을 피한다) ───────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status_code: int, payload: dict, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self) -> dict:
        return self._payload


def test_call_gemini_detects_truncation_via_max_tokens_finish_reason(tmp_path, monkeypatch):
    payload = {"candidates": [{"content": {"parts": [{"text": "잘린 텍스트"}]},
                                "finishReason": "MAX_TOKENS"}]}
    monkeypatch.setattr(L.requests, "post", lambda *a, **kw: _FakeResp(200, payload))
    text, truncated = L._call_gemini(_img(tmp_path), api_key="k", model="gemini-3.6-flash")
    assert text == "잘린 텍스트"
    assert truncated is True


def test_call_gemini_normal_stop_not_truncated(tmp_path, monkeypatch):
    payload = {"candidates": [{"content": {"parts": [{"text": "완전한 텍스트"}]},
                                "finishReason": "STOP"}]}
    monkeypatch.setattr(L.requests, "post", lambda *a, **kw: _FakeResp(200, payload))
    text, truncated = L._call_gemini(_img(tmp_path), api_key="k", model="gemini-3.6-flash")
    assert text == "완전한 텍스트"
    assert truncated is False


def test_call_gemini_missing_finish_reason_defaults_to_not_truncated(tmp_path, monkeypatch):
    # 필드 부재 시 False(정상 완료)로 간주 — 정상 응답을 절단으로 오분류하지 않는다.
    payload = {"candidates": [{"content": {"parts": [{"text": "본문"}]}}]}
    monkeypatch.setattr(L.requests, "post", lambda *a, **kw: _FakeResp(200, payload))
    text, truncated = L._call_gemini(_img(tmp_path), api_key="k", model="gemini-3.6-flash")
    assert text == "본문"
    assert truncated is False


def test_ocr_image_propagates_truncated_flag_from_call_gemini(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GEMINI_API_KEY="k1")
    monkeypatch.setattr(L, "_call_gemini", lambda p, *, api_key, model, prompt=None: ("잘림", True))
    r = L.ocr_image(_img(tmp_path))
    assert r["truncated"] is True
    assert r["text"] == "잘림"


if __name__ == "__main__":
    import tempfile, traceback
    class _MP:
        def __init__(self): self._o = []
        def setattr(self, o, n, v): self._o.append((o, n, getattr(o, n))); setattr(o, n, v)
        def undo(self):
            for o, n, v in reversed(self._o): setattr(o, n, v)
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for name, fn in fns:
        mp = _MP()
        try:
            with tempfile.TemporaryDirectory() as d:
                argn = fn.__code__.co_varnames[: fn.__code__.co_argcount]
                a = {"tmp_path": Path(d)}
                if "monkeypatch" in argn: a["monkeypatch"] = mp
                fn(**{k: v for k, v in a.items() if k in argn})
            print(f"PASS {name}")
        except Exception:
            fails += 1; print(f"FAIL {name}"); traceback.print_exc()
        finally:
            mp.undo()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
