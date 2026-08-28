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


# ── 순서 보존: author_thread[]는 원본 posts 순서를 그대로 유지 ──

def test_author_thread_preserves_original_posts_order():
    posts = [
        _post(code="ROOT", author="alice", text="root"),
        _post(code="C3", author="alice", text="third"),
        _post(code="B1", author="bob", text="bob reply"),
        _post(code="C1", author="alice", text="first"),
        _post(code="C2", author="alice", text="second"),
    ]
    result = normalize(posts, source="src", author="alice", code="ROOT")

    # 원본 리스트에 등장한 순서(third, first, second) 그대로 — text 기준 재정렬 아님.
    assert [p["text"] for p in result["author_thread"]] == ["third", "first", "second"]


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
