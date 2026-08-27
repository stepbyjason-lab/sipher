"""round-29 S2: TikTok content-type 사전신호(is_photo_post/image_count/has_video) 단위 테스트.

네트워크 불필요 — normalize()는 순수 함수(gallery-dl payload dict → 정규화 dict).
photo-mode 필드는 2026-07-13 spike로 실측 확정(post_type=="image" + imagePost.images[]).
실행: `pytest adapters/tiktok/tests/` 또는 이 파일 직접.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters.tiktok import normalize  # noqa: E402


def _photo_payload(n_images: int = 8) -> dict:
    return {
        "id": "7591842042624445716",
        "desc": "ChatGPT 갓 모드 프롬프트 7",
        "post_type": "image",
        "imagePost": {"images": [{"imageURL": {"urlList": ["http://x"]}} for _ in range(n_images)],
                      "cover": {}, "title": ""},
        "video": {"duration": 0},  # photo-mode는 video가 빈 골격
        "author": {"uniqueId": "ai.trend.kr"},
        "stats": {},
    }


def _video_payload() -> dict:
    return {
        "id": "123",
        "desc": "영상 캡션",
        "post_type": "video",
        "video": {"playAddr": "http://p", "downloadAddr": "http://d", "duration": 30},
        "author": {"uniqueId": "someone"},
        "stats": {},
    }


def test_photo_post_signals():
    r = normalize(_photo_payload(8), source="u", media_paths=[], downloaded=False)
    m = r["meta"]
    assert m["is_photo_post"] is True
    assert m["image_count"] == 8          # len(imagePost.images) — kind=3(+커버) 아님
    assert m["has_video"] is False


def test_video_post_signals():
    r = normalize(_video_payload(), source="u", media_paths=[], downloaded=False)
    m = r["meta"]
    assert m["is_photo_post"] is False
    assert m["image_count"] == 0
    assert m["has_video"] is True


def test_signals_present_without_download():
    # "프로브는 공짜" — 다운로드(media_paths=[]) 없이도 사전신호가 채워진다.
    r = normalize(_photo_payload(10), source="u", media_paths=[], downloaded=False)
    assert r["meta"]["image_count"] == 10
    assert r["media_paths"] == []


def test_comments_label_unsupported_honest():
    # round-29 D: TikTok 댓글은 미지원(R31) — 조용한 빈 배열이 아니라 정직 라벨.
    r = normalize(_photo_payload(), source="u", media_paths=[], downloaded=False)
    assert r["comments"] == []
    assert r["meta"]["comments_label"] == "unsupported"


def test_comment_notice_when_desc_points_to_comments():
    # R31: 댓글 수집은 미지원이지만, 본문이 "링크는 댓글에서" 식으로 댓글을 가리키면
    # 조용히 버리지 않고 원문 링크를 포함한 안내를 남긴다(정직 degrade).
    src = "https://www.tiktok.com/@u/video/1"
    p = _video_payload()
    p["desc"] = "사이트 링크는 댓글에서 바로 확인해보세요. Check the comments for the link."
    r = normalize(p, source=src, media_paths=[], downloaded=False)
    notice = r["meta"]["comment_notice"]
    assert notice is not None
    assert src in notice  # 원문 링크가 안내에 포함돼야 한다(사용자 요구)


def test_comment_notice_none_when_desc_unrelated():
    p = _video_payload()
    p["desc"] = "오늘 날씨가 좋네요"
    r = normalize(p, source="u", media_paths=[], downloaded=False)
    assert r["meta"]["comment_notice"] is None


def test_photo_fallback_without_post_type():
    # post_type가 없어도 imagePost.images가 있으면 photo로 본다(방어적 폴백).
    p = _photo_payload(3)
    del p["post_type"]
    r = normalize(p, source="u", media_paths=[], downloaded=False)
    assert r["meta"]["is_photo_post"] is True
    assert r["meta"]["image_count"] == 3


if __name__ == "__main__":
    import traceback
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
