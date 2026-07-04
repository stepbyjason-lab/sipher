r"""
sipher-naver-blog 어댑터 — 독립 도구(패키지).

공개 API: `fetch(url) -> 정규화 JSON dict` (sipher 라우터/단독 CLI 공용).
sipher 내부를 import 하지 않는 깨끗한 경계 → 나중에 `git subtree split`로 추출 가능.

정규화 스키마: { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }
설계: docs/00-overview.md §6.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from . import scrape

__all__ = ["fetch", "parse_url", "normalize", "scrape"]

# blog.naver.com/<id>[/<logNo>] · m.blog.naver.com/<id>[/<logNo>] · ?blogId=&logNo=
_URL_PATH = re.compile(
    r"(?:m\.)?blog\.naver\.com/(?P<id>[A-Za-z0-9_-]+)(?:/(?P<log>\d+))?", re.I)
_URL_QUERY_ID = re.compile(r"[?&]blogId=(?P<id>[A-Za-z0-9_-]+)", re.I)
_URL_QUERY_LOG = re.compile(r"[?&]logNo=(?P<log>\d+)", re.I)
# 경로 첫 세그먼트가 blog_id가 아닌 정적 페이지명들(쿼리에 blogId가 실제 id)
_PATH_RESERVED = {"postview", "postlist", "prologue", "guestbook"}


def parse_url(url: str) -> tuple[str, str | None]:
    """네이버 블로그 URL → (blog_id, log_no|None).

    blog_id 추출 실패 시 ValueError. **log_no는 블로그 루트 URL이면 None**(성공 반환) —
    직접 호출자는 None 여부를 별도로 확인해야 한다. PostView.naver?blogId=… 형식은
    쿼리를 우선 사용(경로의 'PostView'를 blog_id로 오인하지 않음).
    """
    # 쿼리 우선 — PostView.naver?blogId=&logNo= 형식 정확 처리
    mq = _URL_QUERY_ID.search(url)
    ml = _URL_QUERY_LOG.search(url)
    blog_id = mq.group("id") if mq else None
    log_no = ml.group("log") if ml else None
    if not blog_id:  # 경로형 blog.naver.com/<id>/<logNo>
        m = _URL_PATH.search(url)
        if m and m.group("id").lower() not in _PATH_RESERVED:
            blog_id = m.group("id")
            log_no = log_no or m.group("log")
    if not blog_id:
        # 쿼리(서명·토큰) 제거 후 메시지에 — stderr/로그 유출 방지
        raise ValueError(f"네이버 블로그 URL이 아닙니다: {url.split('?', 1)[0]!r}")
    return blog_id, log_no


def fetch(url: str, *, media_dir: str | Path | None = None) -> dict:
    """단일 포스트 URL → 정규화 JSON dict.

    media_dir 지정 시 이미지·영상을 그 경로에 다운로드하고 media_paths/label 채움.
    미지정 시 다운로드 없이 메타+이미지 개수만(빠른 메타 조회).
    블로그 루트 URL(logNo 없음)은 ValueError — 전체 수집은 scrape.scrape_blog 사용.
    """
    blog_id, log_no = parse_url(url)
    if not log_no:
        raise ValueError(
            "포스트 URL(logNo 포함)이 필요합니다. 블로그 전체 수집은 "
            "scrape.scrape_blog(blog_id) 사용."
        )
    post = scrape.scrape_post(blog_id, log_no)
    if media_dir:
        post["media_paths"], post["image_size_label"] = scrape.download_media(post, media_dir)
    return normalize(post, source=url)


def normalize(post: dict, *, source: str) -> dict:
    """중간 post dict(scrape) → sipher 정규화 스키마. 공개 API(CLI scrape 등에서 사용)."""
    return {
        "source": source,
        "platform": "naver_blog",
        "body_text": post.get("body_text", ""),
        "comments": [],          # 네이버 블로그 댓글은 별도 API(범위 밖, follow-up)
        "ocr_text": [],          # sipher 정규화 단계(어댑터 밖)에서 채움
        "transcript": None,
        "media_paths": post.get("media_paths", []),
        "meta": {
            "log_no": post.get("log_no"),
            "title": post.get("title"),
            "add_date": post.get("add_date"),
            "category": post.get("category"),
            "comment_count": post.get("comment_count"),
            "read_count": post.get("read_count"),
            "like_count": post.get("like_count"),
            "image_count": len(post.get("image_urls", [])),
            "video_count": len(post.get("video_urls", [])),
            "image_size_label": post.get("image_size_label", "none"),
            "body_truncated": post.get("body_truncated", False),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }
