r"""
sipher-youtube 어댑터 — 독립 도구(패키지).

공개 API: `fetch(url) -> 정규화 JSON dict` (sipher 라우터/단독 CLI 공용).
sipher 내부를 import 하지 않는 깨끗한 경계 → 나중에 `git subtree split`로 추출 가능.

정규화 스키마: { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }
설계: docs/00-overview.md.

yt-dlp(Unlicense)를 코어로 쓰고, transcript/comments는 옵션 pip(MIT)로 채운다(MCP 비의존).
`parse_url`/`normalize`는 yt-dlp 없이 import·테스트 가능(scrape는 런타임에만 yt-dlp 호출).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from . import comments as _comments
from . import scrape
from . import transcript as _transcript

__all__ = ["fetch", "parse_url", "normalize", "scrape"]

_log = logging.getLogger(__name__)

VideoLabel = Literal["none", "downloaded", "downloaded_from_start", "download_failed", "clipped"]
ChatLabel = Literal["none", "replay_full", "live_captured", "disabled", "download_failed"]
TranscriptLabel = Literal["none", "fetched", "unavailable", "fetch_failed"]
CommentsLabel = Literal["none", "fetched", "fetch_failed"]
EngagementLabel = Literal["computed", "zero_views", "unavailable"]

# 자막 언어 코드 토큰 형식: 문자로 시작 + 영문자/숫자/`_`/`-` (BCP-47 근사, "--exec" 같은
# yt-dlp 인자 인젝션 시도를 차단).
_LANG_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# YouTube video_id = 정확히 11자 [A-Za-z0-9_-]. 이것만 통과시켜 canonical URL을 재구성한다.
_ID = r"(?P<id>[A-Za-z0-9_-]{11})"
_HOST = re.compile(r"^(?:https?://)?(?:[\w-]+\.)?(?:youtube\.com|youtu\.be)/", re.I)
_V_PARAM = re.compile(r"[?&]v=" + _ID)
_SHORT = re.compile(r"youtu\.be/" + _ID)
_PATH = re.compile(r"/(?:live|shorts|embed|v)/" + _ID)
_BARE_ID = re.compile(r"^" + _ID + r"$")


def parse_url(url: str) -> str:
    """YouTube URL(또는 순수 11자 id) → video_id. 실패 시 ValueError.

    호스트를 youtube.com/youtu.be로 제한(SSRF·인자 인젝션 방어)하고 11자 id만 추출한다.
    watch?v= · youtu.be/ · /live//shorts//embed//v/ 형식을 지원.
    """
    if not isinstance(url, str):
        raise ValueError("URL은 문자열이어야 합니다")
    s = url.strip()
    if len(s) > 2048:
        raise ValueError("URL이 너무 깁니다")

    m = _BARE_ID.match(s)
    if m:
        return m.group("id")
    if not _HOST.match(s):
        raise ValueError(f"YouTube URL이 아닙니다: {s.split('?', 1)[0]!r}")
    for pat in (_V_PARAM, _SHORT, _PATH):
        m = pat.search(s)
        if m:
            return m.group("id")
    raise ValueError(f"video_id를 찾을 수 없습니다: {s.split('?', 1)[0]!r}")


def _canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _clean_sub_langs(sub_langs: str) -> str:
    """자막 언어 코드 문자열을 검증·정제한다.

    `,`로 split → 각 토큰 strip → 빈 토큰 제거 → 각 토큰이 `_LANG_TOKEN`에 안 맞으면
    (예: `-`로 시작하는 yt-dlp 옵션 인젝션 시도) ValueError. 남는 토큰이 없으면 "en" 기본값으로
    degrade(사용자 입력 실수를 조용히 삼키지 않고 경고 로그는 남긴다).
    """
    tokens = [t.strip() for t in sub_langs.split(",")]
    tokens = [t for t in tokens if t]
    for t in tokens:
        if not _LANG_TOKEN.match(t):
            raise ValueError(f"잘못된 자막 언어 코드: {t!r}")
    if not tokens:
        _log.warning("자막 언어 코드가 비어 있음 — 기본값 'en' 사용")
        tokens = ["en"]
    return ",".join(tokens)


def _video_label(info: dict, *, from_start: bool, with_video: bool, got_video: bool,
                 ok: bool, clipped: bool = False) -> VideoLabel:
    if not with_video:
        return "none"
    if not ok:
        return "download_failed"
    if not got_video:
        return "download_failed"
    if clipped:
        return "clipped"
    if from_start and info.get("live_status") in ("is_live", "was_live", "post_live"):
        return "downloaded_from_start"
    return "downloaded"


def _chat_label(info: dict, *, with_chat: bool, chat_path: Path | None, count: int,
                status: str) -> ChatLabel:
    if not with_chat:
        return "none"
    if status == "failed":
        return "download_failed"
    if chat_path and count > 0:
        return "live_captured" if info.get("live_status") == "is_live" else "replay_full"
    return "disabled"


def _engagement(info: dict) -> dict | None:
    vc = info.get("view_count")
    if not isinstance(vc, (int, float)) or vc <= 0:
        return None
    like = info.get("like_count")
    like = like if isinstance(like, (int, float)) else 0
    comment = info.get("comment_count")
    comment = comment if isinstance(comment, (int, float)) else 0
    return {
        "like_rate": round(like / vc, 6),
        "comment_rate": round(comment / vc, 6),
    }


def _engagement_label(info: dict) -> EngagementLabel:
    """`meta.engagement`(dict|None)의 None이 "데이터 없음"인지 "조회수 0(정상)"인지
    구분하기 위한 병행 라벨. `_engagement`의 None 반환 로직(view_count 결측/비수치/0 이하 → None)은
    그대로 두고, 여기서만 0과 결측을 나눠 정직하게 표기한다.
    """
    vc = info.get("view_count")
    if not isinstance(vc, (int, float)):
        return "unavailable"
    if vc > 0:
        return "computed"
    if vc == 0:
        return "zero_views"
    return "unavailable"


def _count_chat(chat_path: Path | None) -> int:
    if not chat_path:
        return 0
    try:
        with chat_path.open(encoding="utf-8") as fh:
            return sum(1 for ln in fh if ln.strip())
    except OSError:
        _log.warning("채팅 파일 읽기 실패 %s", chat_path)
        return 0


def fetch(url: str, *, media_dir: str | Path | None = None, from_start: bool = False,
          with_video: bool = True, with_subs: bool = True, sub_langs: str = "ko,en",
          with_chat: bool = False, with_transcript: bool = False,
          with_comments: bool = False, max_comments: int = 20,
          sections: str | None = None, timeout: int | None = None) -> dict:
    """단일 YouTube 영상 URL → 정규화 JSON dict.

    - media_dir 지정 시에만 미디어/자막/채팅을 다운로드(미지정=메타만, video_label=none).
    - from_start=True: 라이브를 처음부터(`--live-from-start`).
    - with_chat=True: 라이브 채팅 replay를 <id>.live_chat.json으로(미디어_dir 필요).
    - with_transcript / with_comments: 옵션 pip로 transcript/comments 채움(미설치 시 skip).

    보안 경고(trusted input): media_dir는 **신뢰 입력**이다. 로컬 사용자가 지정하는 출력
    경로이며, 이 함수는 경로를 검증·containment하지 않는다. 미신뢰·사용자대면 입력(예: 웹
    폼에서 받은 값)을 상위 계층 검증 없이 그대로 넘기면 임의 경로 쓰기가 된다. 개인용 CLI
    의도를 보존하기 위해 여기서 하드 containment는 넣지 않는다 — 호출자가 신뢰 경계를 지켜야 한다.
    """
    sub_langs = _clean_sub_langs(sub_langs)
    video_id = parse_url(url)
    canonical = _canonical(video_id)
    info = scrape.probe(canonical)

    videos: list[Path] = []
    subs: list[Path] = []
    chat_path: Path | None = None
    video_label: VideoLabel = "none"

    if media_dir and (with_video or with_subs):
        dl = scrape.download(canonical, video_id, media_dir, from_start=from_start,
                             with_video=with_video, with_subs=with_subs,
                             sub_langs=sub_langs, sections=sections, timeout=timeout)
        videos, subs, ok = dl["videos"], dl["subtitle_paths"], dl["ok"]
        video_label = _video_label(info, from_start=from_start, with_video=with_video,
                                   got_video=bool(videos), ok=ok, clipped=bool(sections))

    chat_status = "ok"
    if with_chat:
        if media_dir:
            cr = scrape.download_live_chat(canonical, video_id, media_dir, timeout=timeout)
            chat_path, chat_status = cr["path"], cr["status"]
        else:
            _log.warning("--with-chat은 media_dir이 필요합니다 — 채팅 생략")

    chat_count = _count_chat(chat_path)
    transcript, transcript_label = (
        _transcript.fetch_transcript(video_id, languages=tuple(sub_langs.split(",")))
        if with_transcript else (None, "none")
    )
    comment_list, comments_label = (
        _comments.fetch_comments(video_id, limit=max_comments) if with_comments else ([], "none")
    )

    return normalize(
        info, source=url, videos=videos, subtitle_paths=subs, chat_path=chat_path,
        chat_count=chat_count, video_label=video_label,
        chat_label=_chat_label(info, with_chat=with_chat, chat_path=chat_path,
                               count=chat_count, status=chat_status),
        from_start=from_start, transcript=transcript, transcript_label=transcript_label,
        comments=comment_list, comments_label=comments_label,
    )


def normalize(info: dict, *, source: str, videos: list[Path] | None = None,
              subtitle_paths: list[Path] | None = None, chat_path: Path | None = None,
              chat_count: int = 0, video_label: VideoLabel = "none",
              chat_label: ChatLabel = "none", from_start: bool = False,
              transcript: str | None = None, transcript_label: TranscriptLabel = "none",
              comments: list[dict] | None = None,
              comments_label: CommentsLabel = "none") -> dict:
    """yt-dlp info dict → sipher 정규화 스키마(platform=youtube). 공개 API."""
    videos = videos or []
    subtitle_paths = subtitle_paths or []
    comments = comments or []
    return {
        "source": source,
        "platform": "youtube",
        "body_text": info.get("description") or "",
        "comments": comments,
        "ocr_text": [],          # sipher 정규화 단계(어댑터 밖)에서 채움
        "transcript": transcript,  # None이면 whisper(다운스트림) 폴백
        "media_paths": [str(p) for p in videos],
        "meta": {
            "video_id": info.get("id"),
            "title": info.get("title"),
            "webpage_url": info.get("webpage_url"),
            "channel": info.get("channel"),
            "channel_id": info.get("channel_id"),
            "uploader": info.get("uploader"),
            "upload_date": info.get("upload_date"),
            "duration": info.get("duration"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "live_status": info.get("live_status"),
            "was_live": info.get("was_live"),
            # from_start = 요청 의도(사용자가 --from-start 줬는지). 실제로 처음부터 받아졌는지의
            # 진실원천은 video_label=="downloaded_from_start"다(라이브가 아니면 의도해도 무시됨).
            "from_start": from_start,
            "video_label": video_label,
            "subtitle_paths": [str(p) for p in subtitle_paths],
            "subtitle_langs": sorted((info.get("subtitles") or {}).keys()),
            "auto_caption_available": bool(info.get("automatic_captions")),
            "auto_caption_langs": sorted((info.get("automatic_captions") or {}).keys()),
            "chapters": info.get("chapters") or [],
            "heatmap": info.get("heatmap") or [],
            "engagement": _engagement(info),
            "engagement_label": _engagement_label(info),
            "live_chat_path": str(chat_path) if chat_path else None,
            "chat_message_count": chat_count,
            "chat_label": chat_label,
            "transcript_label": transcript_label,
            "comments_label": comments_label,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }
