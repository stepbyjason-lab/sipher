"""round-B: Instagram 첫 댓글 모드(comments=True → 첫 1개만 수집) 단위 테스트.

계약: .handoff/rounds/round-B-first-comment-mode-contract.md §Instagram 테스트.
instaloader network 호출 없이 mock/fake Post 객체만 사용한다(실계정·네트워크 금지).
실행: `pytest adapters/instagram/tests/` 또는 이 파일 직접.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# repo root를 path에 추가(어디서 실행하든 adapters.instagram import 가능) — facebook
# tests/test_comments_label.py와 동일 관례.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters.instagram import (  # noqa: E402
    _collect_first_instagram_comment,
    _resolve_instagram_comments,
    normalize,
)


class _PoisonInstaloaderModule:
    """instaloader_module 인자 대역 — comments=False 경로에서는 절대 속성 접근이
    일어나면 안 된다(early return이 먼저 실행돼야 함). 속성 접근 자체가 실패하도록
    만들어 회귀(early return 가드가 나중에 실수로 제거되는 경우)까지 잡는다."""

    def __getattr__(self, name):
        raise AssertionError(
            f"instaloader_module.{name} should not be accessed when comments=False "
            "(early-return guard should fire first)")


class _FakeOwner:
    def __init__(self, username: str):
        self.username = username


class _FakeComment:
    """instaloader PostComment 흉내 — 실제 필드만 최소 구현."""

    def __init__(self, comment_id: int, text: str, *, username: str = "alice",
                likes_count: int = 0, created_at_utc: datetime | None = None):
        self.id = comment_id
        self.owner = _FakeOwner(username)
        self.text = text
        self.likes_count = likes_count
        self.created_at_utc = created_at_utc or datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakePost:
    """instaloader Post 흉내 — normalize()/_collect_first_instagram_comment()가
    실제로 접근하는 속성만 채운다."""

    def __init__(self, *, comments_iter_factory, comment_count: int = 1):
        self._comments_iter_factory = comments_iter_factory
        self.caption = "테스트 캡션"
        self.owner_username = "alice"
        self.mediaid = 12345
        self.likes = 10
        self.comments = comment_count  # SNS가 노출하는 "전체 댓글 수"(그대로 유지되어야 함)
        self.is_video = False
        self.date_utc = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def get_comments(self):
        return self._comments_iter_factory()


# ── 1~5: comments=True 경로 — 첫 1개만 소비 ──


def test_only_first_comment_consumed_from_lazy_iterator():
    """(계약 #1,#2) 두 번째 댓글을 생성하려 하면 실패하는 iterator로 검증."""
    first = _FakeComment(1, "첫 댓글")

    def comments_iter():
        yield first
        raise AssertionError("second comment should not be consumed")

    post = _FakePost(comments_iter_factory=comments_iter, comment_count=5)
    result = _collect_first_instagram_comment(post)

    assert len(result) == 1  # (계약 #3) 반환된 comments 길이는 1
    assert result[0]["id"] == 1
    assert result[0]["author"] == "alice"
    assert result[0]["text"] == "첫 댓글"


def test_fetch_normalize_sets_comment_count_captured_and_mode():
    """(계약 #4,#5) meta.comment_count_captured==1, meta.comment_collection_mode=="first_only"."""
    first = _FakeComment(1, "첫 댓글")

    def comments_iter():
        yield first
        raise AssertionError("second comment should not be consumed")

    post = _FakePost(comments_iter_factory=comments_iter, comment_count=5)
    collected = _collect_first_instagram_comment(post)

    out = normalize(
        post,
        source="https://www.instagram.com/p/ABC123/",
        code="ABC123",
        comments=collected,
        comments_label="collected",
        comment_collection_mode="first_only",
        media_paths=[],
        downloaded=False,
        has_media=True,
        access_label="ok",
    )

    assert out["meta"]["comment_count_captured"] == 1
    assert out["meta"]["comment_collection_mode"] == "first_only"
    # 기존 comment_count(SNS 노출 전체 댓글 수)는 그대로 유지되어야 함(post.comments)
    assert out["meta"]["comment_count"] == 5


# ── 6: comments=False — get_comments() 자체가 호출되지 않아야 함 ──


def test_comments_false_never_calls_get_comments():
    """(계약 #6) comments=False일 때는 post.get_comments()가 호출되지 않고
    meta.comment_collection_mode == "not_requested".

    round-B Post-Review Fix(P2, Codex 지적): 이전 버전은 `_resolve_instagram_comments()`
    (당시엔 fetch() 인라인 로직)를 실제로 호출하지 않고 `collected_comments = []`를
    손으로 구성해 normalize()에 바로 넘겼다 — 이러면 normalize()가 입력을 그대로
    반영한다는 것만 증명될 뿐, "comments=False면 get_comments()가 실제로 호출되지
    않는다"는 코드 경로 자체는 전혀 실행되지 않아 poison iterator가 무의미했다.
    지금은 `_resolve_instagram_comments(post, comments=False, ...)`를 직접 호출해
    실제 분기 로직을 태운다 — poison iterator/poison instaloader_module이 실제로
    reachable한 경로에 배치된다(facebook test_maybe_fetch_comments_returns_none_when_comments_false
    와 동일한 검증 강도).
    """

    def comments_iter():
        raise AssertionError("get_comments() should not be called when comments=False")

    post = _FakePost(comments_iter_factory=comments_iter, comment_count=5)

    # _resolve_instagram_comments()를 실제로 호출 — comments=False면 이 함수 내부의
    # early return(`if not comments: return ...`)이 _collect_first_instagram_comment()
    # (→ post.get_comments() → poison iterator)에 도달하기 전에 먼저 실행되어야 한다.
    # instaloader_module도 poison으로 넘겨 early return 이후 어떤 속성도 건드리지
    # 않는다는 것까지 함께 검증한다.
    collected_comments, comments_label, comment_collection_mode = (
        _resolve_instagram_comments(post, False, _PoisonInstaloaderModule())
    )

    assert collected_comments == []
    assert comments_label == "not_requested"
    assert comment_collection_mode == "not_requested"

    out = normalize(
        post,
        source="https://www.instagram.com/p/ABC123/",
        code="ABC123",
        comments=collected_comments,
        comments_label=comments_label,
        comment_collection_mode=comment_collection_mode,
        media_paths=[],
        downloaded=False,
        has_media=True,
        access_label="ok",
    )

    assert out["meta"]["comment_collection_mode"] == "not_requested"
    assert out["meta"]["comment_count_captured"] == 0
    assert out["comments"] == []


def test_comments_true_calls_get_comments_via_resolve_helper():
    """대조군: comments=True로 같은 헬퍼를 호출하면 실제로 get_comments()를 태워
    첫 댓글을 수집한다 — 위 comments=False 테스트가 "그냥 항상 호출 안 하는
    코드"를 테스트하는 게 아니라 실제 분기를 구분한다는 것을 함께 증명한다."""
    first = _FakeComment(1, "첫 댓글")

    def comments_iter():
        yield first
        raise AssertionError("second comment should not be consumed")

    post = _FakePost(comments_iter_factory=comments_iter, comment_count=5)

    collected_comments, comments_label, comment_collection_mode = (
        _resolve_instagram_comments(post, True, _PoisonInstaloaderModule())
    )

    assert len(collected_comments) == 1
    assert collected_comments[0]["text"] == "첫 댓글"
    assert comments_label == "collected"
    assert comment_collection_mode == "first_only"


# ── 7: Amendment P2 — 댓글 0개 엣지케이스 ──


def test_zero_comments_empty_iterator_no_indexerror():
    """(Amendment P2) post.get_comments()가 빈 iterator(댓글 0개)를 반환할 때
    comments=True여도 예외 없이 comments==[], comment_count_captured==0,
    comment_collection_mode=="first_only"(시도는 했으나 결과가 0개)."""

    def empty_comments_iter():
        return iter(())  # 댓글 0개 — 빈 제너레이터/이터러블

    post = _FakePost(comments_iter_factory=empty_comments_iter, comment_count=0)

    # 예외 없이 빈 리스트를 반환해야 한다(IndexError 금지).
    collected = _collect_first_instagram_comment(post)
    assert collected == []

    out = normalize(
        post,
        source="https://www.instagram.com/p/ABC123/",
        code="ABC123",
        comments=collected,
        comments_label="collected",  # 정상 시도 + 결과 0개 — fetch_failed/login_required 아님
        comment_collection_mode="first_only",
        media_paths=[],
        downloaded=False,
        has_media=True,
        access_label="ok",
    )

    assert out["meta"]["comment_count_captured"] == 0
    assert out["meta"]["comment_collection_mode"] == "first_only"
    assert out["comments"] == []


if __name__ == "__main__":  # pytest 없이 직접 실행 가능(facebook test_comments_label.py 관례)
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

# ── round-34: Instagram carousel full media contract ──

import urllib.request
from pathlib import Path
import adapters.instagram as IG


class _R34Node:
    def __init__(self, url: str, *, is_video: bool = False):
        self.display_url = None if is_video else url
        self.video_url = url if is_video else None
        self.url = url
        self.is_video = is_video


class _R34Post:
    caption = "carousel"
    owner_username = "alice"
    mediaid = 123
    likes = 1
    comments = 0
    is_video = False
    date_utc = None
    typename = "GraphSidecar"  # round-34 재게이트: _sidecar_nodes가 typename으로 먼저 판정

    def __init__(self, nodes):
        self._nodes = nodes

    def get_sidecar_nodes(self):
        return iter(self._nodes)


class _R34Resp:
    status = 200

    def __init__(self):
        self._served = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _n=-1):
        # round-33: download_to_file()이 chunk 스트리밍(resp.read(n))으로 EOF까지
        # 읽는다 — 실 urllib HTTPResponse처럼 한 번 다 읽으면 그다음 read()는 b"".
        if self._served:
            return b""
        self._served = True
        return b"media"


