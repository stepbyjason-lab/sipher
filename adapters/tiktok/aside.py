"""R52a: explicit, read-only TikTok collection through the user's Aside browser.

Operator preflight: at most two intentional requests/account/KST day, including
source checks, retries and other work. Unknown remaining budget means do not run.
There is deliberately no persistent counter, lock, cookie or login-file access.
DOM selectors and actual first-comment order still require chair live acceptance.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

TIMEOUT = 100
MAX_COMMENTS = 20
PREFIX = "SIPHER_TIKTOK_ASIDE:"
_POST_PATH = re.compile(r"/@[A-Za-z0-9_.-]+/(video|photo)/([0-9]+)\Z")
_ERRORS = {"auth_required", "captcha", "account_warning", "access_restricted",
           "not_found", "comments_unavailable", "parse_error", "redirect_rejected", "cleanup_failed"}
_SIGNAL_SOURCES = {
    "account_warning": {"system_ui", "checkpoint_redirect"},
    "auth_required": {"system_ui", "login_redirect", "login_ui"},
    "captcha": {"system_ui", "captcha_redirect", "captcha_iframe"},
    "access_restricted": {"system_ui"},
}


class AsideError(RuntimeError):
    """Stable error kind; raw browser/CLI content is never included in diagnostics."""

    def __init__(self, kind: str, *, cleanup_failed: bool = False):
        self.kind = kind
        stop = kind in {"auth_required", "captcha", "account_warning", "access_restricted"}
        message = "즉시 중단했습니다. 같은 계정의 확인·재시도를 멈추고 사용자에게 알리세요." if stop else "수집에 실패했습니다."
        if cleanup_failed or kind == "cleanup_failed":
            message += " 새 탭 정리를 확인하지 못했습니다. 사용자가 탭을 확인해야 합니다."
        super().__init__(f"TikTok aside [{kind}]: {message}")


def parse_url(url: str) -> str:
    """Only literal HTTPS post URLs; reject short links and encoded path aliases."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ValueError("aside는 HTTPS TikTok 단일 게시물 URL만 지원합니다")
    try:
        p = urlsplit(url)
        valid = (p.scheme == "https" and p.netloc.lower() in {"tiktok.com", "www.tiktok.com"}
                 and _POST_PATH.fullmatch(p.path) and not p.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("aside는 HTTPS TikTok 단일 게시물 URL만 지원합니다(단축 URL 제외)")
    return f"https://www.tiktok.com{p.path}"


def _executable() -> str:
    configured = os.environ.get("SIPHER_ASIDE_EXE")
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute() or not candidate.is_file():
            raise AsideError("installation_missing")
        return str(candidate)
    found = shutil.which("aside")
    if found:
        return found
    candidate = Path.home() / ".aside" / "cli" / "aside.exe"
    if candidate.is_file():
        return str(candidate)
    raise AsideError("installation_missing")


def _run(executable: str, script: str) -> subprocess.CompletedProcess:
    try:
        # stdin avoids shell parsing and Windows command-line length constraints.
        return subprocess.run(
            [executable, "repl", "--json"], input=script, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT, shell=False,
        )
    except FileNotFoundError as exc:
        raise AsideError("installation_missing") from exc
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills and waits for the child before raising.
        raise AsideError("timeout") from exc
    except OSError as exc:
        raise AsideError("execution_failed") from exc


def _payload(cp: subprocess.CompletedProcess) -> dict:
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        envelope = json.loads(cp.stdout, object_pairs_hook=unique_object)
        run = envelope["run"]
        if not isinstance(run, dict):
            raise ValueError
        texts = [item["text"] for item in run["content"] if item.get("type") == "text"]
        lines = [line for text in texts for line in text.splitlines() if line.startswith(PREFIX)]
        if len(lines) != 1:
            raise ValueError
        payload = json.loads(lines[0][len(PREFIX):], object_pairs_hook=unique_object)
        if not isinstance(payload, dict):
            raise ValueError
        kind = payload.get("status")
        if not isinstance(kind, str) or kind not in _ERRORS | {"ok"}:
            raise ValueError
        source = payload.get("signal_source")
        if kind in _SIGNAL_SOURCES:
            if not isinstance(source, str) or source not in _SIGNAL_SOURCES[kind]:
                raise ValueError
        elif "signal_source" in payload:
            raise ValueError
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise AsideError("parse_error") from exc
    if kind in _SIGNAL_SOURCES:
        raise AsideError(kind, cleanup_failed=payload.get("cleanup_failed") is True)
    if run.get("error"):
        raise AsideError("browser_unavailable")
    if cp.returncode:
        raise AsideError("execution_failed")
    if kind != "ok":
        raise AsideError(kind,
                         cleanup_failed=payload.get("cleanup_failed") is True)
    return payload


def _normalize(data: dict, source: str) -> dict:
    try:
        if parse_url(data["url"]) != source:
            raise ValueError
        body = data["body_text"]
        comments = data["comments"]
        if not isinstance(body, str) or not isinstance(comments, list) or len(comments) > MAX_COMMENTS:
            raise ValueError
        if data["comments_state"] not in {"visible", "empty"}:
            raise ValueError
        if bool(comments) != (data["comments_state"] == "visible"):
            raise ValueError
        normalized = []
        for c in comments:
            if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
                raise ValueError
            for key in ("id", "author"):
                if c.get(key) is not None and not isinstance(c[key], str):
                    raise ValueError
            normalized.append({"id": c.get("id"), "author": c.get("author"),
                               "text": c["text"], "likes": None, "reply_count": None, "media_paths": []})
        author = data.get("author")
        if author is not None and not isinstance(author, str):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise AsideError("parse_error") from exc
    return {
        "source": source, "platform": "tiktok", "body_text": body,
        "comments": normalized, "ocr_text": [], "transcript": None, "media_paths": [],
        "meta": {
            "collector": "aside", "video_id": source.rsplit("/", 1)[1], "author": author,
            "created_at_utc": None, "fetched_at": datetime.now(timezone.utc).isoformat(),
            "comments_label": "fetched" if comments else "none",
            "comments_order": "display", "comments_limit": MAX_COMMENTS,
            "completeness": {"incomplete": bool(comments), "captured": len(comments), "expected": None},
            "media_label": "none", "ocr_label": "not_downloaded", "transcript_label": "not_downloaded",
        },
    }


def fetch(url: str, *, download: bool = False, comments: bool | None = None) -> dict:
    """Read one post. A notice precedes the single browser invocation; never retry."""
    source = parse_url(url)
    if download or comments is False:
        raise ValueError("aside는 미디어 다운로드·댓글 제외 옵션을 지원하지 않습니다")
    executable = _executable()
    script = Path(__file__).with_name("aside_extract.js").read_text(encoding="utf-8")
    script = script.replace("__SIPHER_URL_JSON__", json.dumps(source))
    print("TikTok aside: 새 탭 1개를 열어 읽습니다. 계정당 KST 하루 2회 잔여는 운영자가 "
          "먼저 확인해야 하며 불명확하면 실행을 취소하세요. 첫 계정 경고에서 즉시 중단합니다.",
          file=sys.stderr, flush=True)
    return _normalize(_payload(_run(executable, script)), source)
