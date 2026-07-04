r"""
sipher-youtube 댓글(옵션) — youtube-comment-downloader(MIT) 래핑.

YouTube Data API·키 없이 댓글을 가져온다. 인기순 정렬로 상위/고정 댓글이 먼저 오므로
overview §5의 "첫·고정 댓글"(옵션)을 커버한다. 미설치·실패 시 ([], label) 반환(graceful).
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def fetch_comments(video_id: str, *, limit: int = 20) -> tuple[list[dict], str]:
    """상위(인기순) 댓글 최대 limit개를 정규화 dict 리스트로 반환.

    반환: (comments, label). label ∈ {"fetched", "fetch_failed"}.
    중간에 조회가 끊겨도 이미 모은 댓글은 유지하되 label로 실패를 정직하게 표시한다.
    """
    try:
        from youtube_comment_downloader import (  # 지연 import(옵션 의존성)
            YoutubeCommentDownloader, SORT_BY_POPULAR,
        )
    except ImportError:
        _log.warning("youtube-comment-downloader 미설치 — 댓글 생략"
                     "(pip install youtube-comment-downloader)")
        return [], "fetch_failed"

    if limit <= 0:
        return [], "fetched"

    out: list[dict] = []
    try:
        dl = YoutubeCommentDownloader()
        for i, c in enumerate(dl.get_comments(video_id, sort_by=SORT_BY_POPULAR)):
            if i >= limit:
                break
            out.append({
                "author": c.get("author"),
                "text": c.get("text"),
                "votes": c.get("votes"),
                "time": c.get("time"),
                "heart": bool(c.get("heart")),
                "reply": bool(c.get("reply")),
            })
    except Exception as e:  # 네트워크/파싱 실패 → 부분 수집분이라도 반환
        _log.warning("댓글 조회 실패 video_id=%s — %s (부분 %d건)",
                     video_id, type(e).__name__, len(out))
        return out, "fetch_failed"
    return out, "fetched"
