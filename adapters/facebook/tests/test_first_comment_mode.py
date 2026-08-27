"""round-B: Facebook 첫 댓글 모드(comments=True → 첫 top-level 댓글 1개만 수집) 단위 테스트.

계약: .handoff/rounds/round-B-first-comment-mode-contract.md §Facebook 테스트.
Playwright 실제 브라우저를 띄우지 않는다 — page.evaluate()/page.locator()를 fake로 대체.
실행: `pytest adapters/facebook/tests/` 또는 이 파일 직접.
"""
from __future__ import annotations

import os
import sys

# repo root를 path에 추가(어디서 실행하든 adapters.facebook import 가능) —
# test_comments_label.py와 동일 관례.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters.facebook import _maybe_fetch_comments, normalize  # noqa: E402
from adapters.facebook.scrape import _COLLECT_COMMENTS_JS, extract_comments  # noqa: E402


def _candidate(raw_text: str, *, profile_href: str | None = "https://www.facebook.com/alice"):
    return {"raw_text": raw_text, "profile_href": profile_href}


class _FakeLocator:
    """page.locator(...).filter(...).first.click(...) 체인 흉내.

    max_expand=0 테스트에서는 이 객체가 생성/호출되는 것 자체가 실패를 뜻한다 —
    _FakePage.locator()가 아예 호출되지 않아야 하므로 이 클래스 인스턴스화만으로도
    버그를 드러낸다(계약 #2: max_expand=0일 때 click이 호출되지 않는다).
    """

    def __init__(self):
        self.click_count = 0

    def filter(self, has_text=None):
        return self

    def count(self):
        return 0  # 버튼 없음(자연 완료) — 실제로 호출되면 안 되는 코드 경로이므로 안전한 기본값

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.click_count += 1


class _FakePage:
    """page.evaluate(_COLLECT_COMMENTS_JS, caption_snippet) + page.locator(...) 흉내.

    evaluate_result: _COLLECT_COMMENTS_JS가 반환할 {candidates, body_matched, caption_given} dict.
    """

    def __init__(self, evaluate_result: dict, *, raise_on_locator: bool = False):
        self._evaluate_result = evaluate_result
        self._raise_on_locator = raise_on_locator
        self.locator_call_count = 0
        self._locator = _FakeLocator()

    def evaluate(self, script, *args):
        assert script == _COLLECT_COMMENTS_JS  # 우리가 대체하려는 JS가 맞는지 확인
        return self._evaluate_result

    def locator(self, selector):
        self.locator_call_count += 1
        if self._raise_on_locator:
            raise AssertionError(
                "page.locator() should not be called when max_expand=0 (first-only mode)")
        return self._locator

    def wait_for_timeout(self, ms):
        pass


# ── 1~4: extract_comments(limit=1, max_expand=0) — 첫 댓글 1개만 반환 ──


def test_extract_comments_limit1_returns_only_first_comment():
    """(계약 #1,#3) candidates가 여러 개여도 limit=1이면 반환 comments 길이는 1."""
    page = _FakePage({
        "candidates": [
            _candidate("홍길동\n첫 댓글입니다\n15시간\n좋아요\n답글 달기"),
            _candidate("김철수\n두 번째 댓글\n14시간\n좋아요\n답글 달기"),
            _candidate("이영희\n세 번째 댓글\n13시간\n좋아요\n답글 달기"),
        ],
        "body_matched": True,
        "caption_given": True,
    })

    comments, label = extract_comments(page, "caption snippet", limit=1, max_expand=0)

    assert len(comments) == 1
    # (계약 #4) 첫 번째 유효 candidate의 텍스트/author가 반환된다.
    assert comments[0]["author"] == "홍길동"
    assert comments[0]["text"] == "첫 댓글입니다"
    assert label == "collected"


def test_max_expand_zero_skips_reply_expansion_click():
    """(계약 #2) max_expand=0일 때 page.locator(...).filter(...).first.click(...)이 호출되지 않는다."""
    page = _FakePage({
        "candidates": [_candidate("홍길동\n댓글\n1시간\n좋아요\n답글 달기")],
        "body_matched": True,
        "caption_given": True,
    }, raise_on_locator=True)  # locator() 호출 자체가 실패하도록 강제

    comments, label = extract_comments(page, "caption snippet", limit=1, max_expand=0)

    assert page.locator_call_count == 0  # 확장 루프가 range(0)이라 locator 자체를 안 부름
    assert len(comments) == 1
    assert label == "collected"


