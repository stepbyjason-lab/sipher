"""round-A: Threads author-continuation 분리(`author_thread[]`) 단위 테스트.

Playwright 불필요 — normalize()/fetch() 승격 로직만 순수 함수/mock으로 격리 검증한다.
실행: `pytest adapters/threads/tests/` 또는 이 파일 직접(`python
adapters/threads/tests/test_threads_normalize.py`).
"""
from __future__ import annotations

import os
import sys

# repo root를 path에 추가(어디서 실행하든 adapters.threads import 가능) — facebook
# 테스트(adapters/facebook/tests/test_comments_label.py)와 동일한 관례.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters.threads import normalize  # noqa: E402


def _post(*, code, author=None, text="", likes=0, reply_count=0, **extra) -> dict:
    """normalize()가 소비하는 vendored post dict shape의 최소 fixture."""
    base = {
        "id": f"id-{code}",
        "code": code,
        "author": author,
        "text": text,
        "likes": likes,
        "reply_count": reply_count,
    }
    base.update(extra)
    return base


# ── 분류: same-author non-root → author_thread[], other-author → comments[] ──

def test_same_author_continuation_separated_from_comments():
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="C1", author="alice", text="continuation 1"),
        _post(code="C2", author="alice", text="continuation 2"),
        _post(code="B1", author="bob", text="bob reply"),
        _post(code="C3", author="carol", text="carol reply"),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert result["body_text"] == "root"
    assert len(result["author_thread"]) == 2
    assert [p["text"] for p in result["author_thread"]] == [
        "continuation 1",
        "continuation 2",
    ]
    assert len(result["comments"]) == 2
    assert [c["author"] for c in result["comments"]] == ["bob", "carol"]
    # same-author post가 comments[]에 새지 않았는지 명시적으로 확인.
    assert "alice" not in [c["author"] for c in result["comments"]]


def test_comments_item_shape_matches_author_thread_item_shape():
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="C1", author="alice", text="continuation", likes=5, reply_count=1),
        _post(code="B1", author="bob", text="reply", likes=2, reply_count=0),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    expected_keys = {"id", "code", "author", "text", "likes", "reply_count", "media_paths", "text_blocks"}
    assert set(result["author_thread"][0].keys()) == expected_keys
    assert set(result["comments"][0].keys()) == expected_keys


def test_text_blocks_are_preserved_without_changing_existing_text_field():
    blocks = [
        {"source": "caption", "text": "프롬프트:"},
        {"source": "snippet_attachment", "text": "full prompt"},
    ]
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="C1", author="alice", text="프롬프트:\n\nfull prompt", text_blocks=blocks),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")
    item = result["author_thread"][0]
    assert item["text"] == "프롬프트:\n\nfull prompt"
    assert item["text_blocks"] == blocks


# ── root author fallback: root에 author 없을 때 함수 인자 author로 대체 ──

def test_root_author_fallback_to_function_argument():
    posts = [
        _post(code="ROOT", author=None, text="root"),
        _post(code="C1", author="alice", text="continuation"),
        _post(code="B1", author="bob", text="reply"),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert len(result["author_thread"]) == 1
    assert result["author_thread"][0]["text"] == "continuation"
    assert len(result["comments"]) == 1
    assert result["meta"]["author_thread"]["author_match_available"] is True
    assert result["meta"]["author_thread"]["root_author"] == "alice"


# ── author 완전 부재: 기존 호환성 유지, author_thread 분리 안 함 ──

def test_missing_author_everywhere_preserves_legacy_comments_behavior():
    posts = [
        _post(code="ROOT", author=None, text="root"),
        _post(code="C1", author="alice", text="post by alice"),
        _post(code="C2", author=None, text="unknown author post"),
    ]
    result = normalize(posts, source="src", author="", code="ROOT")

    assert result["author_thread"] == []
    assert len(result["comments"]) == 2
    assert result["meta"]["author_thread"]["author_match_available"] is False
    # unknown author count: root 제외 non-root 중 author 없는 것 1개(C2).
    assert result["meta"]["author_thread"]["unknown_author_count"] == 1


# ── meta completeness: count/deep/possible_more silent false 금지 ──

def test_meta_completeness_fields_present_and_honest():
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="C1", author="alice", text="continuation 1"),
        _post(code="C2", author="alice", text="continuation 2"),
    ]
    result = normalize(
        posts, source="src", author="alice", code="ROOT",
        assessment={"root_found": True, "incomplete": True},
        deep=False, max_pages=None,
    )

    meta = result["meta"]["author_thread"]
    assert meta["count"] == len(result["author_thread"]) == 2
    assert meta["deep"] is False
    # fast + incomplete=True 이므로 possible_more는 silent False가 아니라 True/None.
    assert meta["possible_more"] in (True, None)
    assert meta["possible_more"] is not False


