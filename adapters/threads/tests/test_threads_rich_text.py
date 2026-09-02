"""R41: Threads rich-text attachment와 제한 원저자 continuation 회귀 테스트."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import adapters.threads as threads  # noqa: E402
from adapters.threads.fast_scrape import parse_post as parse_fast_post  # noqa: E402
from adapters.threads.threads_scraper_v2 import parse_post as parse_deep_post  # noqa: E402


def _raw_post(*, code="POST", author="alice", caption="프롬프트:", fragments=None,
              snippet="full prompt", replies=0):
    fragments = list(fragments if fragments is not None else [caption])
    return {
        "id": f"id-{code}",
        "code": code,
        "caption": {"text": caption},
        "user": {"username": author},
        "like_count": 0,
        "image_versions2": {"candidates": []},
        "video_versions": None,
        "text_post_app_info": {
            "direct_reply_count": replies,
            "text_fragments": {"fragments": [{"plaintext": text} for text in fragments]},
            "snippet_attachment_info": {
                "text_fragments": {"fragments": [{"plaintext": snippet}]},
            },
        },
    }


def test_fast_and_deep_parsers_preserve_all_rich_text_sources_without_duplicate_caption():
    raw = _raw_post(caption="프롬프트:", fragments=["프롬프트:", "일반 확장 본문"], snippet="full prompt")
    for parser in (parse_fast_post, parse_deep_post):
        post = parser(raw)
        assert post["text"] == "프롬프트:\n\n일반 확장 본문\n\nfull prompt"
        assert post["text_blocks"] == [
            {"source": "caption", "text": "프롬프트:"},
            {"source": "text_fragment", "text": "일반 확장 본문"},
            {"source": "snippet_attachment", "text": "full prompt"},
        ]


def test_snippet_only_post_is_not_dropped_when_caption_is_empty():
    raw = _raw_post(caption="", snippet="full prompt")
    post = parse_fast_post(raw)
    assert post is not None
    assert post["text"] == "full prompt"
    assert post["text_blocks"] == [{"source": "snippet_attachment", "text": "full prompt"}]


def _post(*, code, author="alice", text="", replies=0, blocks=None, taken_at=None):
    return {
        "id": f"id-{code}", "code": code, "author": author, "text": text,
        "text_blocks": list(blocks or []), "likes": 0, "reply_count": replies,
        "images": [], "videos": [], "taken_at": taken_at,
    }


def test_default_author_only_resolves_author_followup_without_deep_or_other_authors():
    root = _post(code="ROOT", text="root", replies=20)
    teaser = _post(code="V3", text="V3 teaser", replies=1)
    other = _post(code="BOB", author="bob", text="other", replies=1)
    prompt = _post(
        code="V3PROMPT", text="V3 label\n\nfull V3 prompt",
        blocks=[
            {"source": "caption", "text": "V3 label"},
            {"source": "snippet_attachment", "text": "full V3 prompt"},
        ],
    )
    calls = []

    def fake_run(url, *, deep, auto, max_pages, **_kwargs):
        calls.append((url, deep, auto))
        if url.endswith("/ROOT"):
            return [root, teaser, other], False
        if url.endswith("/V3"):
            return [teaser, prompt, other], False
        raise AssertionError(f"unexpected continuation URL: {url}")

    assessment = {"root_found": True, "expected": 2, "captured": 2, "incomplete": False}
    with patch.object(threads, "_run_scrape", side_effect=fake_run), \
         patch("adapters.threads.scrape.assess", return_value=assessment):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT")

    assert calls == [
        ("https://www.threads.net/@alice/post/ROOT", False, False),
        ("https://www.threads.net/@alice/post/V3", False, False),
    ]
    assert [post["code"] for post in result["author_thread"]] == ["V3", "V3PROMPT"]
    assert result["author_thread"][1]["text_blocks"][-1] == {
        "source": "snippet_attachment", "text": "full V3 prompt"
    }
    assert result["comments"] == []
    resolution = result["meta"]["author_thread"]["resolution"]
    assert resolution["mode"] == "fast_author_continuation"
    assert resolution["attempted_pages"] == 1
    assert resolution["discovered_posts"] == 1


def test_continuation_merge_returns_author_thread_in_source_time_order():
    """R43-H1: continuation이 뒤늦게 merge한 더 이른 글도 원문 시간순 자리로 간다."""
    root = _post(code="ROOT", text="root", replies=20, taken_at=1000)
    v1 = _post(code="V1", text="V1", replies=1, taken_at=1100)
    v4 = _post(code="V4", text="V4", taken_at=1400)
    # fast pass는 V1/V4만 보고, V1 재조회에서 그보다 늦게 V2/V3가 발견된다.
    v2 = _post(code="V2", text="V2", taken_at=1200)
    v3 = _post(code="V3", text="V3", taken_at=1300)

    def fake_run(url, *, deep, auto, max_pages, **_kwargs):
        if url.endswith("/ROOT"):
            return [root, v1, v4], False
        if url.endswith("/V1"):
            return [v1, v3, v2], False
        raise AssertionError(f"unexpected continuation URL: {url}")

    assessment = {"root_found": True, "expected": 2, "captured": 2, "incomplete": False}
    with patch.object(threads, "_run_scrape", side_effect=fake_run),          patch("adapters.threads.scrape.assess", return_value=assessment):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT")

    codes = [post["code"] for post in result["author_thread"]]
    assert codes == ["V1", "V2", "V3", "V4"]
    assert result["meta"]["author_thread"]["codes"] == codes


def test_explicit_collection_mode_does_not_run_author_continuation_resolver():
    root = _post(code="ROOT", text="root", replies=20)
    teaser = _post(code="V3", text="V3 teaser", replies=1)
    assessment = {"root_found": True, "expected": 1, "captured": 1, "incomplete": False}
    with patch.object(threads, "_run_scrape", return_value=([root, teaser], False)) as run, \
         patch("adapters.threads.scrape.assess", return_value=assessment):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT", all_comments=True)
    run.assert_called_once()
    assert result["meta"]["author_thread"]["resolution"] == {
        "mode": "not_run", "status": "not_run", "reason": "explicit_collection_mode"
    }


def test_author_continuation_failure_is_exposed_in_resolution_metadata():
    root = _post(code="ROOT", text="root", replies=1)
    teaser = _post(code="V5", text="V5 teaser", replies=1)
    assessment = {"root_found": True, "expected": 1, "captured": 1, "incomplete": False}

    def fake_run(url, **_kwargs):
        if url.endswith("/ROOT"):
            return [root, teaser], False
        raise RuntimeError("blocked")

    with patch.object(threads, "_run_scrape", side_effect=fake_run), \
         patch("adapters.threads.scrape.assess", return_value=assessment):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT")

    resolution = result["meta"]["author_thread"]["resolution"]
    assert resolution["attempted_pages"] == 1
    assert resolution["failed_pages"] == [{"code": "V5", "reason": "RuntimeError"}]


def test_author_continuation_empty_child_result_is_not_reported_as_success():
    root = _post(code="ROOT", text="root", replies=1)
    teaser = _post(code="V5", text="V5 teaser", replies=1)

    def fake_run(url, **_kwargs):
        if url.endswith("/ROOT"):
            return [root, teaser], False
        return [], False

    with patch.object(threads, "_run_scrape", side_effect=fake_run), \
         patch("adapters.threads.scrape.assess", side_effect=[
             {"root_found": True, "expected": 1, "captured": 1, "incomplete": False},
             {"root_found": False, "expected": 0, "captured": 0, "incomplete": True},
         ]):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT")

    assert result["meta"]["author_thread"]["resolution"]["failed_pages"] == [
        {"code": "V5", "reason": "root_not_found"}
    ]


def test_root_failure_stops_before_author_continuation_resolution():
    with patch.object(threads, "_run_scrape", return_value=([], False)), \
         patch.object(threads, "_resolve_author_continuations") as resolver, \
         patch("adapters.threads.scrape.assess", return_value={"root_found": False}):
        try:
            threads.fetch("https://www.threads.net/@alice/post/ROOT")
        except RuntimeError as exc:
            assert "root 포스트" in str(exc)
        else:
            raise AssertionError("root failure must raise")
    resolver.assert_not_called()


def test_author_continuation_page_budget_reports_the_ninth_candidate_as_unresolved():
    root = _post(code="ROOT", text="root", replies=1)
    candidates = [_post(code=f"P{i}", text=f"teaser {i}", replies=1) for i in range(9)]
    calls = []

    def fake_run(url, **_kwargs):
        calls.append(url)
        if url.endswith("/ROOT"):
            return [root, *candidates], False
        code = url.rsplit("/", 1)[-1]
        return [next(post for post in candidates if post["code"] == code)], False

    with patch.object(threads, "_run_scrape", side_effect=fake_run), \
         patch("adapters.threads.scrape.assess", return_value={"root_found": True, "incomplete": False}):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT")

    resolution = result["meta"]["author_thread"]["resolution"]
    assert len(calls) == 1 + threads._AUTHOR_CONTINUATION_MAX_PAGES
    assert resolution["attempted_pages"] == threads._AUTHOR_CONTINUATION_MAX_PAGES == 8
    assert resolution["page_budget_exhausted"] is True
    assert resolution["unresolved_candidates"] == 1


def test_author_continuation_hop_limit_is_observed_at_the_third_hop():
    root = _post(code="ROOT", text="root", replies=1)
    first = _post(code="P1", text="first", replies=1)
    second = _post(code="P2", text="second", replies=1)
    third = _post(code="P3", text="third", replies=1)

    def fake_run(url, **_kwargs):
        if url.endswith("/ROOT"):
            return [root, first], False
        if url.endswith("/P1"):
            return [first, second], False
        if url.endswith("/P2"):
            return [second, third], False
        raise AssertionError(f"hop limit should prevent: {url}")

    with patch.object(threads, "_run_scrape", side_effect=fake_run), \
         patch("adapters.threads.scrape.assess", return_value={"root_found": True, "incomplete": False}):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT")

    resolution = result["meta"]["author_thread"]["resolution"]
    assert resolution["hop_limit"] == 2
    assert resolution["attempted_pages"] == 2
    assert resolution["hop_limit_reached"] is True


def test_author_continuation_time_budget_before_next_child_returns_partial_without_deep():
    teaser = _post(code="P1", text="teaser", replies=1)
    with patch.object(threads.time, "monotonic", side_effect=[0.0, 46.0, 46.0]), \
         patch.object(threads, "_run_scrape") as run:
        posts, resolution = threads._resolve_author_continuations(
            [teaser], author="alice", root_code="ROOT", started_at=0.0,
        )

    run.assert_not_called()
    assert posts == [teaser]
    assert resolution["status"] == "partial"
    assert resolution["partial_reason"] == "time_budget_exhausted"
    assert resolution["time_budget_exhausted"] is True
    assert resolution["unresolved_candidates"] == 1


def test_time_budget_partial_emits_progress_event_with_partial_metadata():
    teaser = _post(code="P1", text="teaser", replies=1)
    events = []
    with patch.object(threads.time, "monotonic", side_effect=[0.0, 46.0, 46.0, 46.0]), \
         patch.object(threads, "_run_scrape") as run:
        _posts, resolution = threads._resolve_author_continuations(
            [teaser], author="alice", root_code="ROOT", progress=events.append, started_at=0.0,
        )

    run.assert_not_called()
    assert resolution["status"] == "partial"
    assert events == [{
        "event": "continuation_partial", "elapsed_ms": 46000,
        "partial_reason": "time_budget_exhausted",
        "partial_reasons": ["time_budget_exhausted"],
        "unresolved_candidates": 1, "elapsed_ms_continuation": 46000,
    }]


def test_author_continuation_overrunning_child_returns_partial_after_child_completes():
    teaser = _post(code="P1", text="teaser", replies=1)
    with patch.object(threads.time, "monotonic", side_effect=[0.0, 0.0, 46.0, 46.0]), \
         patch.object(threads, "_run_scrape", return_value=([teaser], False)) as run, \
         patch("adapters.threads.scrape.assess", return_value={"root_found": True}):
        _posts, resolution = threads._resolve_author_continuations(
            [teaser], author="alice", root_code="ROOT", started_at=0.0,
        )

    run.assert_called_once()
    assert resolution["attempted_pages"] == 1
    assert resolution["status"] == "partial"
    assert resolution["partial_reason"] == "time_budget_exhausted"
    assert resolution["elapsed_ms"] == 46000


def test_fetch_emits_progress_for_root_and_author_continuation_lifecycle():
    root = _post(code="ROOT", text="root", replies=1)
    teaser = _post(code="P1", text="teaser", replies=1)
    events = []

    def fake_run(url, **_kwargs):
        if url.endswith("/ROOT"):
            return [root, teaser], False
        return [teaser], False

    with patch.object(threads, "_run_scrape", side_effect=fake_run), \
         patch("adapters.threads.scrape.assess", return_value={"root_found": True, "incomplete": False}):
        result = threads.fetch("https://www.threads.net/@alice/post/ROOT", progress=events.append)

    assert result["meta"]["author_thread"]["resolution"]["status"] == "complete"
    names = [event["event"] for event in events]
    assert names[0] == "share_resolved"
    assert "fast_started" in names and "fast_complete" in names
    assert "continuation_started" in names and "continuation_complete" in names
    assert names[-1] == "collection_complete"
    assert all("elapsed_ms" in event for event in events)
    forbidden = {"body_text", "ocr_text", "cookie", "cookies", "cookie_value"}
    assert all(forbidden.isdisjoint(event) for event in events)


def test_adapter_cli_progress_uses_stderr_without_breaking_json_stdout(monkeypatch, capsys):
    import json
    from adapters.threads import cli

    def fake_fetch(url, **kwargs):
        kwargs["progress"]({"event": "fast_started", "elapsed_ms": 0, "post_code": "ROOT"})
        kwargs["progress"]({"event": "collection_complete", "elapsed_ms": 1, "status": "complete"})
        return {"source": url, "platform": "threads", "body_text": "body", "author_thread": [],
                "comments": [], "ocr_text": [], "transcript": None, "media_paths": [], "meta": {}}

    monkeypatch.setattr(cli, "fetch", fake_fetch)
    rc = cli.main(["fetch", "https://www.threads.net/@alice/post/ROOT"])

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["platform"] == "threads"
    progress_lines = [json.loads(line) for line in captured.err.splitlines() if line.startswith("{")]
    assert [line["event"] for line in progress_lines] == ["fast_started", "collection_complete"]
    assert all(line["type"] == "progress" for line in progress_lines)


def test_adapter_cli_help_explains_progress_and_partial_contract(capsys):
    import pytest
    from adapters.threads import cli

    with pytest.raises(SystemExit) as exited:
        cli.main(["fetch", "--help"])
    assert exited.value.code == 0
    help_text = capsys.readouterr().out
    for expected in ("stderr", "progress", "partial", "meta.author_thread.resolution.status", "partial_reason"):
        assert expected in help_text


def test_public_fetch_docstring_explains_callback_and_partial_contract():
    doc = threads.fetch.__doc__ or ""
    for expected in ("progress", "callback", "callback 예외", 'status == "partial"', "partial_reason", "RuntimeError"):
        assert expected in doc


def test_threads_handoff_indexes_record_completed_rounds_and_unnumbered_ocr_backlog():
    """완료된 R43/R43-H1 이력과 기존 R42 LATEST 포인터를 각각 보존한다.

    `마지막 완료 라운드`는 이후 라운드가 완료될 때마다 바뀌는 현재 상태이므로 여기서
    고정하지 않는다. `LATEST.md`는 아직 R42 기획서를 가리키므로 해당 표기는 그대로
    검증한다.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    roadmap = (repo_root / ".handoff" / "ROADMAP.md").read_text(encoding="utf-8")
    latest = (repo_root / ".handoff" / "rounds" / "LATEST.md").read_text(encoding="utf-8")

    assert "R40-H2" in roadmap
    assert "## ✅ R43 완료" in roadmap
    assert "## ✅ R43-H1 완료" in roadmap
    assert "### 조건부·미번호 백로그 — 프리미엄 벤더 OCR judge 벤치마크·선정" in roadmap
    assert "### R41 — 프리미엄 벤더 OCR judge 벤치마크·선정" not in roadmap
    assert "round-42-threads-continuation-time-budget-plan-lite.md" in latest
    assert "R42 완료" in latest


def test_r43_public_docs_explain_threads_partial_consumption():
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    readme_en = (repo_root / "README.md").read_text(encoding="utf-8")
    readme_ko = (repo_root / "README.ko.md").read_text(encoding="utf-8")
    overview = (repo_root / "adapters" / "threads" / "docs" / "00-overview.md").read_text(encoding="utf-8")
    for text in (readme_en, readme_ko, overview):
        assert "meta.author_thread.resolution.status" in text
        assert "partial_reason" in text
        assert "stderr" in text
