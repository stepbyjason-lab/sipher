r"""
sipher-youtube 전사(옵션) — youtube-transcript-api(MIT) 래핑.

정제된 자막 텍스트를 반환한다(get_clean_transcript 대체). API 키·헤드리스 브라우저 불필요.
미설치·조회 실패 시 (None, label)을 반환(graceful) → 호출자는 whisper 폴백으로 넘어간다.

youtube-transcript-api는 버전에 따라 API가 다르므로(신 1.x 인스턴스 `fetch` /
구 classmethod `get_transcript`) 어떤 API를 쓸지 예외로 추측하지 않고
`hasattr(YouTubeTranscriptApi, "get_transcript")`로 명시 분기한다.
"""
from __future__ import annotations

import logging
import re

_log = logging.getLogger(__name__)

_WS = re.compile(r"[ \t]+")


def fetch_transcript(
    video_id: str, *, languages: tuple[str, ...] | list[str] = ("ko", "en"),
) -> tuple[str | None, str]:
    """video_id의 자막을 정제 텍스트로 반환.

    languages 우선순위대로 수동 자막 → 자동 자막을 탐색한다(라이브러리 기본 동작).
    반환: (text, label). label ∈ {"fetched", "unavailable", "fetch_failed"}.
    - "unavailable": 자막 자체가 없음(정직한 없음 — NoTranscriptFound/TranscriptsDisabled).
    - "fetch_failed": 그 외 조회 실패(네트워크·차단 등) 또는 라이브러리 미설치.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # 지연 import(옵션 의존성)
        from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled
    except ImportError:
        _log.warning("youtube-transcript-api 미설치 — transcript 생략"
                     "(pip install youtube-transcript-api)")
        return None, "fetch_failed"

    langs = list(languages)
    segments: list[str] = []
    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):  # 구 API: classmethod
            data = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
            segments = [d.get("text", "") for d in data]
        else:  # 신 API(>=1.0): 인스턴스.fetch → FetchedTranscript(iterable of snippets)
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id, languages=langs)
            segments = [getattr(s, "text", "") for s in fetched]
    except (NoTranscriptFound, TranscriptsDisabled) as e:
        _log.info("transcript 없음 video_id=%s — %s", video_id, type(e).__name__)
        return None, "unavailable"
    except Exception as e:  # 그 외 조회 실패(네트워크/차단 등)
        _log.warning("transcript 조회 실패 video_id=%s — %s", video_id, type(e).__name__)
        return None, "fetch_failed"

    text = _clean(segments)
    if not text:
        return None, "unavailable"
    return text, "fetched"


def _clean(segments: list[str]) -> str | None:
    """세그먼트 리스트 → 정제 텍스트. 연속 중복 라인(자동자막 롤링) 제거."""
    lines: list[str] = []
    prev: str | None = None
    for raw in segments:
        s = _WS.sub(" ", (raw or "").replace("\n", " ")).strip()
        if not s or s == prev:
            continue
        lines.append(s)
        prev = s
    text = "\n".join(lines)
    return text or None
