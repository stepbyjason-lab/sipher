r"""
sipher-x 어댑터 — X(구 트위터) 포스트 1건 → sipher 정규화 JSON. 독립 도구(패키지).

gallery-dl(pip, GPL-2.0)을 tiktok 어댑터와 같은 방식으로 subprocess 호출한다(벤더링
아님, 새 의존성 0). sipher 내부를 import 하지 않는 깨끗한 경계를 지킨다.

한 번의 `--dump-json` 호출(`text-tweets`·`conversations`·`replies`·`quoted`)로 원글 ·
저자 이어쓰기 · 인용 포스트 · 저자 대댓글 · 타인 답글이 함께 온다(R45 검체 실측). 받은
트윗을 자리별로 나눈다(R45 기획서 D-3):

- 원글 → `body_text`(원문 그대로).
- 저자가 원글(또는 자기 이어쓰기)에 단 답글 → `author_thread[]`(Threads 어댑터와 같은 필드).
- 원글이 인용한 포스트 → `quoted[]`(답글 목록이 아니다). 인용 아티클은 제목·표지·이미지까지.
- 저자가 다른 트윗에 단 답글 → `comments[]`(`kind="author_reply"`, 자르지 않는다).
- 타인 답글 → `comments[]`(`kind="reply"`, `max_replies` 상한).

X는 로그인 쿠키가 있어야 대화를 준다. 쿠키 파일(넷스케이프 형식) 경로는 `X_COOKIES_FILE`
(`.env.local` 또는 환경변수)로 받는다. 쿠키가 없거나 만료되면 gallery-dl이 `--dump-json`
출력에 `[-1, {"error": "AuthRequired"}]`를 싣고 rc=0으로 끝난다(2026-10-07 실측) —
이것을 빈 성공으로 내지 않고 `XAuthRequired` 오류로 올린다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["fetch", "parse_url", "normalize", "GalleryDlError", "XAuthRequired", "DEFAULT_MAX_REPLIES"]

_log = logging.getLogger(__name__)

DEFAULT_MAX_REPLIES = 100

# x.com / twitter.com(+www./mobile.)의 `/<계정>/status/<id>`만 허용(SSRF 방어). 꼬리
# 파라미터(`?s=46`)는 떼고 canonical URL을 다시 만든다 — 같은 포스트는 같은 결과(ⓐ).
_URL = re.compile(
    r"^(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/"
    r"(?P<user>[A-Za-z0-9_]{1,50}|i/web)/status/(?P<id>\d{1,25})(?:[/?#]|$)",
    re.I,
)

_GALLERY_DL: list[str] = [sys.executable, "-m", "gallery_dl"]
_OPTIONS: list[str] = [
    "-o", "text-tweets=true", "-o", "conversations=true",
    "-o", "replies=true", "-o", "quoted=true",
]
_DUMP_TIMEOUT = 180
_DOWNLOAD_TIMEOUT = 600
_ROOT = Path(__file__).resolve().parents[2]  # .env.local 위치(저장소 루트)
_AUTH_ERRORS = {"AuthRequired", "AuthorizationError"}
_VIDEO_TYPES = {"video", "animated_gif"}


class GalleryDlError(RuntimeError):
    """gallery-dl 부재·실행 실패·출력 파싱 실패·원글 없음."""


class XAuthRequired(GalleryDlError):
    """쿠키가 없거나 만료돼 X가 대화를 주지 않음(gallery-dl AuthRequired)."""


def parse_url(url: str) -> tuple[str, str]:
    """X 포스트 URL → (canonical URL, tweet id). 실패 시 ValueError."""
    if not isinstance(url, str):
        raise ValueError("URL은 문자열이어야 합니다")
    s = url.strip()
    if len(s) > 2048:
        raise ValueError("URL이 너무 깁니다")
    m = _URL.match(s)
    if not m:
        raise ValueError(f"X 포스트 URL이 아닙니다: {s.split('?', 1)[0]!r}")
    user, tweet_id = m.group("user"), m.group("id")
    return f"https://x.com/{user}/status/{tweet_id}", tweet_id


def _cookies_file() -> Path | None:
    """`X_COOKIES_FILE`(.env.local → 환경변수 순) → 쿠키 파일 경로. 미설정이면 None.

    설정했는데 파일이 없으면 gallery-dl까지 가지 않고 그 사실을 오류로 올린다 —
    그대로 넘기면 「쿠키 미설정」·「만료」와 구분되지 않는 AuthRequired가 된다.
    """
    value = None
    env_local = _ROOT / ".env.local"
    if env_local.is_file():
        for line in env_local.read_text(encoding="utf-8").splitlines():
            key, sep, raw = line.strip().partition("=")
            if sep and key.strip() == "X_COOKIES_FILE":
                value = raw.strip().strip('"').strip("'") or None
    value = value or os.environ.get("X_COOKIES_FILE")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = _ROOT / path
    if not path.is_file():
        raise XAuthRequired(f"AuthRequired: X_COOKIES_FILE이 가리키는 쿠키 파일이 없습니다: {path}")
    return path


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    # tiktok 어댑터와 같은 이유로 자식 stdout을 utf-8로 강제한다(cp949 경로 깨짐 방지).
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        return subprocess.run(
            _GALLERY_DL + args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, shell=False, env=env,
        )
    except FileNotFoundError as e:
        raise GalleryDlError(f"파이썬 실행 파일을 찾을 수 없음: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise GalleryDlError(f"gallery-dl 타임아웃({timeout}s)") from e


def _auth_error(message: str, cookies: Path | None) -> XAuthRequired:
    hint = ("X_COOKIES_FILE의 쿠키가 만료됐을 수 있습니다" if cookies
            else "X_COOKIES_FILE에 X 로그인 쿠키 파일(넷스케이프 형식) 경로를 설정하세요")
    return XAuthRequired(f"AuthRequired: {message} — {hint}")


def _raise_if_auth(stderr: str | None, cookies: Path | None) -> str:
    """gallery-dl stderr 꼬리에 인증 오류가 있으면 XAuthRequired, 없으면 꼬리를 돌려준다.

    dump·다운로드 두 호출이 같은 판정을 쓴다 — 다운로드 단계에서 쿠키가 만료돼도
    `download_failed` 라벨의 성공 결과가 아니라 오류로 올라가야 한다(R45 iter 1 F1).
    """
    tail = (stderr or "").strip()[-600:]
    if any(name in tail for name in _AUTH_ERRORS):
        raise _auth_error(tail, cookies)
    return tail


def _dump(canonical: str, cookies: Path | None) -> list:
    cookie_args = ["--cookies", str(cookies)] if cookies else []
    cp = _run(["--dump-json", *_OPTIONS, *cookie_args, "--", canonical], timeout=_DUMP_TIMEOUT)
    if cp.returncode != 0:
        tail = _raise_if_auth(cp.stderr, cookies)
        if "No module named" in tail and "gallery_dl" in tail:
            raise GalleryDlError("gallery-dl 미설치 — `pip install gallery-dl` 필요")
        raise GalleryDlError(f"gallery-dl 실패(rc={cp.returncode}): {tail}")
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise GalleryDlError(f"gallery-dl 메타 JSON 파싱 실패: {e}") from e
    if not isinstance(data, list):
        raise GalleryDlError("gallery-dl 메타 구조가 예상과 다름(list 아님)")
    for item in data:
        if isinstance(item, list) and item and item[0] == -1 and isinstance(item[-1], dict):
            if item[-1].get("error") in _AUTH_ERRORS:
                raise _auth_error(item[-1].get("message") or item[-1]["error"], cookies)
    return data


def _download(canonical: str, out_dir: str, cookies: Path | None, tweet_ids: list[int]) -> list[str]:
    """남길 트윗의 미디어만 원본으로 받고 `--write-metadata` 사이드카를 쓴다. 받은 경로 목록."""
    cookie_args = ["--cookies", str(cookies)] if cookies else []
    keep = "{" + ",".join(str(i) for i in sorted(set(tweet_ids))) + "}"
    cp = _run(
        ["-d", out_dir, "--write-metadata", *_OPTIONS, *cookie_args,
         "--filter", f"tweet_id in {keep}", "--", canonical],
        timeout=_DOWNLOAD_TIMEOUT,
    )
    if cp.returncode != 0:
        # 인증 오류는 오류로 올린다. 그 밖에 일부 파일만 실패한 경우는 받은 파일을 살리고 라벨로 드러낸다.
        tail = _raise_if_auth(cp.stderr, cookies)
        _log.warning("x: 다운로드 rc=%s: %s", cp.returncode, tail[-400:])
    paths: list[str] = []
    for line in (cp.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("# "):  # 이미 받은 파일(skip)도 파일 있음으로 친다(tiktok과 동일).
            s = s[2:].strip()
        if s and Path(s).is_file():
            paths.append(s)
    return paths


def _sidecar(path: str) -> dict:
    try:
        return json.loads(Path(path + ".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _probe_size(path: str) -> tuple[int, int] | None:
    """받은 파일의 실제 가로세로. 시스템 ffprobe(선택 의존, transcribe의 ffmpeg와 같은 패키지)
    가 없거나 읽지 못하면 None — 호출부가 사이드카 선언 크기로 채운다."""
    exe = shutil.which("ffprobe")
    if exe is None:
        return None
    try:
        cp = subprocess.run(
            [exe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        w, h = cp.stdout.strip().splitlines()[0].split(",")[:2]
        return int(w), int(h)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _media_file(path: str) -> dict:
    """`meta.media_files` 한 항목. `width`/`height`는 실제 파일 크기(못 재면 선언 크기),
    `declared_*`는 사이드카의 선언 크기 — X가 선언보다 작은 변형을 줄 수 있다(R45 iter 4)."""
    side = _sidecar(path)
    declared = (side.get("width"), side.get("height"))
    real = _probe_size(path)
    width, height = real or declared
    return {"path": path, "tweet_id": str(_tweet_id_of(path)), "width": width, "height": height,
            "declared_width": declared[0], "declared_height": declared[1],
            "size_source": "probe" if real else "sidecar"}


def _tweet_id_of(path: str) -> int | None:
    """받은 파일 → 트윗 id. 사이드카 메타가 정본, 없으면 기본 파일명 `{tweet_id}_{num}`."""
    tid = _sidecar(path).get("tweet_id")
    if tid is None:
        m = re.match(r"(\d+)_\d+\.", Path(path).name)
        tid = m.group(1) if m else None
    return int(tid) if tid is not None else None


def fetch(url: str, *, media_dir: str | Path | None = None, download: bool = False,
          max_replies: int = DEFAULT_MAX_REPLIES) -> dict:
    """X 포스트 URL → 정규화 JSON dict.

    - 항상 `--dump-json`으로 대화 전체 메타를 받는다(다운로드 없음).
    - download=True면 남길 트윗의 미디어만 `-d media_dir --write-metadata`로 받는다.
    - max_replies는 **타인 답글**에만 거는 상한이다. 저자 이어쓰기·저자 대댓글은 자르지 않는다.

    media_dir은 로컬 사용자가 지정하는 신뢰 입력이다(tiktok과 같은 경계).
    """
    if isinstance(max_replies, bool) or not isinstance(max_replies, int) or max_replies < 0:
        raise ValueError("max_replies는 0 이상의 정수여야 합니다")
    canonical, tweet_id = parse_url(url)
    cookies = _cookies_file()
    data = _dump(canonical, cookies)
    result = normalize(data, source=canonical, tweet_id=tweet_id, max_replies=max_replies)
    result["meta"]["cookies_used"] = cookies is not None
    if download:
        out_dir = str(media_dir) if media_dir else "downloads"
        paths = _download(canonical, out_dir, cookies, result["meta"]["kept_tweet_ids"])
        result = _attach_media(result, paths)
    return result


def _tweets(data: list) -> dict[int, dict]:
    """gallery-dl dump(`[[2, tweet], [3, url, file], ...]`) → {tweet_id: tweet + media[]}."""
    tweets: dict[int, dict] = {}
    for item in data:
        if not (isinstance(item, list) and len(item) >= 2 and isinstance(item[-1], dict)):
            continue
        payload = item[-1]
        if item[0] not in (2, 3) or payload.get("tweet_id") is None:
            continue
        tid = int(payload["tweet_id"])
        tweet = tweets.setdefault(tid, {**payload, "media": []})
        if item[0] == 3 and len(item) >= 3:
            tweet["media"].append({
                "url": item[1], "type": payload.get("type"),
                "width": payload.get("width"), "height": payload.get("height"),
                "extension": payload.get("extension"),
            })
    return tweets


def _name(tweet: dict) -> str | None:
    return (tweet.get("author") or {}).get("name")


def _item(tweet: dict, quotes: dict[int, list[dict]], kind: str | None = None) -> dict:
    item = {
        "id": str(tweet["tweet_id"]),
        "author": _name(tweet),
        "text": tweet.get("content") or "",
        "created_at": tweet.get("date"),
        "reply_to_id": str(tweet["reply_id"]) if tweet.get("reply_id") else None,
        "likes": tweet.get("favorite_count", 0),
        "reply_count": tweet.get("reply_count", 0),
        "media": [dict(m) for m in tweet["media"]],
        "media_paths": [],
        "quoted": [_item(q, quotes) for q in quotes.get(int(tweet["tweet_id"]), [])],
    }
    if tweet.get("article"):
        item["article"] = dict(tweet["article"])
    if kind:
        item["kind"] = kind
    return item


def normalize(data: list, *, source: str, tweet_id: str | int,
              max_replies: int = DEFAULT_MAX_REPLIES) -> dict:
    """gallery-dl `--dump-json` 출력 전체 → sipher 정규화 스키마. 공개 API(오프라인 테스트용)."""
    tweets = _tweets(data)
    root_id = int(tweet_id)
    root = tweets.get(root_id)
    if root is None:
        errors = [i[-1] for i in data if isinstance(i, list) and i and i[0] == -1]
        raise GalleryDlError(f"원글을 찾을 수 없음(비공개·삭제·차단일 수 있음): {errors or '빈 결과'}")
    root_author = _name(root)

    # 인용: quote_id = 인용한 트윗. 원글의 인용은 quoted[]로, 답글의 인용은 그 답글 안에 붙는다.
    quotes: dict[int, list[dict]] = {}
    for t in tweets.values():
        quoter = int(t.get("quote_id") or 0)
        if quoter in tweets and quoter != int(t["tweet_id"]):  # 인용한 쪽이 없으면 일반 답글로 센다
            quotes.setdefault(quoter, []).append(t)
    quoted_ids = {int(t["tweet_id"]) for qs in quotes.values() for t in qs}

    # 저자 이어쓰기: 저자가 원글이나 자기 이어쓰기에 단 답글(사슬). 날짜순이라 부모가 먼저 온다.
    rest = sorted((t for tid, t in tweets.items() if tid != root_id and tid not in quoted_ids),
                  key=lambda t: (t.get("date") or "", int(t["tweet_id"])))
    thread_ids = {root_id}
    thread: list[dict] = []
    for t in rest:
        if _name(t) == root_author and t.get("reply_id") and int(t["reply_id"]) in thread_ids:
            thread_ids.add(int(t["tweet_id"]))
            thread.append(t)

    # 댓글: gallery-dl이 준 순서(X 대화 순서) 그대로. 타인 답글만 상한으로 자른다.
    comments: list[dict] = []
    other_total = 0
    for tid, t in tweets.items():
        if tid in thread_ids or tid in quoted_ids:
            continue
        if _name(t) == root_author:
            comments.append(_item(t, quotes, "author_reply"))
            continue
        other_total += 1
        if other_total <= max_replies:
            comments.append(_item(t, quotes, "reply"))

    body = _item(root, quotes)
    author_thread = [_item(t, quotes) for t in thread]
    result = {
        "source": source,
        "platform": "x",
        "body_text": body["text"],
        "author_thread": author_thread,
        "quoted": body["quoted"],
        "comments": comments,
        "ocr_text": [],
        "transcript": None,
        "media_paths": [],
        "meta": {},
    }
    kept = list(_walk(result))
    declared = body["media"] + [m for it in kept for m in it["media"]]
    video_count = sum(1 for m in declared if m.get("type") in _VIDEO_TYPES)
    author_reply_count = sum(1 for c in comments if c["kind"] == "author_reply")
    result["meta"] = {
        "author": root_author,
        "code": str(root_id),
        "tweet_id": str(root_id),
        "created_at": root.get("date"),
        "lang": root.get("lang"),
        "likes": root.get("favorite_count", 0),
        "reply_count": root.get("reply_count", 0),
        "retweet_count": root.get("retweet_count", 0),
        "quote_count": root.get("quote_count", 0),
        "view_count": root.get("view_count"),
        "media": body["media"],  # 원글 미디어 선언(url·type·가로세로)
        "author_thread": {"count": len(author_thread)},
        "quoted_count": len(result["quoted"]),
        "author_reply_count": author_reply_count,
        "other_reply_count": len(comments) - author_reply_count,
        "other_reply_total": other_total,
        "max_replies": max_replies,
        "replies_truncated": other_total > max_replies,
        "image_count": len(declared) - video_count,
        "video_count": video_count,
        "has_video": video_count > 0,
        "media_label": "none",
        "kept_tweet_ids": sorted([root_id, *(int(it["id"]) for it in kept)]),
        "gallery_dl_errors": [i[-1] for i in data if isinstance(i, list) and i and i[0] == -1],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    return result


def _walk(result: dict):
    """원글을 뺀, 결과에 남은 모든 트윗 item(인용 사슬까지)을 돈다."""
    stack = [*result["quoted"], *result["author_thread"], *result["comments"]]
    while stack:
        it = stack.pop()
        yield it
        stack.extend(it["quoted"])


def _attach_media(result: dict, paths: list[str]) -> dict:
    """받은 파일을 트윗 id로 각 item에 붙이고 실제·선언 가로세로를 남긴다(`_media_file`)."""
    by_id: dict[int, list[str]] = {}
    for p in paths:
        tid = _tweet_id_of(p)
        if tid is not None:
            by_id.setdefault(tid, []).append(p)
    root_id = int(result["meta"]["tweet_id"])
    root_paths = by_id.get(root_id, [])
    items = list(_walk(result))
    kept_paths = list(root_paths)
    for it in items:
        it["media_paths"] = by_id.get(int(it["id"]), [])
        kept_paths += it["media_paths"]
    expected = len(result["meta"]["media"]) + sum(len(it["media"]) for it in items)
    meta = result["meta"]
    meta["media_files"] = [_media_file(p) for p in kept_paths]
    if not kept_paths:
        meta["media_label"] = "download_failed" if expected else "none"
    else:
        meta["media_label"] = "downloaded" if len(kept_paths) >= expected else "partially_downloaded"
    if meta["has_video"] and meta["media_label"] != "downloaded":
        meta["video_label"] = meta["media_label"]
    return {**result, "media_paths": kept_paths}