def test_possible_more_false_only_when_deep_and_assess_says_complete():
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="C1", author="alice", text="continuation"),
    ]
    result = normalize(
        posts, source="src", author="alice", code="ROOT",
        assessment={"root_found": True, "incomplete": False},
        deep=True, max_pages=100,
    )
    assert result["meta"]["author_thread"]["possible_more"] is False


# ── 기존 comments[] 호환성: 타인 댓글 shape·top-level 필드 보존 ──

def test_existing_top_level_schema_fields_untouched():
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="B1", author="bob", text="bob reply"),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    for key in ("source", "platform", "body_text", "comments", "ocr_text",
                "transcript", "media_paths", "meta"):
        assert key in result
    assert result["platform"] == "threads"
    assert result["comments"][0]["author"] == "bob"


# ── 원문 시간순(R43-H1): author_thread[]는 raw taken_at 오름차순 ──

def test_author_thread_sorted_by_taken_at_regardless_of_discovery_order():
    """fast 응답 도착 순서가 역순이어도 원저자 게시 시각순으로 반환한다."""
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C3", author="alice", text="third", taken_at=1300),
        _post(code="B1", author="bob", text="bob reply", taken_at=1250),
        _post(code="C1", author="alice", text="first", taken_at=1100),
        _post(code="C2", author="alice", text="second", taken_at=1200),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["text"] for p in result["author_thread"]] == ["first", "second", "third"]
    # body_text(root)와 comments[]는 이 정렬의 영향을 받지 않는다.
    assert result["body_text"] == "root"
    assert [c["text"] for c in result["comments"]] == ["bob reply"]


def test_continuation_appended_later_moves_to_its_chronological_position():
    """continuation resolver가 merged.append()로 뒤늦게 더한 더 이른 글도 제자리로."""
    root = _post(code="ROOT", author="alice", text="root", taken_at=1000)
    v1 = _post(code="V1", author="alice", text="V1", taken_at=1100)
    v4 = _post(code="V4", author="alice", text="V4", taken_at=1400)
    v5 = _post(code="V5", author="alice", text="V5", taken_at=1500)
    # fast pass가 V1/V4/V5를 먼저 발견하고, continuation이 V2/V3를 뒤에 append한 상태.
    v2 = _post(code="V2", author="alice", text="V2", taken_at=1200)
    v3 = _post(code="V3", author="alice", text="V3", taken_at=1300)
    posts = [root, v1, v4, v5, v2, v3]

    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["code"] for p in result["author_thread"]] == ["V1", "V2", "V3", "V4", "V5"]


def test_equal_taken_at_preserves_original_discovery_order():
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C3", author="alice", text="third", taken_at=1100),
        _post(code="C1", author="alice", text="first", taken_at=1100),
        _post(code="C2", author="alice", text="second", taken_at=1100),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    # stable sort — 동률이면 재정렬하지 않는다.
    assert [p["text"] for p in result["author_thread"]] == ["third", "first", "second"]


def test_missing_taken_at_preserves_original_discovery_order():
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="C3", author="alice", text="third"),
        _post(code="C1", author="alice", text="first"),
        _post(code="C2", author="alice", text="second", taken_at=None),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["text"] for p in result["author_thread"]] == ["third", "first", "second"]


def test_malformed_taken_at_does_not_raise_and_keeps_discovery_order():
    """문자열·dict·NaN 등 정렬 불가 값이 섞여도 예외 없이 발견 순서를 지킨다."""
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C3", author="alice", text="third", taken_at="not-a-number"),
        _post(code="C1", author="alice", text="first", taken_at={"bad": "shape"}),
        _post(code="C2", author="alice", text="second", taken_at=float("nan")),
        _post(code="C4", author="alice", text="fourth", taken_at=True),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["text"] for p in result["author_thread"]] == [
        "third", "first", "second", "fourth",
    ]