def test_r34_instagram_downloads_all_sidecar_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=60: _R34Resp())
    out_dir = tmp_path / "ig"
    post = _R34Post([
        _R34Node("https://cdn.example/a.jpg"),
        _R34Node("https://cdn.example/b.webp"),
        _R34Node("https://cdn.example/c.mp4", is_video=True),
    ])

    paths = IG._download_media(post, out_dir=str(out_dir), code="ABC123")

    assert len(paths) == 3
    assert all(Path(p).is_file() for p in paths)


# ── round-33: 재fetch 실패 시 기존 캐시 파일 무손상(어댑터 레벨) ──


class _R33FailResp:
    """일부 바이트를 내놓은 뒤 read()에서 네트워크 중단을 시뮬레이션."""

    status = 200

    def __init__(self):
        self._served = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _n=-1):
        if not self._served:
            self._served = True
            return b"partial-bytes"
        raise OSError("simulated network drop")


def test_r33_instagram_refetch_failure_preserves_existing_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=60: _R33FailResp())
    out_dir = tmp_path / "ig"
    out_dir.mkdir()
    # 대표 단일 미디어(캐러셀 아님) 경로에 기존 캐시가 이미 존재하는 상황을 재현.
    dest = out_dir / "ig_ABC123.jpg"
    dest.write_bytes(b"OLD-CACHED-MEDIA")

    post = _R34Post([])  # get_sidecar_nodes() -> 빈 iter -> 단일 대표 미디어 경로
    post.is_video = False
    post.url = "https://cdn.example/a.jpg"

    paths = IG._download_media(post, out_dir=str(out_dir), code="ABC123")

    assert paths == []  # 실패했으니 회수 안 됨
    assert dest.read_bytes() == b"OLD-CACHED-MEDIA"  # 기존 캐시 무손상
    # round-33 iter2: tmp 이름이 시도별 고유(mkstemp)라 고정 경로가 아니라 글롭으로 확인.
    assert list(out_dir.glob("ig_ABC123.*.jpg.tmp")) == []


