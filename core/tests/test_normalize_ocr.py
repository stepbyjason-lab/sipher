"""round-36 F4: `core.normalize.enrich_ocr` — OCR 절단(truncation) 신호 전파.

계약(.handoff/rounds/round-36-ocr-truncation-detection-contract.md) §normalize
전파(P0-2): 절단본은 `done_count`에 세지 않아 `meta.ocr_label`이 "done"이 아니라
자연히 "partial"로 나오고(조용한 done 금지), `ocr_text[]` 항목에는 `partial: true`
provenance가 실려 원인(다운로드 부분 vs OCR 절단)을 구분할 수 있어야 한다.
텍스트는 절단본이라도 버리지 않고 보존한다.

`core.ocr_ensemble.ocr_image_ensemble`을 monkeypatch로 대체 — 네트워크 없음.
"""
from __future__ import annotations

import os
import sys

import logging

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pathlib import Path  # noqa: E402

from core import normalize as N  # noqa: E402
from core import ocr_ensemble as _E  # noqa: E402


def _img(tmp_path, name: str) -> str:
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    return str(p)


def _result(media_paths: list[str], image_count: int | None = None) -> dict:
    meta = {}
    if image_count is not None:
        meta["image_count"] = image_count
    return {"media_paths": media_paths, "meta": meta}


def test_r36_single_truncated_image_yields_partial_label_and_item_provenance(tmp_path, monkeypatch):
    p = _img(tmp_path, "card.jpg")
    monkeypatch.setattr(_E, "is_available", lambda: True)
    monkeypatch.setattr(
        _E, "ocr_image_ensemble",
        lambda path: {"text": "잘린 텍스트", "model": "solo(gemini)", "mode": "solo", "partial": True})

    out = N.enrich_ocr(_result([p]))

    assert out["meta"]["ocr_label"] == "partial"  # done이 아니라 partial — 조용한 done 금지
    assert out["ocr_text"][0]["text"] == "잘린 텍스트"  # 텍스트는 보존
    assert out["ocr_text"][0]["partial"] is True  # 항목 provenance: 원인이 OCR 절단


def test_r36_all_truncated_images_yield_partial_not_failed(tmp_path, monkeypatch):
    # 배치 전부가 절단이면 done_count=0이지만 "failed"가 아니라 "partial"이어야 한다
    # (텍스트 자체는 확보됐으므로 완전 실패로 오분류하면 안 됨).
    paths = [_img(tmp_path, f"card{i}.jpg") for i in range(2)]
    monkeypatch.setattr(_E, "is_available", lambda: True)
    monkeypatch.setattr(
        _E, "ocr_image_ensemble",
        lambda path: {"text": f"잘림:{path.name}", "model": "solo(gemini)",
                      "mode": "solo", "partial": True})

    out = N.enrich_ocr(_result(paths))

    assert out["meta"]["ocr_label"] == "partial"
    assert len(out["ocr_text"]) == 2
    assert all(item["partial"] is True for item in out["ocr_text"])


def test_r36_non_truncated_result_has_no_item_provenance_key(tmp_path, monkeypatch):
    # 회귀 가드: truncated 아닌 정상 OCR 결과에는 "partial" 키 자체가 항목에 안 실린다.
    p = _img(tmp_path, "card.jpg")
    monkeypatch.setattr(_E, "is_available", lambda: True)
    monkeypatch.setattr(
        _E, "ocr_image_ensemble",
        lambda path: {"text": "완전한 텍스트", "model": "solo(gemini)", "mode": "solo"})

    out = N.enrich_ocr(_result([p]))

    assert out["meta"]["ocr_label"] == "done"
    assert "partial" not in out["ocr_text"][0]


