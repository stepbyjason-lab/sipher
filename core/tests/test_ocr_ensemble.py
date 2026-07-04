"""round-24: OCR 앙상블 사다리 단위 테스트(네트워크 없음 — provider 호출 monkeypatch)."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pathlib import Path  # noqa: E402

from core import llm_free, ocr_ensemble as E  # noqa: E402


def _img(tmp_path) -> Path:
    p = tmp_path / "card.jpg"
    p.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    return p


def _reset(monkeypatch, *, gemini=True, nim=True, anthropic=False, extra_env=None):
    """provider 가용성·dead 상태·동의 캐시 초기화."""
    E._dead.clear()
    E._paid_consent = None
    monkeypatch.setattr(E, "_env", lambda: {
        **({"NVIDIA_NIM_API_KEY": "nk"} if nim else {}),
        **({"ANTHROPIC_API_KEY": "ak"} if anthropic else {}),
        **(extra_env or {}),
    })
    monkeypatch.setattr(llm_free, "is_available", lambda: gemini)


def test_full_ensemble_uses_gemma4_judge(tmp_path, monkeypatch):
    _reset(monkeypatch)
    calls = []
    monkeypatch.setattr(llm_free, "ocr_image",
                        lambda p, prompt=None: calls.append("gemini") or {"text": "G", "model": "gemini"})
    def nim(p, *, model, prompt):
        calls.append(model)
        return "JUDGED" if "후보" in prompt else "N"
    monkeypatch.setattr(E, "_call_nim", nim)
    r = E.ocr_image_ensemble(_img(tmp_path))
    assert r["mode"] == "ensemble"
    assert r["text"] == "JUDGED"
    assert "gemma-4" in r["model"]
    # 후보 3(gemini+gemma4+nemotron) + judge 1(gemma4)
    assert calls.count(E._NIM_GEMMA4) == 2


def test_nim_dead_degrades_to_gemini_solo(tmp_path, monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(llm_free, "ocr_image",
                        lambda p, prompt=None: {"text": "G", "model": "gemini-2.5-flash"})
    def nim(p, *, model, prompt):
        raise llm_free._QuotaExhausted("credit out")
    monkeypatch.setattr(E, "_call_nim", nim)
    r = E.ocr_image_ensemble(_img(tmp_path))
    assert r["mode"] == "solo" and r["text"] == "G" and "gemini" in r["model"]
    # dead 마킹 → 2회째 호출에서 NIM 재시도 없음
    r2 = E.ocr_image_ensemble(_img(tmp_path))
    assert r2["mode"] == "solo"
    assert {"nim_gemma4", "nim_nemotron"} <= E._dead


def test_gemini_dead_degrades_to_nim_ensemble_then_solo(tmp_path, monkeypatch):
    _reset(monkeypatch)
    def gem(p, prompt=None):
        raise llm_free._QuotaExhausted("all keys out")
    monkeypatch.setattr(llm_free, "ocr_image", gem)
    def nim(p, *, model, prompt):
        return "JUDGED" if "후보" in prompt else f"N:{model[-4:]}"
    monkeypatch.setattr(E, "_call_nim", nim)
    r = E.ocr_image_ensemble(_img(tmp_path))
    # gemini 죽어도 NIM 후보 2개로 앙상블 유지
    assert r["mode"] == "ensemble" and r["text"] == "JUDGED"
    assert "gemini" in E._dead


def test_all_dead_non_tty_raises(tmp_path, monkeypatch):
    _reset(monkeypatch, gemini=False, nim=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    try:
        E.ocr_image_ensemble(_img(tmp_path))
        assert False, "OcrError expected"
    except llm_free.OcrError as e:
        assert "소진" in str(e)


def test_paid_fallback_env_uses_claude(tmp_path, monkeypatch):
    _reset(monkeypatch, gemini=False, nim=False, anthropic=True,
           extra_env={"OCR_PAID_FALLBACK": "claude"})
    monkeypatch.setattr(E, "_call_claude", lambda p, *, prompt: ("PAID", "claude-sonnet-4-5"))
    r = E.ocr_image_ensemble(_img(tmp_path))
    assert r["mode"] == "paid_solo" and r["text"] == "PAID"


def test_judge_failure_falls_back_to_top_candidate(tmp_path, monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(llm_free, "ocr_image", lambda p, prompt=None: (
        (_ for _ in ()).throw(llm_free.OcrError("judge용 gemini도 실패"))
        if prompt else {"text": "G-TOP", "model": "gemini"}))
    def nim(p, *, model, prompt):
        if "후보" in prompt:
            raise llm_free.OcrError("judge boom")  # judge 실패(quota 아님)
        return "N"
    monkeypatch.setattr(E, "_call_nim", nim)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)  # 유료 질문 차단
    r = E.ocr_image_ensemble(_img(tmp_path))
    assert r["mode"] == "solo" and r["text"] == "G-TOP"  # 최상위 후보 폴백, 전체 실패 아님


def test_rate_limited_does_not_dead_mark(tmp_path, monkeypatch):
    # 일시 rate-limit(_RateLimited)은 dead 마킹 안 함 — 다음 이미지에서 부활
    _reset(monkeypatch)
    monkeypatch.setattr(llm_free, "ocr_image",
                        lambda p, prompt=None: {"text": "G", "model": "gemini"})
    state = {"n": 0}
    def nim(p, *, model, prompt):
        state["n"] += 1
        if state["n"] <= 1:  # 첫 후보 호출만 일시 rate-limit
            raise llm_free._RateLimited("RPM")
        return "JUDGED" if "후보" in prompt else "N"
    monkeypatch.setattr(E, "_call_nim", nim)
    E.ocr_image_ensemble(_img(tmp_path))
    assert E._dead == set()  # 일시라 dead 마킹 0건


def test_transient_all_limited_does_not_escalate_to_paid(tmp_path, monkeypatch):
    # 무료 전부 일시 rate-limit → 유료로 안 넘어감(_RateLimited raise), 토큰 절약 원칙
    _reset(monkeypatch, anthropic=True, extra_env={"OCR_PAID_FALLBACK": "claude"})
    def gem(p, prompt=None):
        raise llm_free._RateLimited("RPM")
    monkeypatch.setattr(llm_free, "ocr_image", gem)
    def nim(p, *, model, prompt):
        raise llm_free._RateLimited("RPM")
    monkeypatch.setattr(E, "_call_nim", nim)
    paid_called = []
    monkeypatch.setattr(E, "_call_claude",
                        lambda p, *, prompt: paid_called.append(1) or ("PAID", "claude"))
    try:
        E.ocr_image_ensemble(_img(tmp_path))
        assert False, "_RateLimited expected"
    except llm_free._RateLimited:
        pass
    assert paid_called == []        # 유료 미호출(일시라 부활 대기)
    assert E._dead == set()          # dead 마킹도 0


def test_permanent_all_exhausted_escalates_to_paid(tmp_path, monkeypatch):
    # 영구 소진(_QuotaExhausted)일 때만 유료 escalation
    _reset(monkeypatch, anthropic=True, extra_env={"OCR_PAID_FALLBACK": "claude"})
    monkeypatch.setattr(llm_free, "ocr_image",
                        lambda p, prompt=None: (_ for _ in ()).throw(llm_free._QuotaExhausted("RPD")))
    monkeypatch.setattr(E, "_call_nim",
                        lambda p, *, model, prompt: (_ for _ in ()).throw(llm_free._QuotaExhausted("credit")))
    monkeypatch.setattr(E, "_call_claude", lambda p, *, prompt: ("PAID", "claude"))
    r = E.ocr_image_ensemble(_img(tmp_path))
    assert r["mode"] == "paid_solo" and r["text"] == "PAID"


def test_solo_mode_env_short_circuits(tmp_path, monkeypatch):
    _reset(monkeypatch, extra_env={"OCR_MODE": "solo"})
    monkeypatch.setattr(llm_free, "ocr_image", lambda p, prompt=None: {"text": "G", "model": "gemini"})
    called = []
    monkeypatch.setattr(E, "_call_nim", lambda p, *, model, prompt: called.append(model) or "N")
    r = E.ocr_image_ensemble(_img(tmp_path))
    assert r["mode"] == "solo" and r["text"] == "G"
    assert called == []  # 첫 성공에서 종료 — NIM 미호출


if __name__ == "__main__":
    import tempfile
    import traceback

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
                fn(Path(d), mp)
            print(f"PASS {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        finally:
            mp.undo()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
