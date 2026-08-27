"""round-33: core.media_io 원자적 write/다운로드 유틸 단위 테스트.

재fetch 실패(네트워크 중단·상한 초과·조기 EOF)가 기존 캐시 파일을 훼손하지 않음을
검증한다. 네트워크는 쓰지 않는다 — urllib.request.urlopen을 fake context manager로
대체.

iter2(P0/P2 재게이트 수정) 이후 tmp 파일명이 `out.stem + "." + random + out.suffix +
".tmp"` 형태의 **시도별 고유 이름**(`tempfile.mkstemp`)으로 바뀌었으므로, "tmp가 안
남았다"는 고정 경로 존재 확인 대신 `_tmp_remnants(out)` 헬퍼(글롭)로 검사한다.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import re  # noqa: E402
import urllib.request  # noqa: E402
from pathlib import Path  # noqa: E402

from core import media_io  # noqa: E402


def _tmp_remnants(out: Path) -> list[Path]:
    """out과 같은 디렉터리에 남아있는 (out에서 파생된) tmp 파일 목록."""
    return sorted(out.parent.glob(out.stem + ".*" + out.suffix + ".tmp"))


class _FakeHeaders:
    """urllib http.client.HTTPMessage(get/get_all)를 흉내내는 최소 대역.

    `raw`: Content-Length 헤더의 원본 문자열 값(들). 문자열 하나면 단일 헤더 라인
    (콤마 다중값 `"10, 10"`도 이 형태로 준다), 리스트면 **서로 다른 헤더 라인**이
    여러 번 온 상황(get_all이 여러 항목을 돌려주는 경우)을 흉내낸다.
    """

    def __init__(self, raw: str | list[str] | None = None):
        self._raw = raw

    def get(self, key: str, default=None):
        if key != "Content-Length" or self._raw is None:
            return default
        if isinstance(self._raw, list):
            return self._raw[0] if self._raw else default
        return self._raw

    def get_all(self, key: str, default=None):
        if key != "Content-Length" or self._raw is None:
            return default
        return self._raw if isinstance(self._raw, list) else [self._raw]


class _FakeResponse:
    """urlopen()이 반환하는 context manager를 흉내낸다.

    `chunks`: read()가 순서대로 내놓을 bytes 목록. `fail_after`가 주어지면
    그 인덱스만큼 청크를 내놓은 뒤 OSError를 던져(네트워크 중단 시뮬레이션).
    `content_length`(int)가 주어지면 정상적인 단일 정수 Content-Length 헤더를
    흉내낸다. `raw_content_length`(str|list[str])는 비정상/다중값 등 원본 문자열을
    그대로 지정할 때 쓴다. 둘 다 None이면 헤더 자체가 없는 close-delimited
    응답(`.headers` 자체를 안 둠).
    """

    def __init__(self, chunks: list[bytes], status: int = 200,
                 fail_after: int | None = None, content_length: int | None = None,
                 raw_content_length: str | list[str] | None = None,
                 with_headers: bool = False):
        self._chunks = list(chunks)
        self.status = status
        self._fail_after = fail_after
        self._n_read = 0
        if content_length is not None:
            raw_content_length = str(content_length)
        if with_headers or raw_content_length is not None:
            self.headers = _FakeHeaders(raw_content_length)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _n: int = -1) -> bytes:
        if self._fail_after is not None and self._n_read >= self._fail_after:
            raise OSError("simulated network drop")
        if not self._chunks:
            return b""
        self._n_read += 1
        return self._chunks.pop(0)


def _patch_urlopen(monkeypatch, response: _FakeResponse):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=60: response)


# ---------------------------------------------------------------------------
# atomic_write
# ---------------------------------------------------------------------------


def test_atomic_write_success(tmp_path):
    out = tmp_path / "file.bin"
    assert media_io.atomic_write(out, b"hello") is True
    assert out.read_bytes() == b"hello"
    assert _tmp_remnants(out) == []


def test_atomic_write_failure_preserves_existing_cache(tmp_path, monkeypatch):
    out = tmp_path / "file.bin"
    out.write_bytes(b"OLD")

    def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    assert media_io.atomic_write(out, b"NEW") is False
    assert out.read_bytes() == b"OLD"
    assert _tmp_remnants(out) == []


# ---------------------------------------------------------------------------
# download_to_file
# ---------------------------------------------------------------------------


def test_download_to_file_success(tmp_path, monkeypatch):
    out = tmp_path / "media.bin"
    resp = _FakeResponse([b"abc", b"def", b""])
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is True
    assert out.read_bytes() == b"abcdef"
    assert _tmp_remnants(out) == []


def test_download_to_file_network_failure_preserves_existing_cache(tmp_path, monkeypatch):
    out = tmp_path / "media.bin"
    out.write_bytes(b"OLD-CACHE-CONTENT")

    # 첫 청크는 성공, 다음 read()에서 네트워크가 죽는다.
    resp = _FakeResponse([b"partial-bytes"], fail_after=1)
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert out.read_bytes() == b"OLD-CACHE-CONTENT"  # 무손상
    assert _tmp_remnants(out) == []  # 부분 tmp 정리됨


def test_download_to_file_over_max_bytes_preserves_existing_cache(tmp_path, monkeypatch):
    out = tmp_path / "media.bin"
    out.write_bytes(b"OLD")

    resp = _FakeResponse([b"1234567890", b"1234567890", b""])  # 20 bytes total
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=5
    )
    assert ok is False
    assert out.read_bytes() == b"OLD"
    assert _tmp_remnants(out) == []


def test_download_to_file_no_prior_cache_stays_absent_on_failure(tmp_path, monkeypatch):
    out = tmp_path / "media.bin"
    resp = _FakeResponse([b"partial"], fail_after=1)
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert not out.exists()
    assert _tmp_remnants(out) == []


def test_download_to_file_http_error_status_not_200(tmp_path, monkeypatch):
    out = tmp_path / "media.bin"
    resp = _FakeResponse([b"whatever"], status=404)
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert not out.exists()


def test_download_to_file_host_pattern_rejects(tmp_path, monkeypatch):
    out = tmp_path / "media.bin"
    called = {"n": 0}

    def _urlopen(*a, **k):
        called["n"] += 1
        raise AssertionError("urlopen should not be called for a blocked host")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    host_pattern = re.compile(r"^https?://(?:[\w-]+\.)*allowed\.example/", re.I)
    ok = media_io.download_to_file(
        "https://evil.example/f.bin",
        out,
        headers={"User-Agent": "x"},
        max_bytes=1024,
        host_pattern=host_pattern,
    )
    assert ok is False
    assert called["n"] == 0
    assert not out.exists()


def test_download_to_file_host_pattern_allows(tmp_path, monkeypatch):
    out = tmp_path / "media.bin"
    resp = _FakeResponse([b"ok", b""])
    _patch_urlopen(monkeypatch, resp)

    host_pattern = re.compile(r"^https?://(?:[\w-]+\.)*allowed\.example/", re.I)
    ok = media_io.download_to_file(
        "https://allowed.example/f.bin",
        out,
        headers={"User-Agent": "x"},
        max_bytes=1024,
        host_pattern=host_pattern,
    )
    assert ok is True
    assert out.read_bytes() == b"ok"


# ---------------------------------------------------------------------------
# round-33 iter2 P0 — Content-Length 조기 EOF 방어
# ---------------------------------------------------------------------------


def test_download_to_file_early_eof_no_exception_preserves_cache(tmp_path, monkeypatch):
    """Content-Length:10 선언했는데 본문이 3바이트만 오고 예외 없이 close(조기 EOF).

    resp.read(n)은 실제 urllib에서도 이 경우 예외를 안 던지고 짧은 청크 뒤 b""를
    반환한다 — Content-Length 검증이 없으면 "성공"으로 부분본이 out에 커밋된다.
    """
    out = tmp_path / "media.bin"
    out.write_bytes(b"OLD-COMPLETE-CACHE")

    resp = _FakeResponse([b"abc", b""], content_length=10)  # 선언 10B, 실제 3B
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert out.read_bytes() == b"OLD-COMPLETE-CACHE"  # 무손상 — 잘린 본문으로 교체 안 됨
    assert _tmp_remnants(out) == []


def test_download_to_file_declared_length_over_max_bytes_rejected_before_read(
    tmp_path, monkeypatch
):
    """선언 길이가 max_bytes를 넘으면 한 바이트도 안 읽고 거부한다."""
    out = tmp_path / "media.bin"
    out.write_bytes(b"OLD")

    read_calls = {"n": 0}

    class _CountingResp(_FakeResponse):
        def read(self, _n: int = -1) -> bytes:
            read_calls["n"] += 1
            return super().read(_n)

    resp = _CountingResp([b"x" * 100], content_length=100)
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=10
    )
    assert ok is False
    assert read_calls["n"] == 0  # 읽기 전에 거부
    assert out.read_bytes() == b"OLD"
    assert _tmp_remnants(out) == []


def test_download_to_file_declared_length_matches_commits_success(tmp_path, monkeypatch):
    """Content-Length와 실제 수신 바이트 수가 정확히 일치하면 정상 커밋."""
    out = tmp_path / "media.bin"
    body = b"exact-six"
    resp = _FakeResponse([body, b""], content_length=len(body))
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is True
    assert out.read_bytes() == body
    assert _tmp_remnants(out) == []


def test_download_to_file_no_content_length_still_succeeds(tmp_path, monkeypatch):
    """Content-Length 자체가 없는(close-delimited) 응답은 검증 불가 — 기존 동작대로
    스트림이 예외 없이 끝까지 오면 성공으로 커밋한다(명시된 한계)."""
    out = tmp_path / "media.bin"
    resp = _FakeResponse([b"no-length-header", b""])  # content_length=None
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is True
    assert out.read_bytes() == b"no-length-header"


# ---------------------------------------------------------------------------
# round-33 iter2 P2 — 동시 write 시 tmp 이름 충돌 없음
# ---------------------------------------------------------------------------


def test_download_to_file_concurrent_attempts_use_distinct_tmp_names(tmp_path, monkeypatch):
    """같은 out을 향한 두 동시 시도가 서로 다른 tmp 파일명을 쓴다(고정 `.tmp`였다면
    충돌·교차 삭제 위험이 있었다) — 하나가 실패해도 다른 하나의 진행 중 tmp는 안 건드림.
    """
    out = tmp_path / "media.bin"

    # 시도 1: 성공 경로에서 쓸 tmp를 만들어 "진행 중"인 상태를 흉내낸다.
    tmp1 = media_io._make_tmp_path(out)
    tmp1.write_bytes(b"attempt-1-in-progress")

    # 시도 2: 실패(네트워크 중단) — 자기 tmp만 지워야 하고 tmp1은 살아있어야 한다.
    resp = _FakeResponse([b"partial"], fail_after=1)
    _patch_urlopen(monkeypatch, resp)
    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert tmp1.exists()  # 시도 1의 진행 중 tmp는 무사
    assert tmp1.read_bytes() == b"attempt-1-in-progress"

    tmp1.unlink()  # 정리


# ---------------------------------------------------------------------------
# round-33 iter3 P0 — Content-Length 3-state(absent/valid/invalid) 재게이트
# ---------------------------------------------------------------------------


def test_download_to_file_content_length_duplicate_same_value_insufficient_bytes(
    tmp_path, monkeypatch
):
    """`"10, 10"`(동일값 다중) → valid로 인정(10) — 실제로는 3바이트만 와서 조기 EOF로 거부."""
    out = tmp_path / "media.bin"
    out.write_bytes(b"OLD-CACHE")

    resp = _FakeResponse([b"abc", b""], raw_content_length="10, 10")
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert out.read_bytes() == b"OLD-CACHE"
    assert _tmp_remnants(out) == []


def test_download_to_file_content_length_duplicate_different_values_rejected(
    tmp_path, monkeypatch
):
    """`"10, 20"`(서로 다른 다중값) → invalid — 읽기 전에 거부."""
    out = tmp_path / "media.bin"
    out.write_bytes(b"OLD-CACHE")

    read_calls = {"n": 0}

    class _CountingResp(_FakeResponse):
        def read(self, _n: int = -1) -> bytes:
            read_calls["n"] += 1
            return super().read(_n)

    resp = _CountingResp([b"x" * 10], raw_content_length="10, 20")
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert read_calls["n"] == 0
    assert out.read_bytes() == b"OLD-CACHE"
    assert _tmp_remnants(out) == []


@pytest.mark.parametrize("raw", ["-1", "n/a", "", "1.5", "+10", "１０"])
def test_download_to_file_content_length_invalid_values_rejected(tmp_path, monkeypatch, raw):
    """음수·비정수·빈 문자열 Content-Length → invalid — 읽기 전에 거부, 캐시 무손상."""
    out = tmp_path / "media.bin"
    out.write_bytes(b"OLD-CACHE")

    resp = _FakeResponse([b"whatever"], raw_content_length=raw)
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert out.read_bytes() == b"OLD-CACHE"
    assert _tmp_remnants(out) == []


def test_download_to_file_content_length_absent_still_lenient_commit(tmp_path, monkeypatch):
    """헤더 자체가 완전히 없으면(absent) 관대 커밋 유지 — invalid와 구분되어야 한다."""
    out = tmp_path / "media.bin"
    resp = _FakeResponse([b"body", b""])  # headers 속성 자체가 없음
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is True
    assert out.read_bytes() == b"body"


class _EmptyListHeaders:
    """get_all()이 빈 리스트를 돌려주는 이상 응답 대역(iter3 P2-2 — 실 urllib
    HTTPMessage는 부재 시 None을 주지만, 계약 정합성을 위해 빈 리스트도 방어)."""

    def get(self, key: str, default=None):
        return default

    def get_all(self, key: str, default=None):
        return []


def test_content_length_state_classification_direct():
    """_content_length_state 3-state 분류를 직접 검증(경계 케이스 표)."""
    assert media_io._content_length_state(None) == ("absent", None)
    assert media_io._content_length_state(_FakeHeaders(None)) == ("absent", None)
    assert media_io._content_length_state(_FakeHeaders("42")) == ("valid", 42)
    assert media_io._content_length_state(_FakeHeaders("10, 10")) == ("valid", 10)
    assert media_io._content_length_state(_FakeHeaders("10, 20")) == ("invalid", None)
    assert media_io._content_length_state(_FakeHeaders("-1")) == ("invalid", None)
    assert media_io._content_length_state(_FakeHeaders("n/a")) == ("invalid", None)
    assert media_io._content_length_state(_FakeHeaders("")) == ("invalid", None)
    assert media_io._content_length_state(_FakeHeaders(["10", "20"])) == ("invalid", None)
    assert media_io._content_length_state(_FakeHeaders(["10", "10"])) == ("valid", 10)
    # iter3 P2-1: 부호 붙은/유니코드 숫자는 ASCII 십진 숫자가 아니므로 invalid.
    assert media_io._content_length_state(_FakeHeaders("+10")) == ("invalid", None)
    assert media_io._content_length_state(_FakeHeaders("１０")) == ("invalid", None)
    # iter3 P2-2: get_all()이 빈 리스트를 주면 "헤더 없음(absent)"이 아니라
    # "헤더는 있는데 값이 없는 이상 상태(invalid)"로 분류해야 한다.
    assert media_io._content_length_state(_EmptyListHeaders()) == ("invalid", None)


# ---------------------------------------------------------------------------
# round-33 iter3 P1 — tmp 생성 자체 실패가 예외로 전파되지 않고 False로 수렴
# ---------------------------------------------------------------------------


def test_atomic_write_tmp_creation_failure_returns_false_no_exception(tmp_path):
    """부모 디렉터리가 없으면 mkstemp가 FileNotFoundError를 던진다 — 이게 호출자까지
    전파되면 계약(False 반환) 위반. 예외 없이 False로 수렴해야 한다."""
    out = tmp_path / "missing_parent" / "file.bin"  # 부모 디렉터리 존재하지 않음
    assert media_io.atomic_write(out, b"data") is False
    assert not out.exists()
    assert not out.parent.exists()


def test_download_to_file_tmp_creation_failure_returns_false_no_exception(
    tmp_path, monkeypatch
):
    """다운로드 스트림 자체는 정상 진행됐지만 tmp 생성 시점에 부모 디렉터리가
    없으면(경쟁 조건 등) 예외 없이 False로 수렴해야 한다."""
    out = tmp_path / "missing_parent" / "file.bin"
    resp = _FakeResponse([b"data", b""])
    _patch_urlopen(monkeypatch, resp)

    ok = media_io.download_to_file(
        "https://example.com/f.bin", out, headers={"User-Agent": "x"}, max_bytes=1024
    )
    assert ok is False
    assert not out.exists()
