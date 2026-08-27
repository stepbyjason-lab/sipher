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


def test_escalates_by_default_when_candidate_and_incomplete():
    p_fast, p_assess, p_deep = _patched(FAST_WITH_CANDIDATE, incomplete=True)
    with p_fast, p_assess, p_deep as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=False, auto=False, max_pages=100,
                                          author="alice", code="ROOT")
    mock_deep.assert_called_once()
    assert posts == DEEP_RESULT
    # post-review P1 fix: _run_scrape는 실제로 deep 경로를 탔는지도 반환해야 한다.
    assert actual_deep is True


def test_no_escalation_when_no_candidate_even_if_incomplete():
    p_fast, p_assess, p_deep = _patched(FAST_NO_CANDIDATE, incomplete=True)
    with p_fast, p_assess, p_deep as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=False, auto=False, max_pages=100,
                                          author="alice", code="ROOT")
    mock_deep.assert_not_called()
    assert posts == FAST_NO_CANDIDATE
    assert actual_deep is False


def test_no_escalation_when_candidate_present_but_complete():
    p_fast, p_assess, p_deep = _patched(FAST_WITH_CANDIDATE, incomplete=False)
    with p_fast, p_assess, p_deep as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=False, auto=False, max_pages=100,
                                          author="alice", code="ROOT")
    mock_deep.assert_not_called()
    assert posts == FAST_WITH_CANDIDATE
    assert actual_deep is False


def test_explicit_auto_true_still_escalates_on_incomplete_regardless_of_candidate():
    # 회귀 테스트: auto=True 사용자 명시 시 기존 동작(candidate 무관, incomplete만 보고 승격)이 우선.
    p_fast, p_assess, p_deep = _patched(FAST_NO_CANDIDATE, incomplete=True)
    with p_fast, p_assess, p_deep as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=False, auto=True, max_pages=100,
                                          author="alice", code="ROOT")
    mock_deep.assert_called_once()
    assert posts == DEEP_RESULT
    assert actual_deep is True


def test_explicit_deep_true_skips_fast_pass_entirely():
    # 회귀 테스트: deep=True는 fast_scrape.scrape를 아예 호출하지 않고 바로 deep.
    with patch("adapters.threads.scrape.fast_scrape.scrape",
               new=AsyncMock(return_value=FAST_WITH_CANDIDATE)) as mock_fast, \
         patch("adapters.threads.scrape.scrape_threads_recursive",
               new=AsyncMock(return_value=DEEP_RESULT)) as mock_deep:
        posts, actual_deep = _run_scrape(URL, deep=True, auto=False, max_pages=100,
                                          author="alice", code="ROOT")
    mock_fast.assert_not_called()
    mock_deep.assert_called_once()
    assert posts == DEEP_RESULT
    assert actual_deep is True


# ── post-review P1 fix: fetch()-level end-to-end — actual_deep must reach meta ──

def test_fetch_reports_deep_true_and_scrape_mode_deep_after_default_escalation():
    """post-review P1 (Codex meta review) 회귀 테스트.

    §5 기본-승격 경로(auto=False/deep=False인데 candidate+incomplete로 deep까지
    승격)를 fetch() 전체 경로로 실행했을 때, meta.author_thread.deep과
    meta.completeness.scrape_mode가 실제 실행된 deep 크롤을 정직하게 반영하는지
    확인한다. 수정 전에는 fetch()가 원래 인자 deep=False를 그대로 normalize()에
    넘겨 이 두 필드가 거짓으로 "fast"/False를 보고했다.
    """
    # fast pass: candidate(alice의 연속글) 있음, incomplete=True로 승격 트리거.
    fast_assess = {"root_found": True, "expected": 5, "captured": 1, "incomplete": True}
    # deep pass 이후 재호출되는 assess(): 완전해졌다고 가정(별개 관심사 — 실제 deep
    # 결과 posts에 대해 다시 assess가 호출되므로 두 번째 반환값을 따로 지정).
    deep_assess = {"root_found": True, "expected": 5, "captured": 2, "incomplete": False}

    with patch("adapters.threads.scrape.fast_scrape.scrape",
               new=AsyncMock(return_value=FAST_WITH_CANDIDATE)), \
         patch("adapters.threads.scrape.scrape_threads_recursive",
               new=AsyncMock(return_value=DEEP_RESULT)), \
         patch("adapters.threads.scrape.assess",
               side_effect=[fast_assess, deep_assess]) as mock_assess:
        result = fetch("https://www.threads.net/@alice/post/ROOT", max_pages=100)

    # assess()가 두 번 호출됨을 확인: 1) fast 결과에 대해(에스컬레이션 판단),
    # 2) fetch()가 deep 결과에 대해 재계산(root_found 체크용).
    assert mock_assess.call_count == 2

    meta_at = result["meta"]["author_thread"]
    assert meta_at["deep"] is True, "actual_deep=True인데 meta.author_thread.deep이 거짓 False를 보고함"
    assert meta_at["max_pages"] == 100

    scrape_mode = result["meta"]["completeness"]["scrape_mode"]
    assert scrape_mode == "deep", f"실제 deep 크롤 실행됐는데 scrape_mode={scrape_mode!r} (fast로 오보)"

    # author_thread 분류 자체도 deep 크롤 결과(DEEP_RESULT, continuation 2개) 기준으로 맞는지.
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
