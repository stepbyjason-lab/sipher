"""R46: 이미지·카드뉴스·carousel 게시물의 배경음은 transcript가 아니다.

세 플랫폼(TikTok·Instagram·Facebook)마다 실제 어댑터 `normalize()` 산출물을
`core.fetch` smart 경로에 태워 visual 대조군과 영상 대조군을 비교한다. 네트워크·
전사 backend는 쓰지 않는다 — `transcribe_media`를 가짜로 바꿔 호출 여부를 센다.
기획서: `.handoff/round-46-visual-post-ambient-audio-plan-lite.md`.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core  # noqa: E402
import core.normalize as N  # noqa: E402
from adapters import facebook, instagram, tiktok  # noqa: E402

SPEECH = "실제 말소리 전사"


@pytest.fixture
def run(monkeypatch):
    """adapter 결과 하나를 core.fetch smart 경로로 돌려 (결과, 전사 호출 수)를 돌려준다."""
    calls = {"transcribe": 0}

    def fake_transcribe(path, **kwargs):
        calls["transcribe"] += 1
        return {"text": SPEECH, "model": "fake", "backend": "local"}

    monkeypatch.setattr(N._transcribe, "is_available", lambda: True)
    monkeypatch.setattr(N._transcribe, "transcribe_media", fake_transcribe)
    monkeypatch.setattr(N, "enrich_ocr", lambda r: {**r, "ocr_text": ["카드 OCR"]})

    def _run(url, adapter_result, **fetch_kwargs):
        monkeypatch.setattr(core, "_adapter_fetch", lambda platform: (lambda u, **k: adapter_result))
        return core.fetch(url, **fetch_kwargs), calls["transcribe"]

    return _run


def _files(tmp_path, *names):
    paths = []
    for name in names:
        p = tmp_path / name
        p.write_bytes(b"x")
        paths.append(str(p))
    return paths


def _assert_visual_skipped(out, n_calls, *, audio):
    assert n_calls == 0
    assert out["transcript"] is None
    assert out["meta"]["content_primary"] == "visual"
    assert out["ocr_text"] == ["카드 OCR"]
    if audio:
        assert out["meta"]["transcript_label"] == "skipped_ambient_audio"
        assert out["meta"]["ambient_audio_paths"] == audio
        assert all(a in out["media_paths"] for a in audio)
    else:
        assert out["meta"]["transcript_label"] == "none"


def _assert_transcribed(out, n_calls, primary):
    assert n_calls == 1
    assert out["transcript"] == SPEECH
    assert out["meta"]["content_primary"] == primary
    assert out["meta"]["transcript_label"] == "done"
    assert "ambient_audio_paths" not in out["meta"]


# ── TikTok ──

def _tiktok_photo(n=9):
    return {"id": "7678255338000370965", "desc": "바이브코딩 UI 플러그인 5개", "post_type": "image",
            "imagePost": {"images": [{} for _ in range(n)]}, "video": {"duration": 0},
            "author": {"uniqueId": "ai.trend.kr"}, "stats": {}}


def _tiktok_video():
    return {"id": "1", "desc": "영상", "post_type": "video",
            "video": {"playAddr": "http://p", "downloadAddr": "http://d", "duration": 30},
            "author": {"uniqueId": "u"}, "stats": {}}


def test_tiktok_photo_post_background_mp3_not_transcribed(tmp_path, run):
    *imgs, mp3 = _files(tmp_path, *[f"{i}.jpg" for i in range(9)], "bgm.mp3")
    r = tiktok.normalize(_tiktok_photo(9), source="u", media_paths=[*imgs, mp3], downloaded=True)
    out, n = run("https://www.tiktok.com/@ai.trend.kr/photo/7678255338000370965", r)
    _assert_visual_skipped(out, n, audio=[mp3])
    assert out["body_text"] == "바이브코딩 UI 플러그인 5개"
    assert out["meta"]["image_count"] == 9


def test_tiktok_video_post_still_transcribed(tmp_path, run):
    mp4 = _files(tmp_path, "v.mp4")
    r = tiktok.normalize(_tiktok_video(), source="u", media_paths=mp4, downloaded=True)
    out, n = run("https://www.tiktok.com/@u/video/1", r)
    _assert_transcribed(out, n, "spoken")


def test_explicit_transcribe_true_does_not_override_visual(tmp_path, run):
    # 기획서 §4 ⓓ 기본값: 명시 전사도 배경음은 전사하지 않는다.
    img, mp3 = _files(tmp_path, "1.jpg", "bgm.mp3")
    r = tiktok.normalize(_tiktok_photo(1), source="u", media_paths=[img, mp3], downloaded=True)
    out, n = run("https://www.tiktok.com/@u/photo/1", r, transcribe=True)
    _assert_visual_skipped(out, n, audio=[mp3])


# ── Instagram ──

class _Node:
    def __init__(self, is_video):
        self.is_video = is_video


class _Post:
    caption = "캡션"
    owner_username = "alice"
    mediaid = 1
    likes = 0
    comments = 0
    date_utc = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __init__(self, *, is_video=False, nodes=None):
        self.is_video = is_video
        self._nodes = nodes
        self.typename = "GraphSidecar" if nodes is not None else "GraphImage"

    def get_sidecar_nodes(self):
        if self._nodes == "broken":
            raise RuntimeError("enumeration failed")
        return iter(self._nodes)


def _ig(post, media_paths):
    return instagram.normalize(
        post, source="u", code="ABC", comments=[], comments_label="not_requested",
        comment_collection_mode="not_requested", media_paths=media_paths,
        downloaded=True, has_media=True, access_label="ok",
    )


IG_URL = "https://www.instagram.com/p/ABC/"


def test_instagram_single_image_is_visual(tmp_path, run):
    out, n = run(IG_URL, _ig(_Post(), _files(tmp_path, "a.jpg")))
    _assert_visual_skipped(out, n, audio=None)


def test_instagram_image_carousel_audio_not_transcribed(tmp_path, run):
    # 지금 instagram 어댑터는 이미지 게시물 음악을 받지 않지만, 받게 되더라도 정책이 같다.
    a, b, m4a = _files(tmp_path, "a.jpg", "b.jpg", "music.m4a")
    out, n = run(IG_URL, _ig(_Post(nodes=[_Node(False), _Node(False)]), [a, b, m4a]))
    _assert_visual_skipped(out, n, audio=[m4a])


def test_instagram_mixed_carousel_still_transcribed(tmp_path, run):
    paths = _files(tmp_path, "a.jpg", "b.mp4")
    out, n = run(IG_URL, _ig(_Post(nodes=[_Node(False), _Node(True)]), paths))
    _assert_transcribed(out, n, "mixed")


def test_instagram_video_still_transcribed(tmp_path, run):
    out, n = run(IG_URL, _ig(_Post(is_video=True), _files(tmp_path, "r.mp4")))
    _assert_transcribed(out, n, "spoken")


def test_instagram_unknown_counts_keep_existing_policy(tmp_path, run):
    # 기획서 §4 ⓔ: carousel 열거 실패(개수 불명)는 visual로 찍지 않는다.
    out, n = run(IG_URL, _ig(_Post(nodes="broken"), _files(tmp_path, "x.mp4")))
    assert out["meta"]["content_primary"] is None
    assert n == 1 and out["transcript"] == SPEECH


# ── Facebook ──

FB_URL = "https://www.facebook.com/someone/posts/1"


def test_facebook_image_post_is_visual(tmp_path, run):
    imgs = _files(tmp_path, "a.jpg", "b.jpg")
    r = facebook.normalize({"text": "본문", "image_urls": ["u1", "u2"], "local_images": imgs},
                           source="u")
    out, n = run(FB_URL, r)
    _assert_visual_skipped(out, n, audio=None)
    assert out["body_text"] == "본문"


def test_facebook_video_post_still_transcribed(tmp_path, run):
    r = facebook.normalize({"text": "", "video_urls": ["v"], "local_videos": _files(tmp_path, "v.mp4")},
                           source="u")
    out, n = run(FB_URL, r)
    _assert_transcribed(out, n, "spoken")


# ── 판정 단위 ──

@pytest.mark.parametrize("meta, expected", [
    ({"is_photo_post": True, "image_count": 3, "has_video": False}, "visual"),
    ({"is_photo_post": False, "image_count": 0, "has_video": True}, "spoken"),
    ({"image_count": 2, "video_count": 1}, "mixed"),
    ({"image_count": 0, "video_count": 0}, None),
    ({"image_count": None, "video_count": None}, None),
])
def test_content_primary_rule(meta, expected):
    assert core._content_primary(meta) == expected


def test_content_primary_not_added_outside_three_platforms():
    out = core._apply_common_labels("threads", {"meta": {"image_count": 2, "video_count": 0}},
                                    smart=True, comments=None)
    assert "content_primary" not in out["meta"]