def test_r36_mixed_truncated_and_complete_images_yield_partial_label(tmp_path, monkeypatch):
    p1 = _img(tmp_path, "card1.jpg")
    p2 = _img(tmp_path, "card2.jpg")
    monkeypatch.setattr(_E, "is_available", lambda: True)

    def fake_ensemble(path):
        if path.name == "card1.jpg":
            return {"text": "완전한 텍스트", "model": "solo(gemini)", "mode": "solo"}
        return {"text": "잘린 텍스트", "model": "solo(gemini)", "mode": "solo", "partial": True}

    monkeypatch.setattr(_E, "ocr_image_ensemble", fake_ensemble)

    out = N.enrich_ocr(_result([p1, p2]))

    assert out["meta"]["ocr_label"] == "partial"  # done(2/2)이 아니라 partial(1 절단)
    items = {Path(item["media_path"]).name: item for item in out["ocr_text"]}
    assert "partial" not in items["card1.jpg"]
    assert items["card2.jpg"]["partial"] is True


def test_r36_ocr_label_partial_with_zero_done_and_zero_partial_stays_failed(tmp_path, monkeypatch):
    # 회귀 가드: partial=0이고 done=0이면 여전히 "failed"(순수 실패, R34 기존 동작 불변).
    p = _img(tmp_path, "card.jpg")
    monkeypatch.setattr(_E, "is_available", lambda: True)

    from core import llm_free

    def raise_ocr_error(path):
        raise llm_free.OcrError("완전 실패")

    monkeypatch.setattr(_E, "ocr_image_ensemble", raise_ocr_error)

    out = N.enrich_ocr(_result([p]))

    assert out["meta"]["ocr_label"] == "failed"
    assert out["ocr_text"] == []


def test_ocr_label_partial_reused_not_new_value():
    # 계약: 새 라벨값을 만들지 않고 기존 "partial"을 재사용한다(§normalize 전파).
    assert N._ocr_label(
        provider_available=True, expected_images=2, local_images=2, done=0, partial=2,
    ) == "partial"
    assert N._ocr_label(
        provider_available=True, expected_images=2, local_images=2, done=1, partial=1,
    ) == "partial"
    # 기존(round-34) 동작 불변: partial 인자 없이 호출하는 기존 호출부는 그대로 동작.
    assert N._ocr_label(
        provider_available=True, expected_images=2, local_images=2, done=0,
    ) == "failed"


def test_r38a_unknown_exception_skips_only_one_image_and_logs_exception(tmp_path, monkeypatch, caplog):
    paths = [_img(tmp_path, "one.jpg"), _img(tmp_path, "two.jpg")]
    monkeypatch.setattr(_E, "is_available", lambda: True)

    def fake_ensemble(path):
        if path.name == "one.jpg":
            raise RuntimeError("future bug")
        return {"text": "second image", "model": "solo(gemini)", "mode": "solo"}

    monkeypatch.setattr(_E, "ocr_image_ensemble", fake_ensemble)
    with caplog.at_level(logging.DEBUG, logger=N._log.name):
        out = N.enrich_ocr(_result(paths))

    assert [item["text"] for item in out["ocr_text"]] == ["second image"]
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert records and all(r.exc_info is not None for r in records)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_r38a_enrich_ocr_does_not_swallow_process_interrupts(tmp_path, monkeypatch, interrupt):
    monkeypatch.setattr(_E, "is_available", lambda: True)
    monkeypatch.setattr(_E, "ocr_image_ensemble", lambda path: (_ for _ in ()).throw(interrupt()))

    with pytest.raises(interrupt):
        N.enrich_ocr(_result([_img(tmp_path, "card.jpg")]))


@pytest.mark.parametrize("failure", [N.llm_free.OcrError("known OCR failure"), RuntimeError("unknown OCR failure")])
def test_r38a_ocr_failures_never_produce_done_label(tmp_path, monkeypatch, failure):
    paths = [_img(tmp_path, "one.jpg"), _img(tmp_path, "two.jpg")]
    monkeypatch.setattr(_E, "is_available", lambda: True)

    def fake_ensemble(path):
        if path.name == "two.jpg":
            raise failure
        return {"text": "first image", "model": "solo(gemini)", "mode": "solo"}

    monkeypatch.setattr(_E, "ocr_image_ensemble", fake_ensemble)
    out = N.enrich_ocr(_result(paths, image_count=1))

    assert out["meta"]["ocr_label"] != "done"
