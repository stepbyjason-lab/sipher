"""round-33: threads 미디어 다운로드 — 재fetch 실패 시 기존 캐시 파일 무손상(어댑터 레벨).

core/tests/test_media_io.py가 core.media_io 레벨 계약을 이미 검증한다. 이 테스트는
threads의 _download_one/download_media가 그 유틸을 실제로 통해 호출되는지, 그리고
실패 시 media_utils 호출부가 죽은 경로를 회수하지 않는지를 확인한다.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import urllib.request  # noqa: E402

from adapters.threads import media_utils  # noqa: E402


class _FailResp:
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


def test_threads_refetch_failure_preserves_existing_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=60: _FailResp())

    out_dir = tmp_path / "downloads"
    folder = out_dir / "alice_ABC123"
    folder.mkdir(parents=True)
    dest = folder / "img_01.jpg"
    dest.write_bytes(b"OLD-CACHED-MEDIA")

    posts = [{
        "code": "ABC123",
        "author": "alice",
        "images": ["https://cdn.example/a.jpg"],
        "videos": [],
    }]

    total = media_utils.download_media(posts, out_dir=str(out_dir))

    assert total == 0
    assert posts[0]["downloaded"] == []  # 실패 경로는 회수 안 됨
    assert dest.read_bytes() == b"OLD-CACHED-MEDIA"  # 기존 캐시 무손상
    # round-33 iter2: tmp 이름이 시도별 고유(mkstemp)라 고정 경로가 아니라 글롭으로 확인.
    assert list(folder.glob("img_01.*.jpg.tmp")) == []


def test_threads_download_one_success(tmp_path, monkeypatch):
    class _OkResp:
        status = 200

        def __init__(self):
            self._served = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _n=-1):
            if self._served:
                return b""
            self._served = True
            return b"media-bytes"

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=60: _OkResp())
    dest = tmp_path / "img_01.jpg"

    ok = media_utils._download_one("https://cdn.example/a.jpg", str(dest))

    assert ok is True
    assert dest.read_bytes() == b"media-bytes"
