"""R48: Threads 결과 JSON에 원 게시 시각(`created_at_utc`)을 보존한다."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import adapters.threads as threads  # noqa: E402
from adapters.threads.fast_scrape import parse_post as parse_fast_post  # noqa: E402
from adapters.threads.threads_scraper_v2 import parse_post as parse_deep_post  # noqa: E402


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _post(*, code, author="alice", replies=0, taken_at=None):
    return {
        "id": f"id-{code}", "code": code, "author": author, "text": code,
        "text_blocks": [], "likes": 0, "reply_count": replies,
        "images": [], "videos": [], "taken_at": taken_at,
    }


def test_fast_and_deep_parsers_keep_raw_taken_at():
    raw = {"id": "1", "code": "P", "caption": {"text": "hi"}, "user": {"username": "alice"},
           "taken_at": 1756350765, "text_post_app_info": {}}
    for parser in (parse_fast_post, parse_deep_post):
        assert parser(raw)["taken_at"] == 1756350765


def test_normalize_exposes_utc_iso_for_root_author_thread_and_comments():
    posts = [
        _post(code="ROOT", taken_at=1756350765),
        _post(code="C1", taken_at="1756350800"),
        _post(code="B1", author="bob", taken_at=1756351000),
    ]
    result = threads.normalize(posts, source="src", author="alice", code="ROOT")

    assert result["meta"]["created_at_utc"] == _iso(1756350765) == "2025-08-28T03:12:45+00:00"
    assert result["author_thread"][0]["created_at_utc"] == _iso(1756350800)
    assert result["comments"][0]["created_at_utc"] == _iso(1756351000)


def test_unknown_or_invalid_taken_at_is_null_never_fetched_at():
    bad = [None, True, "not-a-number", {"x": 1}, float("nan"), float("inf"), "1e309",
           10 ** 400, 1e300, 0, -5]
    posts = [_post(code="ROOT")] + [_post(code=f"C{i}", taken_at=v) for i, v in enumerate(bad)]
    result = threads.normalize(posts, source="src", author="alice", code="ROOT")

    assert result["meta"]["created_at_utc"] is None
    assert result["meta"]["fetched_at"]
    assert len(result["author_thread"]) == len(bad)
    assert all("created_at_utc" in item and item["created_at_utc"] is None
               for item in result["author_thread"])


def test_normalize_without_root_reports_null_meta_created_at():
    result = threads.normalize([_post(code="C1", taken_at=1000)], source="src",
                               author="alice", code="ROOT")
    assert result["meta"]["created_at_utc"] is None


def test_fast_fetch_with_continuation_keeps_each_post_time():
    root = _post(code="ROOT", replies=5, taken_at=1000)
    teaser = _post(code="V1", replies=1, taken_at=1100)
    late = _post(code="V2", taken_at=1050)

    def fake_run(url, **_kwargs):
        return ([root, teaser] if url.endswith("/ROOT") else [teaser, late]), False

    with patch.object(threads, "_run_scrape", side_effect=fake_run), \
         patch("adapters.threads.scrape.assess", return_value={"root_found": True, "incomplete": False}):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT")

    assert result["meta"]["created_at_utc"] == _iso(1000)
    assert [(p["code"], p["created_at_utc"]) for p in result["author_thread"]] == [
        ("V2", _iso(1050)), ("V1", _iso(1100)),
    ]


def test_deep_fetch_keeps_post_times_for_author_thread_and_comments():
    posts = [_post(code="ROOT", taken_at=1000), _post(code="C1", taken_at=1200),
             _post(code="B1", author="bob", taken_at=1300)]
    with patch.object(threads, "_run_scrape", return_value=(posts, True)), \
         patch("adapters.threads.scrape.assess", return_value={"root_found": True, "incomplete": False}):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT", deep=True)

    assert result["meta"]["completeness"]["scrape_mode"] == "deep"
    assert result["meta"]["created_at_utc"] == _iso(1000)
    assert result["author_thread"][0]["created_at_utc"] == _iso(1200)
    assert result["comments"][0]["created_at_utc"] == _iso(1300)
