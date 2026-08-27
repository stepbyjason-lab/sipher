"""round-29 S1: core.fetch smart 기본동작 단위 테스트(네트워크 없음).

`_adapter_fetch`와 `normalize.enrich_*`를 monkeypatch로 대체해, smart가 플랫폼별
capability 맵의 미디어/댓글 kwargs를 올바로 주입하는지 + 사용자 명시가 이기는지 +
smart=False opt-in 시맨틱을 검증한다. 실제 어댑터/OCR/전사는 호출하지 않는다.
실행: `pytest core/tests/test_router_smart.py` 또는 이 파일 직접.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core  # noqa: E402
import core.normalize as N  # noqa: E402


def _cap(monkeypatch):
    """어댑터 fetch와 enrich_*를 가로채 (주입된 kwargs, enrich 호출수)를 기록한다."""
    calls = {"kwargs": None, "platform": None, "ocr": 0, "transcribe": 0}

    def fake_adapter_fetch(platform):
        def _fetch(url, **kwargs):
            calls["kwargs"] = kwargs
            calls["platform"] = platform
            return {"source": url, "platform": platform, "body_text": "", "comments": [],
                    "ocr_text": [], "transcript": None, "media_paths": [], "meta": {}}
        return _fetch

    monkeypatch.setattr(core, "_adapter_fetch", fake_adapter_fetch)
    monkeypatch.setattr(N, "enrich_ocr",
                        lambda r: (calls.__setitem__("ocr", calls["ocr"] + 1) or r))
    monkeypatch.setattr(N, "enrich_transcribe",
                        lambda r, **k: (calls.__setitem__("transcribe", calls["transcribe"] + 1) or r))
    return calls


def test_smart_default_tiktok_injects_download_and_enriches(monkeypatch):
    calls = _cap(monkeypatch)
    core.fetch("https://www.tiktok.com/@x/photo/1")
    assert calls["platform"] == "tiktok"
    assert calls["kwargs"]["download"] is True
    assert calls["kwargs"]["media_dir"] == core._SMART_DEFAULT_MEDIA_DIR
    assert calls["ocr"] == 1 and calls["transcribe"] == 1  # smart → 둘 다 인리치


def test_smart_youtube_injects_video_subs_transcript_comments(monkeypatch):
    calls = _cap(monkeypatch)
    core.fetch("https://youtube.com/watch?v=x")
    k = calls["kwargs"]
    assert k["with_video"] is True and k["with_subs"] is True
    assert k["with_transcript"] is True and k["with_comments"] is True


def test_smart_instagram_injects_comments_and_download(monkeypatch):
    calls = _cap(monkeypatch)
    core.fetch("https://instagram.com/p/x")
    assert calls["kwargs"]["comments"] is True
    assert calls["kwargs"]["download"] is True


def test_user_kwarg_overrides_smart_injection(monkeypatch):
    # 사용자가 명시한 어댑터 kwarg가 smart 주입을 이긴다(override 금지).
    calls = _cap(monkeypatch)
    core.fetch("https://www.tiktok.com/@x/photo/1", download=False)
    assert calls["kwargs"]["download"] is False


def test_explicit_ocr_false_skips_ocr_but_keeps_transcribe(monkeypatch):
    calls = _cap(monkeypatch)
    core.fetch("https://www.tiktok.com/@x/photo/1", ocr=False)
    assert calls["ocr"] == 0          # 명시 False override
    assert calls["transcribe"] == 1   # transcribe는 여전히 smart 위임(True)


def test_smart_false_is_opt_in(monkeypatch):
    # smart=False면 주입 없음 + ocr/transcribe 기본 False(현행 opt-in 시맨틱).
    calls = _cap(monkeypatch)
    core.fetch("https://www.tiktok.com/@x/photo/1", smart=False)
    assert "download" not in calls["kwargs"]
    assert "media_dir" not in calls["kwargs"]
    assert calls["ocr"] == 0 and calls["transcribe"] == 0


def test_naver_gets_media_dir_for_download(monkeypatch):
    # naver는 media_dir 존재가 다운로드 트리거 → smart가 기본 경로를 채워야 한다.
    calls = _cap(monkeypatch)
    core.fetch("https://blog.naver.com/x/1")
    assert calls["platform"] == "naver_blog"
    assert calls["kwargs"]["media_dir"] == core._SMART_DEFAULT_MEDIA_DIR


def test_web_fallback_no_media_injection(monkeypatch):
    # web은 미디어 다운로드 비목표 — download/media_dir 주입 없음.
    calls = _cap(monkeypatch)
    core.fetch("https://example.com/article")
    assert calls["platform"] == "web"
    assert "download" not in calls["kwargs"]
    assert "media_dir" not in calls["kwargs"]


if __name__ == "__main__":
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
            fn(mp)
            print(f"PASS {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        finally:
            mp.undo()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)


# ── round-34 smart contract repair tests ──

import importlib
from inspect import signature
from pathlib import Path

import pytest


def _signature_cap(monkeypatch):
    """Adapter fake that still binds against the real adapter fetch signature."""
    calls = {"platform": None, "kwargs": None, "ocr": 0, "transcribe": 0}

    def fake_adapter_fetch(platform):
        real_fetch = getattr(importlib.import_module(f"adapters.{platform}"), "fetch")
        sig = signature(real_fetch)

        def _fetch(url, **kwargs):
            sig.bind(url, **kwargs)
            calls["platform"] = platform
            calls["kwargs"] = dict(kwargs)
            return {"source": url, "platform": platform, "body_text": "", "comments": [],
                    "ocr_text": [], "transcript": None, "media_paths": [], "meta": {}}
        return _fetch

    monkeypatch.setattr(core, "_adapter_fetch", fake_adapter_fetch)
    monkeypatch.setattr(N, "enrich_ocr",
                        lambda r: (calls.__setitem__("ocr", calls["ocr"] + 1) or r))
    monkeypatch.setattr(N, "enrich_transcribe",
                        lambda r, **k: (calls.__setitem__("transcribe", calls["transcribe"] + 1) or r))
    return calls


def test_r34_common_download_none_delegates_to_smart_for_tiktok(monkeypatch):
    calls = _signature_cap(monkeypatch)
    core.fetch("https://www.tiktok.com/@x/photo/1", download=None)
    assert calls["platform"] == "tiktok"
    assert calls["kwargs"]["download"] is True


def test_r34_common_download_false_translates_to_youtube_with_video_false(monkeypatch):
    calls = _signature_cap(monkeypatch)
    core.fetch("https://youtube.com/watch?v=abcdefghijk", download=False)
    assert "download" not in calls["kwargs"]
    assert calls["kwargs"].get("with_video") is False


def test_r34_common_comments_false_translates_to_youtube_with_comments_false(monkeypatch):
    calls = _signature_cap(monkeypatch)
    core.fetch("https://youtube.com/watch?v=abcdefghijk", comments=False)
    assert "comments" not in calls["kwargs"]
    assert calls["kwargs"].get("with_comments") is False


def test_r34_common_transcribe_false_suppresses_youtube_with_transcript(monkeypatch):
    calls = _signature_cap(monkeypatch)
    core.fetch("https://youtube.com/watch?v=abcdefghijk", transcribe=False)
    assert calls["kwargs"].get("with_transcript") is False
    assert calls["transcribe"] == 0


def test_r34_local_transcribe_false_is_respected(tmp_path, monkeypatch):
    media = tmp_path / "clip_false.mp4"
    media.write_bytes(b"fake")
    calls = {"transcribe": 0}

    import core.local as L

    monkeypatch.setattr(
        L,
        "fetch_local",
        lambda path, **kwargs: {"source": path, "platform": "local", "body_text": "",
                                "comments": [], "ocr_text": [], "transcript": None,
                                "media_paths": [path], "meta": {}},
    )
    monkeypatch.setattr(N, "enrich_ocr", lambda r: r)
    monkeypatch.setattr(N, "enrich_transcribe",
                        lambda r, **k: (calls.__setitem__("transcribe", calls["transcribe"] + 1) or r))

    core.fetch(str(media), transcribe=False, ocr=False)
    assert calls["transcribe"] == 0


def test_r34_local_default_transcribes(tmp_path, monkeypatch):
    media = tmp_path / "clip_default.mp4"
    media.write_bytes(b"fake")
    calls = {"transcribe": 0}

    import core.local as L

    monkeypatch.setattr(
        L,
        "fetch_local",
        lambda path, **kwargs: {"source": path, "platform": "local", "body_text": "",
                                "comments": [], "ocr_text": [], "transcript": None,
                                "media_paths": [path], "meta": {}},
    )
    monkeypatch.setattr(N, "enrich_ocr", lambda r: r)
    monkeypatch.setattr(N, "enrich_transcribe",
                        lambda r, **k: (calls.__setitem__("transcribe", calls["transcribe"] + 1) or r))

    core.fetch(str(media), ocr=False)
    assert calls["transcribe"] == 1


def test_r34_smart_youtube_comments_default_is_first_comment(monkeypatch):
    calls = _signature_cap(monkeypatch)
    core.fetch("https://youtube.com/watch?v=abcdefghijk")
    assert calls["kwargs"]["with_comments"] is True
    assert calls["kwargs"]["max_comments"] == 1


def test_r34_smart_caps_match_adapter_signatures():
    for platform, caps in core._SMART_CAPS.items():
        if platform == "web":
            continue
        fetch = getattr(importlib.import_module(f"adapters.{platform}"), "fetch")
        params = signature(fetch).parameters
        for group in ("media", "comments"):
            for name in (caps.get(group) or {}):
                assert name in params, f"{platform} smart kwarg drift: {name}"


def _r34_base_result(*, image_count: int, media_paths: list[str]) -> dict:
    return {"source": "u", "platform": "x", "body_text": "", "comments": [],
            "ocr_text": [], "transcript": None, "media_paths": media_paths,
            "meta": {"image_count": image_count}}


def test_r34_ocr_label_not_downloaded_when_images_known_but_no_files():
    out = N.enrich_ocr(_r34_base_result(image_count=3, media_paths=[]))
    assert out["meta"]["ocr_label"] == "not_downloaded"


def test_r34_ocr_label_partial_uses_declared_image_count(tmp_path, monkeypatch):
    img = tmp_path / "partial.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(N._ocr_ensemble, "is_available", lambda: True)
    monkeypatch.setattr(N._ocr_ensemble, "ocr_image_ensemble",
                        lambda path: {"text": "ok", "model": "fake"})
    out = N.enrich_ocr(_r34_base_result(image_count=2, media_paths=[str(img)]))
    assert out["meta"]["ocr_label"] == "partial"
    assert len(out["ocr_text"]) == 1


def test_r34_ocr_label_done_when_declared_images_all_processed(tmp_path, monkeypatch):
    img = tmp_path / "done.jpg"
    img.write_bytes(b"fake")
    monkeypatch.setattr(N._ocr_ensemble, "is_available", lambda: True)
    monkeypatch.setattr(N._ocr_ensemble, "ocr_image_ensemble",
                        lambda path: {"text": "ok", "model": "fake"})
    out = N.enrich_ocr(_r34_base_result(image_count=1, media_paths=[str(img)]))
    assert out["meta"]["ocr_label"] == "done"


def test_r34_ocr_label_none_when_image_count_zero():
    out = N.enrich_ocr(_r34_base_result(image_count=0, media_paths=[]))
    assert out["meta"]["ocr_label"] == "none"


def test_r34_transcribe_does_not_erase_adapter_failure_label():
    result = {"source": "u", "platform": "youtube", "body_text": "", "comments": [],
              "ocr_text": [], "transcript": None, "media_paths": [],
              "meta": {"transcript_label": "fetch_failed", "video_label": "download_failed"}}
    out = N.enrich_transcribe(result)
    assert out["meta"]["transcript_label"] == "fetch_failed"
    assert out["meta"]["video_label"] == "download_failed"


def test_r34_unsupported_comments_label_is_added_for_naver_smart(monkeypatch):
    def fake_adapter_fetch(platform):
        def _fetch(url, **kwargs):
            return {"source": url, "platform": platform, "body_text": "", "comments": [],
                    "ocr_text": [], "transcript": None, "media_paths": [],
                    "meta": {"comment_count": 4}}
        return _fetch

    monkeypatch.setattr(core, "_adapter_fetch", fake_adapter_fetch)
    monkeypatch.setattr(N, "enrich_ocr", lambda r: r)
    monkeypatch.setattr(N, "enrich_transcribe", lambda r, **k: r)

    out = core.fetch("https://blog.naver.com/someone/123")
    assert out["meta"]["comments_label"] == "unsupported"


def test_r34_cli_no_ocr_and_no_transcribe_pass_false(monkeypatch, capsys):
    import core.__main__ as cli

    captured = {}

    def fake_fetch(url, **kwargs):
        captured.update(kwargs)
        return {"source": url, "platform": "tiktok", "body_text": "", "comments": [],
                "ocr_text": [], "transcript": None, "media_paths": [], "meta": {}}

    monkeypatch.setattr(cli, "fetch", fake_fetch)
    monkeypatch.setattr(cli, "render_markdown", lambda result, **kwargs: "ok\n")

    rc = cli.main([
        "fetch",
        "https://www.tiktok.com/@x/photo/1",
        "--no-ocr",
        "--no-transcribe",
    ])

    assert rc == 0
    assert captured["ocr"] is False
    assert captured["transcribe"] is False
    assert captured["smart"] is True
    assert capsys.readouterr().out == "ok\n"


# ── round-34 재게이트 P1: 전 플랫폼 × 공통옵션 × None/True/False 매트릭스 ───────
# 1차 R34 테스트는 TikTok/YouTube 일부 조합만 커버해 다른 플랫폼의 번역·opt-out
# 회귀를 못 잡았다(재게이트 실측). 실제 어댑터 시그니처에 바인딩되는지가 핵심 —
# `_signature_cap`의 `sig.bind()`가 실패하면 이 테스트 자체가 TypeError로 죽는다.

_PLATFORM_URLS = {
    "tiktok": "https://www.tiktok.com/@x/photo/1",
    "threads": "https://www.threads.net/@x/post/1",
    "instagram": "https://www.instagram.com/p/ABC123/",
    "facebook": "https://www.facebook.com/watch/?v=1",
    "youtube": "https://youtube.com/watch?v=abcdefghijk",
    "naver_blog": "https://blog.naver.com/x/1",
}


@pytest.mark.parametrize("platform", sorted(_PLATFORM_URLS))
@pytest.mark.parametrize("download", [None, True, False])
def test_r34_matrix_download_binds_real_signature(monkeypatch, platform, download):
    calls = _signature_cap(monkeypatch)
    core.fetch(_PLATFORM_URLS[platform], download=download)
    assert calls["platform"] == platform
    real_fetch = getattr(importlib.import_module(f"adapters.{platform}"), "fetch")
    if "download" not in signature(real_fetch).parameters:
        assert "download" not in calls["kwargs"]  # 공통 kwarg가 미지원 어댑터로 새면 안 됨


@pytest.mark.parametrize("platform", sorted(_PLATFORM_URLS))
@pytest.mark.parametrize("comments", [None, True, False])
def test_r34_matrix_comments_binds_real_signature(monkeypatch, platform, comments):
    calls = _signature_cap(monkeypatch)
    core.fetch(_PLATFORM_URLS[platform], comments=comments)
    assert calls["platform"] == platform
    real_fetch = getattr(importlib.import_module(f"adapters.{platform}"), "fetch")
    if "comments" not in signature(real_fetch).parameters:
        assert "comments" not in calls["kwargs"]


@pytest.mark.parametrize("platform", sorted(_PLATFORM_URLS))
@pytest.mark.parametrize("transcribe", [None, True, False])
def test_r34_matrix_transcribe_binds_real_signature(monkeypatch, platform, transcribe):
    calls = _signature_cap(monkeypatch)
    core.fetch(_PLATFORM_URLS[platform], transcribe=transcribe, ocr=False)
    assert calls["platform"] == platform
