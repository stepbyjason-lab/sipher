"""round-38C: Threads /share/ 단축링크 해석·SSRF 경계 단위 테스트.

HTTP는 requests monkeypatch로만 흉내 낸다. 실제 리다이렉트·Playwright·네트워크를
사용하지 않아 _resolve_share_url()의 호스트 검증 경계를 독립적으로 검증한다.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters import threads  # noqa: E402


class _Response:
    def __init__(self, status_code: int, location: str | None = None):
        self.status_code = status_code
        self.headers = {} if location is None else {"Location": location}


def _patch_requests(monkeypatch, get):
    """어댑터의 requests 경계를 가짜 GET과 예외 타입으로 고정한다."""
    monkeypatch.setattr(
        threads,
        "requests",
        SimpleNamespace(get=get, RequestException=requests.RequestException),
        raising=False,
    )


def test_share_redirect_resolves_and_fetch_uses_canonical_author_code(monkeypatch):
    """수락 바 1: share 302를 정식 URL로 풀고 fetch()가 author/code를 사용한다."""
    share_url = "https://www.threads.com/share/HJnDlImNF/"
    final_url = "https://www.threads.com/@conanssam/post/Dbno1iNk6un?xmt=signed"
    calls = []

    def fake_get(url, *, allow_redirects, timeout):
        calls.append((url, allow_redirects, timeout))
        return _Response(302, final_url)

    def fake_run(url, *, deep, auto, max_pages, author, code, progress=None):
        assert url == "https://www.threads.net/@conanssam/post/Dbno1iNk6un"
        assert (author, code) == ("conanssam", "Dbno1iNk6un")
        return ([{
            "id": "root", "code": code, "author": author, "text": "post",
            "likes": 0, "reply_count": 0, "images": [], "videos": [],
        }], False)

    _patch_requests(monkeypatch, fake_get)
    monkeypatch.setattr(threads, "_run_scrape", fake_run)
    monkeypatch.setattr(threads._dispatcher, "assess", lambda posts, url: {"root_found": True})

    assert threads._resolve_share_url(share_url) == final_url
    result = threads.fetch(share_url)

    assert result["meta"]["author"] == "conanssam"
    assert result["meta"]["code"] == "Dbno1iNk6un"
    assert calls == [(share_url, False, 30), (share_url, False, 30)]


def test_schemeless_share_url_uses_https_for_resolution(monkeypatch):
    """수락 바 1 강화: 기존 parse_url처럼 schema-less Threads URL도 지원한다."""
    share_url = "threads.net/share/HJnDlImNF/"
    final_url = "https://www.threads.net/@conanssam/post/Dbno1iNk6un"
    calls = []

    def fake_get(url, *, allow_redirects, timeout):
        calls.append(url)
        return _Response(302, final_url)

    _patch_requests(monkeypatch, fake_get)

    assert threads._resolve_share_url(share_url) == final_url
    assert calls == ["https://threads.net/share/HJnDlImNF/"]


def test_share_redirect_to_off_host_is_rejected_without_off_host_request(monkeypatch):
    """수락 바 2: off-host Location은 body 요청 전 ValueError로 거부한다."""
    share_url = "https://www.threads.net/share/safe_code/"
    calls = []

    def fake_get(url, *, allow_redirects, timeout):
        calls.append(url)
        if "evil.example" in url:
            raise AssertionError("off-host body request must never occur")
        return _Response(302, "http://evil.example/private")

    _patch_requests(monkeypatch, fake_get)

    with pytest.raises(ValueError, match="Threads URL"):
        threads._resolve_share_url(share_url)

    assert calls == [share_url]


def test_non_threads_share_is_rejected_before_network_request(monkeypatch):
    """수락 바 3: 비-threads share URL은 네트워크에 닿기 전에 거부한다."""
    def fake_get(*args, **kwargs):
        raise AssertionError("non-threads URL must not make a network request")

    _patch_requests(monkeypatch, fake_get)

    with pytest.raises(ValueError, match="Threads URL"):
        threads._resolve_share_url("https://evil.example/share/safe_code/")


def test_regular_post_url_is_network_free_noop(monkeypatch):
    """수락 바 4: 기존 정식 post URL은 해석 없이 그대로 통과한다."""
    url = "https://www.threads.com/@alice/post/ABC_123?xmt=signed"

    def fake_get(*args, **kwargs):
        raise AssertionError("regular post URL must not make a network request")

    _patch_requests(monkeypatch, fake_get)

    assert threads._resolve_share_url(url) == url


def test_share_redirect_loop_is_capped_at_three_hops(monkeypatch):
    """수락 바 5: /share/ 재귀 리다이렉트는 N=3에서 멈춰 ValueError가 난다."""
    share_url = "https://www.threads.com/share/loop/"
    calls = []

    def fake_get(url, *, allow_redirects, timeout):
        calls.append(url)
        return _Response(302, "/share/loop/")

    _patch_requests(monkeypatch, fake_get)

    with pytest.raises(ValueError, match="리다이렉트"):
        threads._resolve_share_url(share_url)

    assert calls == [share_url, share_url, share_url]


def test_share_network_exception_is_normalized_to_value_error(monkeypatch):
    """수락 바 6: timeout 같은 requests 예외는 raw 형태로 새지 않는다."""
    def fake_get(*args, **kwargs):
        raise requests.Timeout("simulated timeout")

    _patch_requests(monkeypatch, fake_get)

    with pytest.raises(ValueError, match="해석") as exc_info:
        threads._resolve_share_url("https://www.threads.com/share/safe_code/")

    assert not isinstance(exc_info.value, requests.Timeout)


def test_oversized_share_url_is_rejected_before_network_request(monkeypatch):
    """보안 회귀: parse_url의 기존 2048자 한계를 HTTP보다 먼저 적용한다."""
    def fake_get(*args, **kwargs):
        raise AssertionError("oversized share URL must not make a network request")

    _patch_requests(monkeypatch, fake_get)
    oversized = "https://www.threads.com/share/safe_code/?" + "x" * 2048

    with pytest.raises(ValueError, match="너무 깁니다"):
        threads._resolve_share_url(oversized)