def test_r34_instagram_normalize_records_carousel_counts_and_partial_label():
    post = _R34Post([
        _R34Node("https://cdn.example/a.jpg"),
        _R34Node("https://cdn.example/b.jpg"),
        _R34Node("https://cdn.example/c.mp4", is_video=True),
    ])
    out = IG.normalize(
        post,
        source="https://www.instagram.com/p/ABC123/",
        code="ABC123",
        comments=[],
        comments_label="not_requested",
        comment_collection_mode="not_requested",
        media_paths=["one.jpg", "two.jpg"],
        downloaded=True,
        has_media=True,
        access_label="ok",
    )

    assert out["meta"]["image_count"] == 2
    assert out["meta"]["video_count"] == 1
    assert out["meta"]["media_label"] == "partially_downloaded"


class _R34BrokenSidecarPost(_R34Post):
    """캐러셀인 게 확실하지만(typename) 노드 열거가 실패하는 포스트(재게이트 P0 repro)."""

    def get_sidecar_nodes(self):
        raise RuntimeError("graphql fetch failed")


def test_r34_sidecar_enumeration_failure_is_not_reported_as_success(tmp_path):
    # 재게이트 P0: 열거 실패를 대표 1건 성공으로 위장하지 않는다 — 다운로드 시도 안 함
    # + image_count/video_count=None + media_label="download_failed"(정직 실패).
    post = _R34BrokenSidecarPost([])
    paths = IG._download_media(post, out_dir=str(tmp_path / "ig"), code="ABC123")
    assert paths == []

    out = IG.normalize(
        post,
        source="https://www.instagram.com/p/ABC123/",
        code="ABC123",
        comments=[],
        comments_label="not_requested",
        comment_collection_mode="not_requested",
        media_paths=paths,
        downloaded=True,
        has_media=True,
        access_label="ok",
    )
    assert out["meta"]["image_count"] is None
    assert out["meta"]["video_count"] is None
    assert out["meta"]["media_label"] == "download_failed"