def test_infinite_taken_at_keeps_discovery_slot_among_finite_ones():
    """±무한대는 "가장 이르다/늦다"가 아니라 시각 미상 — 발견 자리에 남는다."""
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C3", author="alice", text="third", taken_at=1300),
        _post(code="PINF", author="alice", text="+inf", taken_at=float("inf")),
        _post(code="C1", author="alice", text="first", taken_at=1100),
        _post(code="NINF", author="alice", text="-inf", taken_at=float("-inf")),
        _post(code="C2", author="alice", text="second", taken_at=1200),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["text"] for p in result["author_thread"]] == [
        "first", "+inf", "second", "-inf", "third",
    ]


def test_non_finite_taken_at_strings_keep_discovery_slot_among_finite_ones():
    """float()이 받아주는 무한/오버플로 문자열도 시각 미상으로 떨어뜨린다."""
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C3", author="alice", text="third", taken_at=1300),
        _post(code="S1", author="alice", text="Infinity", taken_at="Infinity"),
        _post(code="C1", author="alice", text="first", taken_at=1100),
        _post(code="S2", author="alice", text="-inf str", taken_at="-inf"),
        _post(code="S3", author="alice", text="overflow", taken_at="1e309"),
        _post(code="C2", author="alice", text="second", taken_at=1200),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["text"] for p in result["author_thread"]] == [
        "first", "Infinity", "second", "-inf str", "overflow", "third",
    ]


def test_int_taken_at_beyond_float_range_keeps_discovery_slot():
    """float()이 OverflowError를 내는 임의정밀 int도 예외 없이 시각 미상."""
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C2", author="alice", text="second", taken_at=1200),
        _post(code="HUGE", author="alice", text="huge", taken_at=10 ** 400),
        _post(code="C1", author="alice", text="first", taken_at=1100),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["text"] for p in result["author_thread"]] == ["first", "huge", "second"]


def test_numeric_string_taken_at_is_sorted_numerically_not_lexically():
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at="1000"),
        _post(code="C1", author="alice", text="later", taken_at="1100"),
        _post(code="C2", author="alice", text="earlier", taken_at="900"),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["text"] for p in result["author_thread"]] == ["earlier", "later"]


def test_posts_with_unknown_taken_at_keep_their_slot_among_sorted_ones():
    """시각 미상 항목은 발견 순서 자리에 남고, 시각을 아는 항목만 서로 재배치된다."""
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C3", author="alice", text="third", taken_at=1300),
        _post(code="CX", author="alice", text="unknown"),
        _post(code="C1", author="alice", text="first", taken_at=1100),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert [p["text"] for p in result["author_thread"]] == ["first", "unknown", "third"]


def test_meta_author_thread_codes_match_returned_author_thread_order():
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C3", author="alice", text="third", taken_at=1300),
        _post(code="B1", author="bob", text="bob reply", taken_at=1250),
        _post(code="C1", author="alice", text="first", taken_at=1100),
        _post(code="C2", author="alice", text="second", taken_at=1200),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    codes = [p["code"] for p in result["author_thread"]]
    assert codes == ["C1", "C2", "C3"]
    assert result["meta"]["author_thread"]["codes"] == codes


def test_public_item_shape_gains_no_ordering_metadata_field():
    """정렬은 내부 정책 — taken_at/sequence 같은 새 공개 필드를 노출하지 않는다."""
    posts = [
        _post(code="ROOT", author="alice", text="root", taken_at=1000),
        _post(code="C1", author="alice", text="continuation", taken_at=1100),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    assert set(result["author_thread"][0].keys()) == {
        "id", "code", "author", "text", "likes", "reply_count", "media_paths", "text_blocks",
    }


# ── self_reply_ambiguous: 허위 정밀도 주장 금지 라벨 ──

def test_self_reply_ambiguous_flag_present_when_classification_happened():
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="C1", author="alice", text="continuation"),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")
    meta = result["meta"]["author_thread"]
    assert meta["self_reply_ambiguous"] is True
    assert "reply-target" in meta["self_reply_ambiguous_reason"] or \
        "답글" in meta["self_reply_ambiguous_reason"]


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
