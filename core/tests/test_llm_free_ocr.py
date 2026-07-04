"""llm_free OCR 단일키 동작 — 멀티키 로테이션 제거 후(2026-07-03, ToS).

네트워크 없이 `_call_gemini`를 monkeypatch로 대체. 검증: 단일키 성공, quota→
_QuotaExhausted 전파(앙상블 dead 마킹용), 키 값 로그 비노출, 키 없으면 OcrError.
"""
from __future__ import annotations

import logging
import os
import sys

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
    monkeypatch.setattr(L, "_call_gemini", lambda p, *, api_key, model, prompt=None: "OK")
    r = L.ocr_image(_img(tmp_path))
    assert r["text"] == "OK" and r["model"] == L._DEFAULT_MODEL


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


def test_classify_429_perday_is_permanent():
    class R:
        def json(self):
            return {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    assert L._classify_429(R()) is L._QuotaExhausted


def test_classify_429_perminute_is_transient():
    class R:
        def json(self):
            return {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}
    assert L._classify_429(R()) is L._RateLimited


def test_classify_429_unknown_is_transient():
    # 판별 불가(details 없음) → 보수적으로 일시(provider를 죽이지 않음)
    class R:
        def json(self): return {"error": {}}
    assert L._classify_429(R()) is L._RateLimited


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
        monkeypatch.setattr(L, "_call_gemini", lambda p, *, api_key, model, prompt=None: "OK")
        L.ocr_image(_img(tmp_path))
    finally:
        L._log.removeHandler(h)
    assert "SECRETKEY_ABC" not in "\n".join(recs)


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
