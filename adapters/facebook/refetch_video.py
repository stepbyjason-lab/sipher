r"""
sipher-fb 영상 보강 — 자작 수집 도구 fb_video_refetch 골격 이식.

전략(보존): permalink 방문 → 비디오 lazy-load 트리거(스크롤/클릭) →
  Playwright `page.on('request')`로 .mp4/.m4s 네트워크 요청 가로채기 + DOM `<video>` 스캔
  → video id 기준 dedup → 인증 컨텍스트로 즉시 다운로드(CDN URL 만료 회피).

설계 변경점(배치 CLI → 라이브러리):
- jsonl/progress/쿠키관리/argparse/힌트필터 제거. 인증 context 주입형 "permalink 1건 → 영상" 변환.
- scrape.py 헬퍼(_redact·_ALLOWED_CDN_HOST·_atomic_write·resolve_target) 재사용(DRY).

견고성/보안 가드(9패턴 — scrape/refetch_images와 동일 기준, 원본 대비 신규 강화 표시 ★):
1. 입력검증: permalink는 facebook.com만(resolve_target). video URL은 video_id_from_url로 형태 검증.
2. _redact 로그.
3. ★크기상한(_MAX_VIDEO, content-length 사전 + body 사후) — 원본엔 상한 없음(메모리 고갈 위험).
4. ★원자적 쓰기(_atomic_write) — 원본은 직접 write_bytes(중단 시 부분파일).
5. ★호스트 화이트리스트(SSRF, _ALLOWED_CDN_HOST) — 다운로드 직전 재검사.
6. 로그레벨: 운영이슈=warning, 흔한실패=debug, 예외=type만(+exc_info debug).
7. per-video try/except — 한 영상 실패가 전체 중단 안 함.
8. fail-fast 정규식(video_id_from_url 단순 앵커).
9. 정직 라벨 video_label: network_capture(가장 확실) > dom_scan > none.

⚠️ 메모리: Playwright APIResponse.body()는 스트리밍 미지원 → 영상 전체를 메모리 적재.
  content-length로 사전 차단하되 헤더 누락 시 _MAX_VIDEO까지 메모리에 받은 뒤 사후 차단.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .scrape import _ALLOWED_CDN_HOST, _atomic_write, _redact, resolve_target

if TYPE_CHECKING:
    from playwright.sync_api import APIRequestContext, BrowserContext, Page

_log = logging.getLogger(__name__)

# FB 영상 URL path 마지막 segment(확장자 제외) = video id. fail-fast 앵커.
_VIDEO_ID_RE = re.compile(r"/([A-Za-z0-9_-]{20,})\.(?:mp4|m4s)")
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|m4s)(?:\?|$)")

_MAX_VIDEO = 300 << 20      # 300 MB 상한(피드 영상 실무 상한 — 메모리 보호)
_MIN_VIDEO_BYTES = 50_000   # 50KB 미만 = 실 영상 아님(에러/플레이스홀더)


def video_id_from_url(url: str) -> str | None:
    """FB 영상 URL → video id(확장자 제외 마지막 segment). 미매칭이면 None."""
    if not url:
        return None
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def _is_fb_video_cdn(url: str) -> bool:
    """fbcdn 호스트 + .mp4/.m4s 확장자(네트워크 캡처 필터)."""
    return bool(url) and bool(_ALLOWED_CDN_HOST.match(url)) and bool(_VIDEO_EXT_RE.search(url))


def dedup_by_video_id(urls: list[str]) -> list[str]:
    """video id 기준 순서보존 dedup. id 없는 URL은 제외(다운로드 불가)."""
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        vid = video_id_from_url(u)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        out.append(u)
    return out


def download_video(req: APIRequestContext, url: str, media_dir: Path,
                   *, timeout_ms: int = 120000) -> tuple[str | None, str]:
    """인증 컨텍스트로 영상 1건 다운로드 → (로컬경로 or None, status).

    SSRF 화이트리스트 → id 추출 → 재실행 skip → 크기상한(사전+사후) → 원자적 쓰기.
    status ∈ {cached, fetched, no_id, skip_host, http_err, req_err, tiny, too_large, write_fail}
    """
    if not _ALLOWED_CDN_HOST.match(url):
        _log.debug("영상 호스트 비허용 — skip: %s", _redact(url))
        return None, "skip_host"
    vid = video_id_from_url(url)
    if not vid:
        _log.debug("영상 id 추출 실패 — skip: %s", _redact(url))
        return None, "no_id"

    out = media_dir / f"fb_vid_{vid}.mp4"
    if out.exists() and out.stat().st_size > _MIN_VIDEO_BYTES:
        return str(out), "cached"

    try:
        resp = req.get(url, timeout=timeout_ms)
    except Exception as exc:  # 네트워크/타임아웃 — URL 섞일 수 있어 type만
        _log.warning("영상 요청 실패 %s — %s", _redact(url), type(exc).__name__)
        _log.debug("영상 요청 예외 상세", exc_info=True)
        return None, "req_err"
    if resp.status != 200:
        _log.debug("영상 HTTP %s — skip %s", resp.status, _redact(url))
        return None, "http_err"
    clen = resp.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX_VIDEO:  # 사전 차단(메모리 적재 전)
        _log.warning("영상 크기상한 초과(content-length=%s) — skip %s", clen, _redact(url))
        return None, "too_large"
    try:
        body = resp.body()
    except Exception as exc:
        _log.warning("영상 본문 읽기 실패 %s — %s", _redact(url), type(exc).__name__)
        _log.debug("영상 본문 예외 상세", exc_info=True)
        return None, "req_err"
    if len(body) < _MIN_VIDEO_BYTES:
        _log.debug("영상 본문 과소(%dB) — skip %s", len(body), _redact(url))
        return None, "tiny"
    if len(body) > _MAX_VIDEO:  # content-length 누락 대비 사후 방어
        _log.warning("영상 크기상한 초과(%dB) — skip %s", len(body), _redact(url))
        return None, "too_large"
    if not _atomic_write(out, body):
        return None, "write_fail"
    return str(out), "fetched"


# ---- DOM 트리거/스캔 (best-effort 브라우저) ----

def trigger_video_load(page: Page) -> None:
    """비디오 lazy-load 유발 — 영역 스크롤 + 첫 video 클릭. best-effort."""
    try:
        page.evaluate(r"""() => {
            const sels = ['video', '[role="button"][aria-label*="재생"]',
                          '[role="button"][aria-label*="Play"]', '[data-video-id]'];
            for (const s of sels)
                document.querySelectorAll(s).forEach(el => {
                    try { el.scrollIntoView({block:'center'}); } catch(e){}
                });
        }""")
        page.wait_for_timeout(1500)
        try:
            v = page.locator("video").first
            if v.count() > 0:
                v.click(timeout=2000, force=True)
                page.wait_for_timeout(1500)
        except Exception as exc:
            _log.debug("video 클릭 트리거 실패(계속) — %s", type(exc).__name__)
    except Exception as exc:
        _log.debug("video lazy-load 트리거 실패 — %s", type(exc).__name__)


def scan_dom_videos(page: Page) -> list[str]:
    """DOM `<video src>` / `<source src>` 스캔(백업 경로)."""
    try:
        return page.evaluate(r"""() => {
            const out = [];
            document.querySelectorAll('video').forEach(v => {
                if (v.src) out.push(v.src);
                v.querySelectorAll('source').forEach(s => { if (s.src) out.push(s.src); });
            });
            return [...new Set(out)];
        }""") or []
    except Exception as exc:
        _log.debug("DOM video 스캔 실패 — %s", type(exc).__name__)
        return []


def enrich_post_videos(ctx: BrowserContext, permalink: str, *,
                       media_dir: str | Path | None = None) -> dict:
    """permalink 1건 → 영상 보강 결과 dict.

    네트워크 캡처(.mp4/.m4s, fbcdn)와 DOM 스캔을 합쳐 video id로 dedup,
    media_dir 지정 시 즉시 다운로드. 캡처 URL은 DOM보다 우선(라벨 network_capture).
    반환: {permalink, video_urls, local_videos, video_label, video_count}.
    """
    url = resolve_target(permalink)  # facebook.com 외 거부
    media: Path | None = None
    if media_dir is not None:
        media = Path(media_dir)
        media.mkdir(parents=True, exist_ok=True)

    page = ctx.new_page()
    captured: list[str] = []

    def on_request(req) -> None:  # noqa: ANN001 (playwright Request)
        u = getattr(req, "url", "")
        if _is_fb_video_cdn(u):
            captured.append(u)

    page.on("request", on_request)
    dom_videos: list[str] = []
    _log.info("영상 보강 시작 — 페이지 로딩·네트워크 캡처 %s", _redact(url))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector('div[role="article"]', timeout=8000)
        except Exception:
            _log.debug("article 미로딩(계속) %s", _redact(url))
        page.wait_for_timeout(800)
        trigger_video_load(page)
        page.wait_for_timeout(2000)  # 비디오 로드 시간 확보
        dom_videos = scan_dom_videos(page)
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception as exc:
            _log.debug("listener 제거 실패 — %s", type(exc).__name__)
        try:
            page.close()
        except Exception as exc:
            _log.debug("page.close 실패 — %s", type(exc).__name__)

    # 캡처(네트워크) 우선 → DOM 백업. id 기준 dedup.
    video_urls = dedup_by_video_id(captured + dom_videos)
    captured_ids = {video_id_from_url(u) for u in captured}
    label = "none"
    if video_urls:
        label = "network_capture" if any(video_id_from_url(u) in captured_ids for u in video_urls) else "dom_scan"

    local_videos: list[str] = []
    if media is not None and video_urls:
        ok = fail = 0
        for u in video_urls:
            sp, status = download_video(ctx.request, u, media)
            if sp:
                local_videos.append(sp)
                ok += 1
            else:
                fail += 1
        if fail:
            _log.warning("영상 %d/%d 다운로드 실패 %s", fail, ok + fail, _redact(url))

    if not video_urls:
        # 영상 없는 포스트일 수도, lazy-load 실패/로그인월/DOM변경일 수도 — 조용한 빈 반환 금지
        _log.info("영상 0건 — 영상 없는 포스트이거나 lazy-load 미발생/접근차단일 수 있음 %s",
                  _redact(url))
    _log.info("영상 보강 완료 urls=%d local=%d label=%s %s",
              len(video_urls), len(local_videos), label, _redact(url))
    return {
        "permalink": permalink,
        "video_urls": video_urls,
        "local_videos": local_videos,
        "video_label": label,
        "video_count": len(video_urls),
    }
