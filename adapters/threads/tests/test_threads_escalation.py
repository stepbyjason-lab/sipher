"""round-A §5: Threads fetch() 기본 deep 승격 트리거 단위 테스트.

`_run_scrape()`는 asyncio.run()을 내부에서 호출하는 동기 함수라 pytest-asyncio 없이
직접 호출 가능하다. vendored fast_scrape.scrape()/scrape_threads_recursive()는
playwright가 필요하므로 monkeypatch로 대체해 실제 네트워크 없이 승격 분기만
검증한다. 실행: `pytest adapters/threads/tests/` 또는 이 파일 직접.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters.threads import _run_scrape, fetch  # noqa: E402

URL = "https://www.threads.net/@alice/post/ROOT"

FAST_WITH_CANDIDATE = [
    {"id": "r", "code": "ROOT", "author": "alice", "text": "root",
     "likes": 0, "reply_count": 5, "images": [], "videos": []},
    {"id": "c1", "code": "C1", "author": "alice", "text": "continuation",
     "likes": 0, "reply_count": 0, "images": [], "videos": []},
]

FAST_NO_CANDIDATE = [
    {"id": "r", "code": "ROOT", "author": "alice", "text": "root",
     "likes": 0, "reply_count": 1, "images": [], "videos": []},
    {"id": "b1", "code": "B1", "author": "bob", "text": "reply",
     "likes": 0, "reply_count": 0, "images": [], "videos": []},
]

DEEP_RESULT = [
    {"id": "r", "code": "ROOT", "author": "alice", "text": "root",
     "likes": 0, "reply_count": 5, "images": [], "videos": []},
    {"id": "c1", "code": "C1", "author": "alice", "text": "continuation",
     "likes": 0, "reply_count": 0, "images": [], "videos": []},
    {"id": "c2", "code": "C2", "author": "alice", "text": "continuation2",
     "likes": 0, "reply_count": 0, "images": [], "videos": []},
]


def _patched(fast_return, incomplete):
    """fast_scrape.scrape / assess / scrape_threads_recursive 를 함께 patch."""
    return (
        patch("adapters.threads.scrape.fast_scrape.scrape",
              new=AsyncMock(return_value=fast_return)),
        patch("adapters.threads.scrape.assess",
              return_value={"root_found": True, "expected": 5,
                            "captured": len(fast_return) - 1, "incomplete": incomplete}),
        patch("adapters.threads.scrape.scrape_threads_recursive",
              new=AsyncMock(return_value=DEEP_RESULT)),
    )


def test_no_escalation_by_default_even_with_candidate_and_incomplete():
    p_fast, p_assess, p_deep = _patched(FAST_WITH_CANDIDATE, incomplete=True)
    with p_fast, p_assess, p_deep as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=False, auto=False, max_pages=100)
    mock_deep.assert_not_called()
    assert posts == FAST_WITH_CANDIDATE
    assert actual_deep is False


def test_no_escalation_when_no_candidate_even_if_incomplete():
    p_fast, p_assess, p_deep = _patched(FAST_NO_CANDIDATE, incomplete=True)
    with p_fast, p_assess, p_deep as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=False, auto=False, max_pages=100)
    mock_deep.assert_not_called()
    assert posts == FAST_NO_CANDIDATE
    assert actual_deep is False


def test_no_escalation_when_candidate_present_but_complete():
    p_fast, p_assess, p_deep = _patched(FAST_WITH_CANDIDATE, incomplete=False)
    with p_fast, p_assess, p_deep as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=False, auto=False, max_pages=100)
    mock_deep.assert_not_called()
    assert posts == FAST_WITH_CANDIDATE
    assert actual_deep is False


def test_default_fetch_is_fast_and_author_only():
    # raw fast 수집은 댓글 2개를 모두 잡았고 complete다. 저자 전용 필터가 Bob 댓글을
    # 숨겨도 meta.completeness는 이 원본 수집 사실을 보존해야 한다.
    fast_assess = {"root_found": True, "expected": 2, "captured": 2, "incomplete": False}
    mixed = FAST_WITH_CANDIDATE + [
        {"id": "b1", "code": "B1", "author": "bob", "text": "other reply",
         "likes": 0, "reply_count": 0, "images": [], "videos": []},
    ]
    with patch("adapters.threads.scrape.fast_scrape.scrape",
               new=AsyncMock(return_value=mixed)):
        with patch("adapters.threads.scrape.assess", return_value=fast_assess):
            result = fetch("https://www.threads.net/@alice/post/ROOT")
    assert result["body_text"] == "root"
    assert len(result["author_thread"]) == 1
    assert result["comments"] == []
    completeness = result["meta"]["completeness"]
    assert completeness["incomplete"] is False
    assert completeness["author_only"] is True
    assert completeness["comments_filtered"] == 1


def test_fetch_all_comments_includes_other_authors():
    fast_assess = {"root_found": True, "expected": 2, "captured": 2, "incomplete": False}
    mixed = FAST_WITH_CANDIDATE + [
        {"id": "b1", "code": "B1", "author": "bob", "text": "other reply",
         "likes": 0, "reply_count": 0, "images": [], "videos": []},
    ]
    with patch("adapters.threads.scrape.fast_scrape.scrape",
                new=AsyncMock(return_value=mixed)):
        with patch("adapters.threads.scrape.assess", return_value=fast_assess):
            result = fetch("https://www.threads.net/@alice/post/ROOT", all_comments=True)
    assert result["body_text"] == "root"
    assert len(result["author_thread"]) == 1
    assert len(result["comments"]) == 1
    assert result["comments"][0]["author"] == "bob"
    assert "author_only" not in result["meta"]["completeness"]


def test_explicit_auto_true_still_escalates_on_incomplete_regardless_of_candidate():
    # 회귀 테스트: auto=True 사용자 명시 시 기존 동작(candidate 무관, incomplete만 보고 승격)이 우선.
    p_fast, p_assess, p_deep = _patched(FAST_NO_CANDIDATE, incomplete=True)
    with p_fast, p_assess, p_deep as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=False, auto=True, max_pages=100)
    mock_deep.assert_called_once()
    assert posts == DEEP_RESULT
    assert actual_deep is True


def test_explicit_deep_true_skips_fast_pass_entirely():
    # 회귀 테스트: deep=True는 fast_scrape.scrape를 아예 호출하지 않고 바로 deep.
    with patch("adapters.threads.scrape.fast_scrape.scrape",
               new=AsyncMock(return_value=FAST_WITH_CANDIDATE)) as mock_fast, \
         patch("adapters.threads.scrape.scrape_threads_recursive",
               new=AsyncMock(return_value=DEEP_RESULT)) as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=True, auto=False, max_pages=100)
    mock_fast.assert_not_called()
    mock_deep.assert_called_once()
    assert posts == DEEP_RESULT
    assert actual_deep is True


# ── post-review P1 fix: fetch()-level end-to-end — actual_deep must reach meta ──

def test_fetch_reports_deep_true_when_explicit_auto_escalates():
    """auto=True 명시 시 incomplete이면 deep으로 승격되고 meta에 반영된다."""
    fast_assess = {"root_found": True, "expected": 5, "captured": 1, "incomplete": True}
    deep_assess = {"root_found": True, "expected": 5, "captured": 2, "incomplete": False}

    with patch("adapters.threads.scrape.fast_scrape.scrape",
               new=AsyncMock(return_value=FAST_WITH_CANDIDATE)), \
         patch("adapters.threads.scrape.scrape_threads_recursive",
               new=AsyncMock(return_value=DEEP_RESULT)), \
         patch("adapters.threads.scrape.assess",
               side_effect=[fast_assess, deep_assess]) as mock_assess:
        result = fetch("https://www.threads.net/@alice/post/ROOT", auto=True, max_pages=100)

    assert mock_assess.call_count == 2
    meta_at = result["meta"]["author_thread"]
    assert meta_at["deep"] is True
    assert meta_at["max_pages"] == 100
    scrape_mode = result["meta"]["completeness"]["scrape_mode"]
    assert scrape_mode == "deep"
    assert len(result["author_thread"]) == 2


def test_fetch_reports_deep_false_when_no_escalation_happens():
    """대조군: 승격이 일어나지 않으면 meta.author_thread.deep은 여전히 False."""
    fast_assess = {"root_found": True, "expected": 1, "captured": 1, "incomplete": False}

    with patch("adapters.threads.scrape.fast_scrape.scrape",
               new=AsyncMock(return_value=FAST_NO_CANDIDATE)), \
         patch("adapters.threads.scrape.scrape_threads_recursive",
               new=AsyncMock(return_value=DEEP_RESULT)) as mock_deep, \
         patch("adapters.threads.scrape.assess", return_value=fast_assess):
        result = fetch("https://www.threads.net/@alice/post/ROOT", max_pages=100)

    mock_deep.assert_not_called()
    assert result["meta"]["author_thread"]["deep"] is False
    assert result["meta"]["completeness"]["scrape_mode"] == "fast"


if __name__ == "__main__":  # pytest 없이 직접 실행 가능
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError:
            fails += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
