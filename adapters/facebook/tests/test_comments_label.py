"""round-16: _decide_comments_label 순수 함수 단위 테스트.

Playwright 불필요(scrape.py는 playwright를 TYPE_CHECKING으로만 import) — 라벨
판정 로직만 격리 검증한다. 실행: `pytest adapters/facebook/tests/` 또는 이 파일 직접.
"""
from __future__ import annotations

import os
import sys

# repo root를 path에 추가(어디서 실행하든 adapters.facebook import 가능) — web engine 테스트 관례.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters.facebook.scrape import _decide_comments_label  # noqa: E402


def _label(**over):
    """기본값(성공 케이스) 위에 override — 각 테스트가 관심 변수만 지정."""
    base = dict(raw_count=3, comment_count=3, expand_hit_cap=False,
                expand_interrupted=False, parse_fail=0, body_matched=True,
                caption_given=True)
    base.update(over)
    return _decide_comments_label(**base)


# ── none vs fetch_failed (round-16 #4: '댓글0'과 '보강실패' 구분) ──

def test_zero_articles_is_none():
    # 본문 이후 댓글 article이 0개 = 진짜 빈 상태
    assert _label(raw_count=0, comment_count=0) == "none"


def test_candidates_but_all_parse_failed_is_fetch_failed():
    # 후보 article은 있었는데 하나도 파싱 못함 = 추출 degrade (not 빈 상태)
    assert _label(raw_count=5, comment_count=0, parse_fail=5) == "fetch_failed"


def test_one_candidate_zero_comment_is_fetch_failed():
    assert _label(raw_count=1, comment_count=0) == "fetch_failed"


# ── collected ──

def test_clean_collection_is_collected():
    assert _label() == "collected"


def test_no_caption_given_still_collected():
    # 캡션 미지정(caption_given=False)이면 body_matched=False여도 저신뢰로 안 떨어뜨림
    assert _label(caption_given=False, body_matched=False) == "collected"


# ── partial: 불완전 신호들 ──

def test_expand_hit_cap_is_partial():
    assert _label(expand_hit_cap=True) == "partial"


def test_expand_interrupted_is_partial():
    # round-14 M2 회귀 방지 — 강제중단 신호가 partial로 흐르는지
    assert _label(expand_interrupted=True) == "partial"


def test_parse_fail_is_partial():
    assert _label(parse_fail=1) == "partial"


def test_caption_given_but_body_unmatched_is_partial():
    # round-16 #5: 캡션 주어졌는데 본문 매칭 실패로 idx0 폴백 = 저신뢰 partial
    assert _label(caption_given=True, body_matched=False) == "partial"


# ── 경계: comment_count==0이면 다른 신호보다 우선(none/fetch_failed 판정) ──

def test_zero_comment_ignores_expand_signals():
    assert _label(raw_count=0, comment_count=0, expand_hit_cap=True) == "none"


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