def test_first_candidate_parse_failure_falls_through_to_next():
    """첫 candidate가 파싱 실패(빈 텍스트)해도 표시 순서상 다음 candidate로 넘어가
    "첫 유효 댓글"을 반환한다(계약 §Facebook 필수변경 마지막 항목)."""
    page = _FakePage({
        "candidates": [
            _candidate("15시간\n좋아요\n답글 달기"),  # 메타뿐 — 본문 없음 → 파싱 실패
            _candidate("김철수\n실제 첫 유효 댓글\n14시간\n좋아요\n답글 달기"),
        ],
        "body_matched": True,
        "caption_given": True,
    })

    comments, label = extract_comments(page, "caption snippet", limit=1, max_expand=0)

    assert len(comments) == 1
    assert comments[0]["author"] == "김철수"
    assert comments[0]["text"] == "실제 첫 유효 댓글"


# ── 5: enrich_post_comments / _maybe_fetch_comments가 first-only 설정을 전달 ──


def test_maybe_fetch_comments_calls_enrich_with_limit1_max_expand0(monkeypatch):
    """(계약 #5) _maybe_fetch_comments() 호출 경로가 limit=1, max_expand=0을 전달한다."""
    calls = []

    def fake_enrich_post_comments(ctx, permalink, *, caption_snippet=None,
                                   max_expand=999, limit=None):
        calls.append({"max_expand": max_expand, "limit": limit})
        return {"comments": [{"id": None, "author": "a", "text": "b",
                              "likes": None, "reply_count": 0, "media_paths": []}],
                "comments_label": "collected"}

    import adapters.facebook.scrape as scrape_mod
    monkeypatch.setattr(scrape_mod, "enrich_post_comments", fake_enrich_post_comments)

    result = _maybe_fetch_comments(object(), "https://www.facebook.com/x/posts/1", comments=True)

    assert result is not None
    assert len(calls) == 1
    assert calls[0]["limit"] == 1
    assert calls[0]["max_expand"] == 0


def test_maybe_fetch_comments_returns_none_when_comments_false(monkeypatch):
    """comments=False면 enrich_post_comments 자체가 호출되지 않는다."""
    def fail_if_called(*a, **kw):
        raise AssertionError("enrich_post_comments should not be called when comments=False")

    import adapters.facebook.scrape as scrape_mod
    monkeypatch.setattr(scrape_mod, "enrich_post_comments", fail_if_called)

    result = _maybe_fetch_comments(object(), "https://www.facebook.com/x/posts/1", comments=False)
    assert result is None


# ── 6: normalize() 결과 meta 필드 ──


def test_normalize_sets_comment_collection_mode_first_only():
    """(계약 #6) normalize() 결과에 meta.comment_collection_mode == "first_only"와
    meta.comment_count_captured in (0, 1)이 들어간다."""
    post = {
        "permalink": "https://www.facebook.com/x/posts/1",
        "text": "본문",
        "comments_raw": [
            {"id": "id1", "author": "홍길동", "text": "첫 댓글", "likes": 3,
             "reply_count": 0, "media_paths": []},
        ],
        "comments_label": "collected",
    }
    out = normalize(post, source="https://www.facebook.com/x/posts/1")

    assert out["meta"]["comment_collection_mode"] == "first_only"
    assert out["meta"]["comment_count_captured"] in (0, 1)
    assert out["meta"]["comment_count_captured"] == 1
    assert len(out["comments"]) == 1


def test_normalize_not_requested_when_comments_false():
    """comments=False(기본, comments_raw 키 자체가 없음)면
    meta.comment_collection_mode == "not_requested"."""
    post = {"permalink": "https://www.facebook.com/x/posts/1", "text": "본문"}
    out = normalize(post, source="https://www.facebook.com/x/posts/1")

    assert out["meta"]["comment_collection_mode"] == "not_requested"
    assert out["meta"]["comments_label"] == "not_collected"
    assert out["comments"] == []


# ── 7: 기존 라벨 테스트 회귀 확인은 test_comments_label.py 자체 실행으로 별도 커버 ──
# (계약 #7: adapters/facebook/tests/test_comments_label.py는 계속 통과해야 한다 —
#  이 파일에서는 import하지 않는다. _decide_comments_label은 순수 함수라 여기서
#  건드릴 필요가 없다 — Amendment에서 이미 두 테스트가 분리돼 있음을 확인함.)


# ── 8: Amendment P2 — 댓글 0개 엣지케이스 ──


def test_zero_candidates_returns_empty_list_and_none_label_no_indexerror():
    """(Amendment P2) page.evaluate()가 candidates: []를 반환할 때
    extract_comments(page, ..., limit=1)이 IndexError 없이 ([], "none")을 반환한다."""
    page = _FakePage({
        "candidates": [],
        "body_matched": False,
        "caption_given": True,
    })

    comments, label = extract_comments(page, "caption snippet", limit=1, max_expand=0)

    assert comments == []
    assert label == "none"


if __name__ == "__main__":  # pytest 없이 직접 실행 가능(test_comments_label.py 관례)
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            if "monkeypatch" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                print(f"SKIP {fn.__name__} (monkeypatch fixture — pytest 필요)")
                continue
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError:
            fails += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
