r"""
sipher-youtube 수집 — yt-dlp 얇은 subprocess 래핑.

yt-dlp를 `sys.executable -m yt_dlp`로 호출(PATH 비의존, 모듈 설치만 있으면 동작).
yt-dlp 모듈을 직접 import 하지 않으므로, 이 파일을 import 해도 yt-dlp 의존성은
런타임(probe/download 호출) 시점에만 필요하다 — normalize/parse_url은 무의존 테스트 가능.

보안/견고성(멀티렌즈, docs/00-overview §4):
- 호출자는 **검증된 canonical URL**만 넘긴다(__init__.parse_url이 11자 video_id로 재구성).
  positional 앞에 `--`를 두어 옵션 오인/인자 인젝션을 차단.
- yt-dlp 미설치·실패·타임아웃은 YtdlpError로 명확히 보고(graceful degradation).
- 출력 경로는 `%(id)s.%(ext)s`로 고정 → id 기준 glob으로 영상/자막/채팅을 결정적 분리.
"""
from __future__ import annotations

import glob as _glob
import json
import logging
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

# yt-dlp 호출 프리픽스 — 현재 인터프리터의 모듈로 실행(콘솔 스크립트 PATH 비의존).
_YTDLP: list[str] = [sys.executable, "-m", "yt_dlp"]

_PROBE_TIMEOUT = 120  # 메타 조회 상한(초)

# 자막/채팅 아닌 순수 미디어 판별용 확장자.
_SUB_EXTS = (".vtt", ".srt", ".ass", ".json3", ".srv1", ".srv2", ".srv3")
_SKIP_EXTS = (".part", ".ytdl", ".temp")


class YtdlpError(RuntimeError):
    """yt-dlp 부재·실행 실패·출력 파싱 실패."""


def _run(args: list[str], *, timeout: int | None) -> subprocess.CompletedProcess[str]:
    """yt-dlp를 실행하고 CompletedProcess 반환. 실패 시 YtdlpError."""
    cmd = _YTDLP + args
    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as e:  # 인터프리터 자체 실행 불가(비정상 환경)
        raise YtdlpError(f"파이썬 실행 파일을 찾을 수 없음: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise YtdlpError(f"yt-dlp 타임아웃({timeout}s)") from e
    if cp.returncode != 0:
        tail = (cp.stderr or "").strip()[-600:]
        if "No module named" in tail and "yt_dlp" in tail:
            raise YtdlpError("yt-dlp 미설치 — `pip install yt-dlp` 필요")
        raise YtdlpError(f"yt-dlp 실패(rc={cp.returncode}): {tail}")
    return cp


def probe(canonical_url: str, *, timeout: int = _PROBE_TIMEOUT) -> dict:
    """단일 영상 메타(-J JSON) 조회 → dict. 미디어 다운로드 없음."""
    cp = _run(["-J", "--no-playlist", "--no-warnings", "--", canonical_url], timeout=timeout)
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise YtdlpError(f"yt-dlp 메타 JSON 파싱 실패: {e}") from e
    if not isinstance(data, dict) or not data.get("id"):
        raise YtdlpError("yt-dlp 메타가 유효한 영상 dict가 아님(삭제/비공개/빈 응답 가능)")
    return data


def download(canonical_url: str, video_id: str, media_dir: str | Path, *,
             from_start: bool = False, with_video: bool = True,
             with_subs: bool = True, sub_langs: str = "ko,en",
             sections: str | None = None, timeout: int | None = None) -> dict:
    """영상(+선택 자막)을 media_dir에 다운로드.

    from_start=True면 `--live-from-start`(라이브 처음부터). with_video=False면 자막만.
    yt-dlp 실행이 실패해도 예외를 올리지 않고 `_collect_outputs`로 부분 산출물을 회수한다
    (라이브 중단·타임아웃 등으로도 이미 받은 파일은 버리지 않는 graceful degradation).
    반환: {"videos": [Path], "subtitle_paths": [Path], "ok": bool}.
    """
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(media_dir / "%(id)s.%(ext)s")

    args = ["--no-playlist", "--no-warnings", "-o", out_tmpl,
            "--retries", "3", "--fragment-retries", "5"]
    if not with_video:
        args.append("--skip-download")
    if from_start:
        args.append("--live-from-start")
    if with_subs:
        args += ["--write-subs", "--write-auto-subs",
                 "--sub-langs", sub_langs, "--sub-format", "vtt/srt/best"]
    if sections:  # yt-dlp 시간 구간(예 "*0-300") — range 요청으로 해당 구간만 받음
        args += ["--download-sections", sections]
    args += ["--", canonical_url]

    ok = True
    try:
        _run(args, timeout=timeout)
    except YtdlpError as e:
        ok = False
        _log.warning("영상 다운로드 실패(부분 산출물 회수 시도) video_id=%s — %s", video_id, e)
    out = _collect_outputs(media_dir, video_id)
    return {"videos": out["videos"], "subtitle_paths": out["subtitle_paths"], "ok": ok}


def download_live_chat(canonical_url: str, video_id: str, media_dir: str | Path, *,
                       timeout: int | None = None) -> dict:
    """라이브 채팅 replay를 `<id>.live_chat.json`으로 다운로드(영상 다운 없음).

    채팅 replay가 비활성이거나 없는 영상이면 yt-dlp가 실패할 수 있다(정직 라벨 구분 목적으로
    실패와 "채팅 없음"을 status로 분리 — path만으로는 원인을 구분할 수 없었다).
    반환: {"path": Path|None, "status": "ok"|"failed"}.
    """
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(media_dir / "%(id)s.%(ext)s")
    args = ["--no-playlist", "--no-warnings", "--skip-download", "-o", out_tmpl,
            "--write-subs", "--sub-langs", "live_chat", "--", canonical_url]
    status = "ok"
    try:
        _run(args, timeout=timeout)
    except YtdlpError as e:
        status = "failed"
        _log.warning("라이브 채팅 다운로드 실패(채팅 비활성/없음일 수 있음) — %s", e)
    chat = media_dir / f"{video_id}.live_chat.json"
    return {"path": chat if chat.exists() else None, "status": status}


def _collect_outputs(media_dir: Path, video_id: str) -> dict:
    """media_dir에서 video_id 산출물을 영상/자막/채팅으로 분리."""
    videos: list[Path] = []
    subs: list[Path] = []
    for p in sorted(media_dir.glob(f"{_glob.escape(video_id)}.*")):
        name = p.name.lower()
        if name.endswith(_SKIP_EXTS):
            continue
        if name.endswith(".live_chat.json"):
            continue  # 채팅은 download_live_chat이 별도 회수
        if name.endswith(_SUB_EXTS):
            subs.append(p)
        else:
            videos.append(p)
    return {"videos": videos, "subtitle_paths": subs}
