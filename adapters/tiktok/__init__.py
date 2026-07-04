r"""
sipher-tiktok 어댑터 — 독립 도구(패키지). pip 라이브러리(gallery-dl) 직접
subprocess 호출(벤더링 아님).

공개 API: `fetch(url) -> 정규화 JSON dict` (sipher 라우터/단독 CLI 공용).
sipher 내부를 import 하지 않는 깨끗한 경계(threads/naver_blog/instagram과 동일 원칙).

정규화 스키마: { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }
설계: docs/00-overview.md. round-09 contract(.handoff/rounds/round-09-sns-adapters-contract.md).

gallery-dl은 `sys.executable -m gallery_dl`로 호출한다(PATH에 CLI 콘솔 스크립트가
없어도 동작 — adapters/youtube의 yt-dlp 호출 패턴과 동일 원칙, core/transcribe.py의
subprocess 안전 원칙도 동일: list 인자, shell=False).

첫 댓글 수집은 이번 스코프 밖이다 — `--dump-json`이 댓글을 채우지 않음(round-09
spike로 실측 확인, contract §비목표).
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

__all__ = ["fetch", "parse_url", "normalize"]

_log = logging.getLogger(__name__)

MediaLabel = Literal["none", "downloaded", "download_failed"]

# tiktok.com(+www.) 및 단축 링크 도메인(vt./vm.)만 허용(SSRF 방어). 단축 링크는
# gallery-dl이 자체적으로 리다이렉트를 해석하므로(round-09 spike로 확인된 바는
# 정식 tiktok.com/@user/video/<id> 경로 — vt/vm은 host 화이트리스트만 통과시키고
# video id 재구성은 하지 않는다: threads/instagram과 달리 URL 자체가 gallery-dl의
# 입력 단위이기 때문).
_HOST = re.compile(r"^(?:https?://)?(?:www\.|vt\.|vm\.)?tiktok\.com/", re.I)

_GALLERY_DL: list[str] = [sys.executable, "-m", "gallery_dl"]
_DUMP_TIMEOUT = 60
_DOWNLOAD_TIMEOUT = 180


class GalleryDlError(RuntimeError):
    """gallery-dl 부재·실행 실패·출력 파싱 실패."""


def parse_url(url: str) -> str:
    """TikTok URL → 검증된(host 화이트리스트 통과) URL 그대로 반환. 실패 시 ValueError.

    threads/instagram의 parse_url과 달리 shortcode/video id를 추출해 canonical URL을
    재구성하지 않는다 — gallery-dl은 원본 URL(정식 링크든 vt/vm 단축 링크든)을 그대로
    입력 단위로 받아 자체적으로 해석하기 때문(video id 형식이 계정 유형별로 달라
    안전하게 재구성할 정규 스킴이 없음). host 화이트리스트만이 SSRF 방어선이다.
    """
    if not isinstance(url, str):
        raise ValueError("URL은 문자열이어야 합니다")
    s = url.strip()
    if len(s) > 2048:
        raise ValueError("URL이 너무 깁니다")
    if not _HOST.match(s):
        raise ValueError(f"TikTok URL이 아닙니다: {s.split('?', 1)[0]!r}")
    return s


def _run(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    cmd = _GALLERY_DL + args
    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, shell=False,
        )
    except FileNotFoundError as e:
        raise GalleryDlError(f"파이썬 실행 파일을 찾을 수 없음: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise GalleryDlError(f"gallery-dl 타임아웃({timeout}s)") from e
    return cp


def _dump_json(canonical_url: str) -> dict:
    """`--dump-json`으로 메타만 조회(다운로드 없음). 실패 시 GalleryDlError."""
    cp = _run(["--dump-json", "--", canonical_url], timeout=_DUMP_TIMEOUT)
    if cp.returncode != 0:
        tail = (cp.stderr or "").strip()[-600:]
        if "No module named" in tail and "gallery_dl" in tail:
            raise GalleryDlError("gallery-dl 미설치 — `pip install gallery-dl` 필요")
        raise GalleryDlError(f"gallery-dl 실패(rc={cp.returncode}): {tail}")
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise GalleryDlError(f"gallery-dl 메타 JSON 파싱 실패: {e}") from e
    # gallery-dl dump-json은 [[kind, payload], ...] 형태(kind=2는 미디어 항목,
    # round-09 spike로 실측 확인). 첫 미디어 항목의 payload를 사용한다.
    if not isinstance(data, list) or not data:
        raise GalleryDlError("gallery-dl 메타가 비어있음(비공개/삭제/차단된 URL일 수 있음)")
    entry = next((item for item in data if isinstance(item, list) and len(item) >= 2
                  and isinstance(item[1], dict) and item[1].get("desc") is not None), None)
    if entry is None:
        # desc 필드가 없어도(예: 슬라이드쇼 세부 payload) 첫 dict payload를 폴백으로 사용.
        entry = next((item for item in data if isinstance(item, list) and len(item) >= 2
                      and isinstance(item[1], dict)), None)
    if entry is None:
        raise GalleryDlError("gallery-dl 메타 구조가 예상과 다름(payload dict를 찾을 수 없음)")
    return entry[1]


def _download(canonical_url: str, out_dir: str) -> list[str]:
    """실제 미디어 다운로드. --dump-json은 다운로드하지 않으므로 별도 호출.

    다운로드된 파일 목록은 gallery-dl 표준 출력(각 줄에 저장 경로)에서 파싱한다.
    실패해도 예외를 올리지 않고 빈 리스트를 반환(media_label로 상위에서 판별).
    """
    cp = _run(["-d", out_dir, "--", canonical_url], timeout=_DOWNLOAD_TIMEOUT)
    if cp.returncode != 0:
        _log.warning("tiktok: 다운로드 실패(rc=%s): %s", cp.returncode, (cp.stderr or "")[-400:])
        return []
    paths = [line.strip() for line in (cp.stdout or "").splitlines() if line.strip()]
    return [p for p in paths if Path(p).is_file()]


def fetch(url: str, *, media_dir: str | Path | None = None, download: bool = False) -> dict:
    """단일 TikTok 영상 URL → 정규화 JSON dict.

    - `python -m gallery_dl --dump-json <url>`로 메타(desc/작성자/통계) 조회(항상
      실행 — 다운로드 없이도 캡션·메타는 얻는다).
    - download=True면 별도로 `-d media_dir <url>`을 실행해 실제 파일을 받는다
      (--dump-json은 다운로드하지 않으므로 두 번 호출 — gallery-dl 자체 계약).
    - 첫 댓글 수집은 하지 않는다(비목표 — round-09 contract).

    보안 경고(trusted input): media_dir은 로컬 사용자가 지정하는 신뢰 입력이다.
    이 함수는 경로 containment를 하지 않는다(threads/instagram과 동일 경계).
    """
    canonical = parse_url(url)
    payload = _dump_json(canonical)

    media_paths: list[str] = []
    if download:
        out_dir = str(media_dir) if media_dir else "downloads"
        media_paths = _download(canonical, out_dir)

    return normalize(payload, source=url, media_paths=media_paths, downloaded=download)


def normalize(payload: dict, *, source: str, media_paths: list[str], downloaded: bool) -> dict:
    """gallery-dl `--dump-json` payload(단일 항목) → sipher 정규화 스키마. 공개 API.

    round-09 spike로 실측한 실제 키: desc(캡션), createTime(UNIX epoch), id,
    stats/statsV2(diggCount/commentCount/playCount/shareCount/collectCount),
    author(uniqueId/nickname/verified/...), authorStats(followerCount/...),
    video(playAddr/downloadAddr/duration/width/height/...).
    """
    stats = payload.get("stats") or {}
    author = payload.get("author") or {}
    video = payload.get("video") or {}
    # gallery-dl은 createTime을 문자열로 반환하는 경우가 관측됨(round-09 실측:
    # "1768668049", int/float 아님) — 정수/실수/숫자문자열 모두 관대하게 허용한다.
    create_time_raw = payload.get("createTime")
    created_at = None
    try:
        create_time = float(create_time_raw)
        created_at = datetime.fromtimestamp(create_time, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        created_at = None

    media_label: MediaLabel = "none"
    if downloaded:
        media_label = "downloaded" if media_paths else "download_failed"

    return {
        "source": source,
        "platform": "tiktok",
        "body_text": payload.get("desc") or "",
        "comments": [],  # 첫 댓글 수집은 비목표(round-09 contract §비목표)
        "ocr_text": [],
        "transcript": None,
        "media_paths": media_paths,
        "meta": {
            "video_id": payload.get("id"),
            "author": author.get("uniqueId") or author.get("nickname"),
            "author_verified": bool(author.get("verified", False)),
            "digg_count": stats.get("diggCount", 0),
            "comment_count": stats.get("commentCount", 0),
            "play_count": stats.get("playCount", 0),
            "share_count": stats.get("shareCount", 0),
            "duration_sec": video.get("duration"),
            "media_label": media_label,
            "created_at_utc": created_at,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }
