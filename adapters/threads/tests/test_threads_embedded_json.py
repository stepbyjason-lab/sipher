"""R49: 포스트 페이지 내장 JSON의 새 그릇(2026-10 관측)에서 원글·저자 연속글·댓글을 줍는다.

고정 입력은 진단(`.handoff/incidents/2026-10-07-threads-root-not-found.md`) 때 본 실제 구조를
줄인 것이다. 이름·값은 가명이다.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters.threads.fast_scrape import parse_post  # noqa: E402
from adapters.threads.media_utils import iter_thread_posts  # noqa: E402
from adapters.threads.scrape import assess  # noqa: E402

URL = "https://www.threads.com/@author_a/post/ROOT01"


def _post(code, username, text, *, reply_count=0, self_thread=None, replies=None):
    tpai = {"direct_reply_count": reply_count}
    if self_thread is not None:
        tpai["self_thread"] = {"posts": {"edges": [{"node": p} for p in self_thread]}}
    if replies is not None:
        # 댓글 묶음 node는 code가 없는 컨테이너이고, 실제 댓글은 그 안의 posts.edges[].node다.
        tpai["direct_replies"] = {"edges": [
            {"node": {"id": f"thread-{r['code']}", "posts": {"edges": [{"node": r}]}}} for r in replies
        ]}
    return {
        "id": f"{code}_1", "pk": f"{code}_1", "code": code,
        "user": {"username": username, "id": f"u-{username}"},
        "caption": {"text": text}, "taken_at": 1790000000,
        "text_post_app_info": tpai,
    }


def _page():
    cont1 = _post("CONT01", "author_a", "연속글 1", reply_count=1)
    cont2 = _post("CONT02", "author_a", "연속글 2")
    reply = _post("REPLY01", "other_b", "댓글")
    root = _post("ROOT01", "author_a", "원글", reply_count=1,
                 self_thread=[cont1, cont2, dict(cont1)], replies=[reply])
    related_same_author = _post("FEED01", "author_a", "추천 피드의 같은 저자 글")
    related_other = _post("FEED02", "other_c", "추천 피드 글")
    data = {
        "media": root,
        "relatedPosts": {"threads": [
            {"id": "t1", "thread_items": [{"post": related_same_author}]},
            {"id": "t2", "thread_items": [{"post": related_other}]},
        ]},
    }
    return {"require": [["ScheduledServerJS", "handle", None, [
        {"__bbox": {"require": [["RelayPrefetchedStreamCache", "next", [], [
            "adp_key", {"__bbox": {"complete": True, "result": {"data": data}}},
        ]]]}},
    ]]]}


def test_new_layout_yields_root_continuations_replies_and_skips_related_feed():
    posts = iter_thread_posts(json.loads(json.dumps(_page())))
    assert [p["code"] for p in posts] == ["ROOT01", "CONT01", "CONT02", "REPLY01"]


def test_new_layout_reaches_root_through_fast_parser():
    parsed = [parse_post(p) for p in iter_thread_posts(_page())]
    a = assess(parsed, URL)
    assert a["root_found"] is True
    assert a["expected"] == 1  # direct_reply_count가 새 그릇에서도 살아 있다
    assert {p["author"] for p in parsed if p["code"] != "REPLY01"} == {"author_a"}


def test_legacy_thread_items_layout_is_unchanged():
    legacy = {"data": {"data": {"containing_thread": {"thread_items": [
        {"post": {"id": "1", "code": "A"}}, {"post": {"id": "2", "code": "B"}}, {"post": {"id": "1", "code": "A"}},
    ]}}, "relatedPosts": {"threads": [{"thread_items": [{"post": {"id": "3", "code": "C"}}]}]}}}
    assert [p["code"] for p in iter_thread_posts(legacy)] == ["A", "B"]


def test_media_or_node_without_post_shape_is_not_a_post():
    data = {"media": {"id": "m", "url": "x"}, "edges": [{"node": {"id": "n", "posts": {"edges": []}}}]}
    assert iter_thread_posts(data) == []
