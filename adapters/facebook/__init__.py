r"""
sipher-fb 어댑터(패키지) — 독립 도구.

공개 API:
- `fetch(url) -> 정규화 dict`         : 단일 포스트 permalink → 정규화 JSON(이미지 풀사이즈+영상 보강)
- `scrape(target) -> list[정규화 dict]`: 프로필/페이지 타임라인 전체 → 정규화 JSON 배열
- `parse_url(url) -> (kind, target)`  : 'post'(permalink) vs 'profile'(타임라인) 분류
- `normalize(post, source)`           : 중간 post dict → sipher 정규화 스키마
- 서브모듈 auth / scrape / refetch_images / refetch_video

sipher 내부 미-import 경계 유지 → `git subtree split`로 추출 가능.
playwright는 fetch/scrape 실행 시에만 지연 import(normalize/parse_url은 의존성 없이 테스트 가능).

정규화 스키마: { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }
설계: docs/00-overview.md §6, adapters/naver_blog 참조 구현과 동일 계약.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from . import auth, refetch_images, refetch_video, scrape

__all__ = ["fetch", "scrape_profile_normalized", "parse_url", "normalize",
           "auth", "scrape", "refetch_images", "refetch_video"]

_log = logging.getLogger(__name__)

# 포스트(개별 글) 식별자 — 하나라도 있으면 'post'. 없으면 'profile/page' 타임라인.
# /share/p/<id> (포스트)·/share/v/<id> (영상)·/share/r/ 등 공유 단축링크도 개별 글 → post.
# (실제 페이지는 redirect되며 page.goto가 따라감)
_POST_MARKERS = re.compile(
    r"(/posts/|/permalink\.php|story_fbid=|/videos/|/watch|/share/|[?&]fbid=|/photo)", re.I)


def parse_url(url: str) -> tuple[str, str]:
    """FB URL → ('post'|'profile', 정규 target).

    facebook.com 외 호스트/형식은 scrape.resolve_target이 ValueError를 던진다(입력검증).
    post 마커(posts/permalink/story_fbid/videos/fbid/photo)가 있으면 'post', 없으면 'profile'.
    """
    target = scrape.resolve_target(url)  # facebook.com 검증 + 프로필ID→URL 정규화
    kind = "post" if _POST_MARKERS.search(url) else "profile"
    return kind, target


# round-14: comments=True인데 보강 자체가 실패/미실행이면 이 상수로 라벨 결정.
# fetch()가 comments=False(기본, 옵트인 안 함)면 아예 comments_raw를 안 넘기므로
# "not_collected"가 기본 유지된다(§정직 라벨 — 하위호환).
_DEFAULT_COMMENTS_LABEL = "not_collected"


def _merge_enriched(permalink: str, *, base: dict | None = None,
                    img: dict | None = None, vid: dict | None = None,
                    cmt: dict | None = None) -> dict:
    """scrape 중간 dict + 이미지/영상/댓글 보강 결과 → 통합 post dict(normalize 입력)."""
    base = base or {}
    img = img or {}
    vid = vid or {}
    # 본문: 보강(더보기 펼친) 텍스트 우선, 없으면 scroll 단계 텍스트
    text = img.get("text") or base.get("text") or ""
    merged = {
        "permalink": permalink,
        "text": text,
        "image_urls": img.get("image_urls") or base.get("image_urls") or [],
        "local_images": img.get("local_images") or base.get("local_images") or [],
        "fullsize_label": img.get("fullsize_label", base.get("image_label", "none")),
        "video_urls": vid.get("video_urls") or base.get("video_urls") or [],
        "local_videos": vid.get("local_videos") or [],
        "video_label": vid.get("video_label", "none"),
        "likes": base.get("likes"),
        "comments": base.get("comments"),  # FB가 노출하는 댓글 "개수"(기존 필드, 본문 정규식 파싱)
    }
    if cmt is not None:  # comments=True 옵트인일 때만 채움(기본은 키 자체가 없어 정직 not_collected 유지)
        merged["comments_raw"] = cmt.get("comments") or []
        merged["comments_label"] = cmt.get("comments_label", "none")
    return merged


def normalize(post: dict, *, source: str) -> dict:
    """중간 post dict → sipher 정규화 스키마(platform=facebook). 공개 API.

    round-14: post에 "comments_raw"/"comments_label" 키가 있으면(=fetch(comments=True)
    옵트인 경로) Threads 동형 comments[]로 채운다. 없으면(기본 comments=False) 기존
    하위호환 동작 그대로 — comments=[] + meta.comments_label="not_collected".
    """
    local_images = post.get("local_images") or []
    local_videos = post.get("local_videos") or []
    has_comment_attempt = "comments_raw" in post
    comments_raw = post.get("comments_raw") or []
    comments = [
        {
            "id": c.get("id"),
            "author": c.get("author"),
            "text": c.get("text") or "",
            "likes": c.get("likes"),
            "reply_count": c.get("reply_count", 0),
            "media_paths": c.get("media_paths") or [],
        }
        for c in comments_raw
    ] if has_comment_attempt else []
    comments_label = post.get("comments_label", _DEFAULT_COMMENTS_LABEL) if has_comment_attempt \
        else _DEFAULT_COMMENTS_LABEL
    return {
        "source": source,
        "platform": "facebook",
        "body_text": post.get("text", ""),
        "comments": comments,
        "ocr_text": [],          # sipher 정규화 단계(어댑터 밖)에서 채움
        "transcript": None,      # 영상 전사는 note-factory/whisper(어댑터 밖)
        "media_paths": list(local_images) + list(local_videos),
        "meta": {
            "permalink": post.get("permalink"),
            "image_count": len(post.get("image_urls") or []),
            "video_count": len(post.get("video_urls") or []),
            "local_image_count": len(local_images),
            "local_video_count": len(local_videos),
            "fullsize_label": post.get("fullsize_label", "none"),
            "video_label": post.get("video_label", "none"),
            "likes": post.get("likes"),
            "comment_count": post.get("comments"),
            "comment_count_captured": len(comments) if has_comment_attempt else None,
            "comments_label": comments_label,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def _open_auth(p, *, auth_mode, profile_dir, cookies_path, browser_name, headless):
    return auth.authenticated_context(
        p, auth_mode, profile_dir=profile_dir, cookies_path=cookies_path,
        browser_name=browser_name, headless=headless)


def _maybe_fetch_comments(ctx, permalink: str, *, comments: bool,
                          caption_snippet: str | None = None) -> dict | None:
    """comments=True일 때만 scrape.enrich_post_comments 호출(round-14 옵트인).

    한 포스트의 댓글 보강 실패가 전체 fetch를 죽이지 않는다(계약 §Reliability) —
    실패 시 label="fetch_failed"으로 degrade(round-16: '댓글0'과 구분), 예외를 삼키지 않고 로그만 남긴다.
    """
    if not comments:
        return None
    try:
        return scrape.enrich_post_comments(ctx, permalink, caption_snippet=caption_snippet)
    except Exception as e:  # 댓글 보강 자체가 예기치 않게 죽어도 포스트 전체는 살린다
        _log.warning("댓글 보강 실패(포스트는 계속 진행) %s — %s",
                     scrape._redact(permalink), type(e).__name__)
        return {"comments": [], "comments_label": "fetch_failed"}


def fetch(url: str, *, media_dir: str | Path | None = None,
          auth_mode: str = "persistent", profile_dir: str | Path | None = None,
          cookies_path: str | Path | None = None, browser_name: str = "firefox",
          headless: bool = True, with_video: bool = True, deep: bool = False,
          comments: bool = False) -> dict:
    """단일 포스트 permalink → 정규화 JSON dict.

    인증 context를 열어 풀사이즈 이미지(T3) + 영상(T4)을 보강한 뒤 정규화한다.
    프로필/페이지 URL이면 ValueError(전체 수집은 scrape_profile_normalized 사용).
    media_dir 지정 시 미디어 다운로드 + media_paths 채움.
    deep=True면 앨범 set 전체 확장(과수집 위험, 옵트인) — 기본은 포스트(article) 한정.
    comments=True면 댓글 본문 보강(round-14, 옵트인 — 기본 off, DOM 왕복 추가비용).
    """
    from playwright.sync_api import sync_playwright  # 지연 import(테스트성)

    kind, _target = parse_url(url)
    if kind != "post":
        raise ValueError(
            "포스트 permalink가 필요합니다(프로필/페이지 전체는 scrape_profile_normalized 사용).")

    with sync_playwright() as p:
        _log.info("인증 컨텍스트 여는 중(mode=%s, headless=%s)...", auth_mode, headless)
        ctx, browser = _open_auth(p, auth_mode=auth_mode, profile_dir=profile_dir,
                                  cookies_path=cookies_path, browser_name=browser_name,
                                  headless=headless)
        _log.info("인증 OK — 포스트 수집 시작 %s", scrape._redact(url))
        try:
            img = refetch_images.enrich_post_images(ctx, url, media_dir=media_dir, deep=deep)
            vid = refetch_video.enrich_post_videos(ctx, url, media_dir=media_dir) if with_video else None
            cmt = _maybe_fetch_comments(ctx, url, comments=comments, caption_snippet=img.get("text", "")[:80] or None)
            post = _merge_enriched(url, img=img, vid=vid, cmt=cmt)
        finally:
            try:
                ctx.close()
            finally:
                if browser is not None:
                    browser.close()
    return normalize(post, source=url)


def scrape_profile_normalized(target: str, *, media_dir: str | Path | None = None,
                              auth_mode: str = "persistent",
                              profile_dir: str | Path | None = None,
                              cookies_path: str | Path | None = None,
                              browser_name: str = "firefox", headless: bool = True,
                              max_scrolls: int = 300, enrich: bool = True,
                              with_video: bool = True, deep: bool = False,
                              comments: bool = False) -> list[dict]:
    """프로필/페이지 타임라인 → 정규화 dict 배열.

    1) scrape.scrape_profile로 타임라인 순회(permalink·썸네일·영상 URL 수집).
    2) enrich=True면 각 permalink를 재방문해 풀사이즈 이미지(T3)+영상(T4) 보강.
    3) comments=True면 각 permalink를 추가로 재방문해 댓글 보강(round-14, 옵트인).
    4) per-post normalize(한 건 실패가 전체를 버리지 않음).
    """
    from playwright.sync_api import sync_playwright  # 지연 import

    with sync_playwright() as p:
        ctx, browser = _open_auth(p, auth_mode=auth_mode, profile_dir=profile_dir,
                                  cookies_path=cookies_path, browser_name=browser_name,
                                  headless=headless)
        results: list[dict] = []
        try:
            posts = scrape.scrape_profile(ctx, target, media_dir=media_dir,
                                          max_scrolls=max_scrolls)
            for base in posts:
                permalink = base.get("permalink")
                try:
                    if enrich and permalink:
                        img = refetch_images.enrich_post_images(ctx, permalink, media_dir=media_dir, deep=deep)
                        vid = refetch_video.enrich_post_videos(ctx, permalink, media_dir=media_dir) if with_video else None
                        cmt = _maybe_fetch_comments(ctx, permalink, comments=comments,
                                                    caption_snippet=img.get("text", "")[:80] or None)
                        merged = _merge_enriched(permalink, base=base, img=img, vid=vid, cmt=cmt)
                    else:
                        cmt = _maybe_fetch_comments(ctx, permalink, comments=comments) if permalink else None
                        merged = _merge_enriched(permalink or "", base=base, cmt=cmt)
                    results.append(normalize(merged, source=permalink or target))
                except Exception as e:  # 한 포스트 실패 → 로그 후 계속
                    _log.warning("포스트 정규화 실패 %s — %s",
                                 scrape._redact(permalink or ""), type(e).__name__)
        finally:
            try:
                ctx.close()
            finally:
                if browser is not None:
                    browser.close()
    _log.info("프로필 수집 완료 posts=%d", len(results))
    return results
