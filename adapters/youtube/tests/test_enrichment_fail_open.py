"""round-38B: YouTube optional enrichment fail-open regression tests."""
from __future__ import annotations

import builtins
import os
import sys
from types import ModuleType

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters import youtube
from adapters.youtube import comments, transcript


class _NoTranscriptFound(Exception):
    pass


class _TranscriptsDisabled(Exception):
    pass


def _transcript_module(*, failure: BaseException | None = None, data=None) -> ModuleType:
    module = ModuleType("youtube_transcript_api")

    class _Api:
        @classmethod
        def get_transcript(cls, video_id, *, languages):
            if failure is not None:
                raise failure
            return data if data is not None else [{"text": "ok"}]

    module.YouTubeTranscriptApi = _Api
    module.NoTranscriptFound = _NoTranscriptFound
    module.TranscriptsDisabled = _TranscriptsDisabled
    return module


def _comments_module(*, failure: BaseException | None = None) -> ModuleType:
    module = ModuleType("youtube_comment_downloader")

    class _Downloader:
        def get_comments(self, video_id, *, sort_by):
            if failure is not None:
                raise failure
            yield {"author": "a", "text": "ok"}

    module.YoutubeCommentDownloader = _Downloader
    module.SORT_BY_POPULAR = "popular"
    return module


def _raise_optional_import(monkeypatch, module_name: str) -> None:
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == module_name:
            raise ImportError(f"missing {module_name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)


def test_r38b_fetch_transcript_import_error_is_fail_open(monkeypatch):
    _raise_optional_import(monkeypatch, "youtube_transcript_api")

    assert transcript.fetch_transcript("abcdefghijk") == (None, "fetch_failed")


@pytest.mark.parametrize(
    ("failure", "expected_label"),
    [
        (_NoTranscriptFound("none"), "unavailable"),
        (_TranscriptsDisabled("disabled"), "unavailable"),
        (RuntimeError("network"), "fetch_failed"),
    ],
)
def test_r38b_fetch_transcript_query_errors_are_fail_open(
    monkeypatch, failure, expected_label,
):
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", _transcript_module(failure=failure))

    assert transcript.fetch_transcript("abcdefghijk") == (None, expected_label)


def test_r38b_fetch_comments_import_error_is_fail_open(monkeypatch):
    _raise_optional_import(monkeypatch, "youtube_comment_downloader")

    assert comments.fetch_comments("abcdefghijk") == ([], "fetch_failed")


def test_r38b_fetch_comments_internal_error_is_fail_open(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "youtube_comment_downloader",
        _comments_module(failure=RuntimeError("blocked")),
    )

    assert comments.fetch_comments("abcdefghijk") == ([], "fetch_failed")


def test_r38b_youtube_fetch_completes_when_transcript_cleanup_raises(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "youtube_transcript_api",
        _transcript_module(data=[{"text": 7}]),
    )
    monkeypatch.setattr(
        youtube.scrape,
        "probe",
        lambda url: {"id": "abcdefghijk", "description": "kept body"},
    )

    out = youtube.fetch(
        "https://youtube.com/watch?v=abcdefghijk",
        with_video=False,
        with_subs=False,
        with_transcript=True,
    )

    assert out["body_text"] == "kept body"
    assert out["transcript"] is None
    assert out["meta"]["transcript_label"] == "fetch_failed"


def test_r38b_youtube_fetch_completes_when_comments_call_raises(monkeypatch):
    monkeypatch.setattr(
        youtube._comments,
        "fetch_comments",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("module-level boom")),
    )
    monkeypatch.setattr(
        youtube.scrape,
        "probe",
        lambda url: {"id": "abcdefghijk", "description": "kept body"},
    )

    out = youtube.fetch(
        "https://youtube.com/watch?v=abcdefghijk",
        with_video=False,
        with_subs=False,
        with_transcript=False,
        with_comments=True,
    )

    assert out["body_text"] == "kept body"
    assert out["comments"] == []
    assert out["meta"]["comments_label"] == "fetch_failed"


def test_r47_media_dir_mkdir_oserror_keeps_metadata_and_labels_failure(tmp_path, monkeypatch):
    # R47(R38-D): 미디어 폴더 자리에 같은 이름의 일반 파일 → mkdir가 OSError(FileExistsError).
    # 이전엔 그 예외가 fetch 밖으로 나가 probe로 받은 제목·본문·챕터가 통째로 사라졌다.
    blocker = tmp_path / "downloads"
    blocker.write_text("not a directory", encoding="utf-8")
    chapters = [{"title": "intro", "start_time": 0.0}]
    monkeypatch.setattr(
        youtube.scrape,
        "probe",
        lambda url: {"id": "abcdefghijk", "title": "kept title",
                     "description": "kept body", "chapters": chapters},
    )
    monkeypatch.setattr(
        youtube.scrape, "_run",
        lambda *a, **k: pytest.fail("폴더 생성 실패 뒤 yt-dlp를 부르면 안 된다"),
    )

    out = youtube.fetch(
        "https://youtube.com/watch?v=abcdefghijk",
        media_dir=blocker,
        with_chat=True,
    )

    assert out["body_text"] == "kept body"
    assert out["meta"]["title"] == "kept title"
    assert out["meta"]["chapters"] == chapters
    assert out["media_paths"] == []
    assert out["meta"]["video_label"] == "download_failed"
    assert out["meta"]["chat_label"] == "download_failed"
