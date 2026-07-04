r"""
sipher-fb 이미지 풀사이즈 보강 — 자작 수집 도구 fb_image_refetch 골격 이식.

★ 공개 대체재가 없는 핵심 IP: FB 포스트의 라이트박스/포토뷰어를 우회해
  (1) CDN `stp=` 쿼리 제거로 원본 해상도 회수, (2) photo viewer(fbid) + album set URL을
  순회해 그리드에 "+N" 으로 숨겨진 추가 사진까지 회수한다.

설계 변경점 (배치 CLI → 라이브러리):
- 자작 수집 도구는 posts_pw.jsonl 읽고 progress/resume/쿠키관리하는 독립 배치 스크립트였다.
  여기서는 **인증된 BrowserContext 주입형 함수**로 축약 — 세션/쿠키/큐/진행파일은
  호출 측(auth.py + T5 fetch/cli) 책임. 이 모듈은 "permalink 1건 → 풀사이즈 이미지" 변환만.
- 하드코딩 경로(ARCHIVE/MEDIA_DIR)·jsonl I/O·argparse 제거.
- scrape.py의 검증된 헬퍼(_redact·_ALLOWED_CDN_HOST·_atomic_write·_img_filename·_MAX_IMG)
  재사용(DRY) — 동일 dedup 네임스페이스(fb_<md5(base)>.ext)라 scrape가 받은 썸네일을
  같은 파일에 풀사이즈로 upgrade 한다.

견고성/보안 가드 (멀티렌즈 9패턴 — scrape.py와 동일 기준):
1. 입력검증: permalink는 facebook.com 호스트만(임의 goto 차단). resolve_target 재사용.
2. _redact: 모든 로그에서 서명 쿼리 제거.
3. 크기상한(content-length 사전 + body 사후, _MAX_IMG).
4. 원자적 쓰기(_atomic_write) — upgrade 시에도 부분파일 방지.
5. 호스트 화이트리스트(SSRF): stp 제거 변형 URL도 _ALLOWED_CDN_HOST 재검사.
6. 로그레벨: 운영이슈=warning, best-effort 순회 실패=debug, 예외=type만(+exc_info debug).
7. per-photo try/except — 한 사진 실패가 전체 중단 안 함.
8. 정규식 fail-fast(_strip_stp·_FBID_RE) + DOM evaluate 우선.
9. 정직 라벨 fullsize_label: viewer(가장확실) > largest_cdn(stp제거/라이트박스) > article_thumbnail.
"""
from __future__ import annotations

import logging
import re
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from .scrape import (
    _ALLOWED_CDN_HOST,
    _MAX_IMG,
    _atomic_write,
    _img_filename,
    _redact,
    resolve_target,
)

if TYPE_CHECKING:
    from playwright.sync_api import APIRequestContext, BrowserContext, Page

_log = logging.getLogger(__name__)

_FBID_RE = re.compile(r"[?&]fbid=(\d+)")
_SET_RE = re.compile(r"[?&]set=([^&#]+)")
# stp= 세그먼트만 제거(원본 해상도). 선두 구분자 보존(?stp= → ?, &stp= → 삭제).
# fail-fast 단순 치환(ReDoS 회피).
_STP_RE = re.compile(r"([?&])stp=[^&]*")

_MIN_BYTES = 200          # 1x1/플레이스홀더 컷
_GOOD_AREA = 1200 * 900   # 이 이상이면 캐시 그대로(재다운로드 생략)

# 정직 라벨 우선순위(클수록 신뢰)
_LABEL_RANK = {"none": 0, "article_thumbnail": 1, "largest_cdn": 2, "fullsize_viewer": 3}


def _strip_stp(url: str) -> str:
    """FB CDN URL의 stp= 쿼리 제거 → 원본 사이즈 응답 유도. 빈 쿼리/꼬리 정리."""
    if not url:
        return url
    out = _STP_RE.sub(lambda m: "?" if m.group(1) == "?" else "", url)
    return out.replace("?&", "?").rstrip("?&")


def _img_dimensions(data: bytes) -> tuple[int, int] | None:
    """JPEG/PNG 헤더에서 (width, height) 추출. 실패/미지원 포맷이면 None.

    경계 검사로 잘린 헤더에 IndexError/struct.error가 새지 않게 한다(fail-soft).
    """
    try:
        if data[:3] == b"\xff\xd8\xff":  # JPEG
            i = 2
            n = len(data)
            while i < n:
                while i < n and data[i] != 0xFF:
                    i += 1
                while i < n and data[i] == 0xFF:
                    i += 1
                if i >= n:
                    return None
                marker = data[i]
                i += 1
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    if i + 7 > n:
                        return None
                    h = struct.unpack(">H", data[i + 3:i + 5])[0]
                    w = struct.unpack(">H", data[i + 5:i + 7])[0]
                    return w, h
                if i + 2 > n:
                    return None
                seglen = struct.unpack(">H", data[i:i + 2])[0]
                if seglen <= 0:
                    return None
                i += seglen
        elif data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w = struct.unpack(">I", data[16:20])[0]
            h = struct.unpack(">I", data[20:24])[0]
            return w, h
    except (struct.error, IndexError):
        return None
    return None


def _area(data: bytes) -> int:
    d = _img_dimensions(data[:64 * 1024])
    return d[0] * d[1] if d else 0


def download_fullsize(req: APIRequestContext, url: str, media_dir: Path,
                      *, timeout_ms: int = 30000) -> tuple[str | None, str]:
    """풀사이즈 우선 다운로드 → (로컬경로 or None, status).

    원본 url과 stp= 제거 변형을 모두 시도해 **면적이 더 큰 응답**을 채택(upgrade-only:
    기존 파일이 충분히 크면 재다운로드 생략, 작으면 더 큰 쪽으로 덮어씀).
    두 변형 모두 호스트 화이트리스트(SSRF)·크기상한·원자적쓰기 적용.

    status ∈ {cached, fetched, upgraded, kept_existing, skip_host, http_err,
              req_err, too_small, too_large, all_fail, write_fail}
    """
    if not _ALLOWED_CDN_HOST.match(url):
        _log.debug("풀사이즈 호스트 비허용 — skip: %s", _redact(url))
        return None, "skip_host"

    out = media_dir / _img_filename(url)
    existing_area = 0
    if out.exists() and out.stat().st_size > _MIN_BYTES:
        try:
            existing_area = _area(out.read_bytes()[:64 * 1024])
        except OSError as exc:
            _log.debug("기존 파일 읽기 실패 %s — %s", out.name, type(exc).__name__)
        if existing_area >= _GOOD_AREA:
            return str(out), "cached"  # 이미 충분히 큼

    candidates = [url]
    stripped = _strip_stp(url)
    if stripped != url and _ALLOWED_CDN_HOST.match(stripped):  # 변형도 SSRF 재검사
        candidates.append(stripped)

    best: tuple[bytes, int] | None = None
    saw_response = False
    for cand in candidates:
        try:
            resp = req.get(cand, timeout=timeout_ms)
        except Exception as exc:  # Playwright 네트워크/타임아웃 — URL 섞일 수 있어 type만
            _log.debug("풀사이즈 요청 실패 %s — %s", _redact(cand), type(exc).__name__)
            continue
        saw_response = True  # 서버 응답 수신(상태 무관) — req_err(전송실패)과 all_fail(응답O·미사용) 구분
        if resp.status != 200:
            _log.debug("풀사이즈 HTTP %s — skip %s", resp.status, _redact(cand))
            continue
        clen = resp.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > _MAX_IMG:
            _log.warning("풀사이즈 크기상한 초과(content-length=%s) — skip %s", clen, _redact(cand))
            continue
        try:
            body = resp.body()
        except Exception as exc:
            _log.debug("풀사이즈 본문 읽기 실패 %s — %s", _redact(cand), type(exc).__name__)
            continue
        if len(body) < _MIN_BYTES:
            continue
        if len(body) > _MAX_IMG:
            _log.warning("풀사이즈 크기상한 초과(%dB) — skip %s", len(body), _redact(cand))
            continue
        area = _area(body)
        if best is None or area > best[1]:
            best = (body, area)

    if best is None:
        return (str(out), "kept_existing") if existing_area else (None, "all_fail" if saw_response else "req_err")
    if existing_area and best[1] <= existing_area:
        return str(out), "kept_existing"  # 새 응답이 기존보다 안 큼
    if not _atomic_write(out, best[0]):
        return None, "write_fail"
    return str(out), ("upgraded" if existing_area else "fetched")


# ---- DOM 수집 (best-effort 브라우저 순회) ----

def click_see_more(page: Page, max_clicks: int = 5) -> None:
    """본문 '더 보기'/'See more' 토글 클릭(보이는 만큼). best-effort."""
    sel = ('div[role="button"]:has-text("더 보기"), '
           'div[role="button"]:has-text("See more")')
    for _ in range(max_clicks):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=2000)
                page.wait_for_timeout(400)
            else:
                return
        except Exception:  # 토글 없음/가려짐 — 정상 종료
            return


def extract_post_text(page: Page) -> str:
    """더보기 클릭 후 가장 긴 article 본문 추출. 실패 시 빈 문자열."""
    try:
        return page.evaluate(r"""() => {
            let best = ''; let bestLen = 0;
            document.querySelectorAll('div[role="article"]').forEach(a => {
                const t = a.innerText || '';
                if (t.length > bestLen) { bestLen = t.length; best = t; }
            });
            return best.slice(0, 5000);
        }""") or ""
    except Exception as exc:
        _log.debug("본문 추출 실패 — %s", type(exc).__name__)
        return ""


def _owner_token(resolved_url: str) -> str | None:
    """리졸브된 permalink에서 글 주인 식별자(username 또는 numeric page/profile id) 추출.

    포커스 글 article 매칭용. FB permalink 페이지는 포커스 글 외 타인 글(친구 글·관련글)도
    각각 div[role=article]로 렌더하므로, 글 주인과 작성자 byline이 일치하는 article에만
    스코프해야 과수집(타인 사진 혼입)을 막는다.
    """
    if not resolved_url:
        return None
    m = re.search(r"facebook\.com/([^/?#]+)/(?:posts|videos)/", resolved_url, re.I)
    if m:
        seg = m.group(1)
        if seg.lower() not in {"share", "story.php", "permalink.php", "photo", "watch", "media"}:
            return seg
    m = re.search(r"[?&]id=(\d+)", resolved_url)  # profile.php?id= / story.php?...id=
    return m.group(1) if m else None


# 포커스 글 article을 작성자 byline(h2/h3/h4 헤더 링크)과 owner 토큰 일치로 선택하는 JS 조각.
# 매칭 실패 시 전체 article 폴백(과수집 가능 → 호출측 경고). ownerToken 없으면 폴백.
_FOCUS_SCOPE_JS = (
    "const arts=[...document.querySelectorAll('div[role=\"article\"]')];"
    "const authorHref=(a)=>{const h=a.querySelector('h2 a[href],h3 a[href],h4 a[href]');"
    "return (h&&h.getAttribute('href'))||'';};"
    "let focus=null;"
    "if(ownerToken)focus=arts.find(a=>{const h=authorHref(a);"
    "return h.includes('/'+ownerToken)||h.includes('id='+ownerToken);});"
    "const scope=focus?[focus]:(arts.length?arts:[document]);"
)


def collect_article_images(page: Page, *, owner_token: str | None = None) -> list[str]:
    """포커스 글 article DOM 안의 fbcdn 이미지(>100px). viewer 실패 시 백업/단일사진용."""
    try:
        return page.evaluate(
            "(ownerToken) => {" + _FOCUS_SCOPE_JS + r"""
            const out = [];
            scope.forEach(a => a.querySelectorAll('img').forEach(im => {
                if (im.src && im.src.includes('fbcdn') &&
                    (im.naturalWidth||im.width||0) > 100) out.push(im.src);
            }));
            return [...new Set(out)];
        }""", owner_token) or []
    except Exception as exc:
        _log.debug("article 이미지 수집 실패 — %s", type(exc).__name__)
        return []


def _extract_fbids(page: Page) -> list[str]:
    """현재 페이지의 fbid= 링크 추출(중복 제거)."""
    try:
        return page.evaluate(r"""() => {
            const out = [];
            document.querySelectorAll('a[href*="fbid="]').forEach(a => {
                const m = (a.href||'').match(/[?&]fbid=(\d+)/);
                if (m) out.push(m[1]);
            });
            return [...new Set(out)];
        }""") or []
    except Exception as exc:
        _log.debug("fbid 추출 실패 — %s", type(exc).__name__)
        return []


def collect_photo_viewer_urls(page: Page, ctx: BrowserContext,
                              *, owner_token: str | None = None,
                              base_url: str = "https://www.facebook.com",
                              max_photos: int = 80, deep: bool = False) -> list[str]:
    """★핵심: 포커스 글(작성자=owner_token) article의 photo viewer(fbid)를 모아
    각 viewer 페이지에서 풀사이즈 img를 추출. deep=True면 앨범 set 전체 확장.
    raw HTTP로는 못 잡음(FB가 JS로 늦게 주입). 정확하지만 사진당 ~3-5초.
    실패는 best-effort로 debug 로그 후 진행 — 전체를 죽이지 않는다."""
    photo_links: list[dict] = []
    seen_fbid: set[str] = set()
    try:
        # ★fbid 추출을 포커스 글 article로 한정 — permalink 페이지엔 타인 글(친구 글·관련글)도
        # 각각 div[role=article]로 렌더되므로 전체 긁으면 타인 사진 과수집(실측: 2장→40장→3장).
        res = page.evaluate(
            "(ownerToken) => {" + _FOCUS_SCOPE_JS + r"""
            const seen = new Set(); const out = [];
            scope.forEach(root => root.querySelectorAll('a[href*="fbid="]').forEach(a => {
                const href = a.href || '';
                const m = href.match(/[?&]fbid=(\d+)/);
                if (!m) return;
                const fbid = m[1];
                if (seen.has(fbid)) return;
                seen.add(fbid);
                const sm = href.match(/[?&]set=([^&#]+)/);
                out.push({fbid: fbid, set: sm ? sm[1] : '', href: href});
            }));
            return {matched: !!focus, links: out};
        }""", owner_token)
        raw = (res or {}).get("links", []) if isinstance(res, dict) else (res or [])
        if isinstance(res, dict) and owner_token and not res.get("matched"):
            _log.warning("포커스 글 article 식별 실패(owner=%s) — 전체 article 수집(타인 사진 과수집 가능)",
                         owner_token)
    except Exception as exc:
        _log.debug("photo viewer 링크 추출 실패 — %s", type(exc).__name__)
        raw = []
    for pl in raw:
        fb = pl.get("fbid")
        if fb and fb not in seen_fbid:
            seen_fbid.add(fb)
            photo_links.append(pl)

    if not photo_links:
        return []

    first_set = photo_links[0].get("set", "")
    viewer = ctx.new_page()
    full_urls: list[str] = []
    seen_path: set[str] = set()
    try:
        # 1) album set URL 방문 → 앨범 전체 fbid 노출. ★기본 off(deep) — 앨범 전체는
        #    이 포스트 밖 사진까지 포함해 과수집. 단일 포스트는 article 한정으로 충분.
        if deep and first_set:
            try:
                viewer.goto(f"{base_url}/media/set/?set={first_set}",
                            wait_until="domcontentloaded", timeout=25000)
                viewer.wait_for_timeout(2000)
                for fb in _extract_fbids(viewer):
                    if fb not in seen_fbid:
                        seen_fbid.add(fb)
                        photo_links.append({"fbid": fb, "set": first_set, "href": ""})
            except Exception as exc:
                _log.debug("set URL 방문 실패 — %s", type(exc).__name__)

        # 2) 각 fbid viewer 페이지 → 풀사이즈 img(면적 최대) 1장
        if len(photo_links) > max_photos:  # 무언 절단 금지 — 상한 가시화
            _log.warning("사진 %d장 중 상한 %d장만 회수(max_photos)", len(photo_links), max_photos)
        n_targets = min(len(photo_links), max_photos)
        _log.info("photo viewer %d장 순회 시작(사진당 수초 소요) %s", n_targets, _redact(base_url))
        for i, pl in enumerate(photo_links[:max_photos], 1):
            if i == 1 or i % 5 == 0 or i == n_targets:
                _log.info("  photo viewer 진행 %d/%d", i, n_targets)
            href = pl.get("href") or ""
            if href.startswith("/"):
                viewer_url = base_url + href
            elif href.startswith("http"):
                viewer_url = href
            else:
                s = pl.get("set", "")
                viewer_url = f"{base_url}/photo?fbid={pl.get('fbid')}" + (f"&set={s}" if s else "")
            if not _is_allowed_target(viewer_url):  # facebook.com만 navigate(SSRF/오염 차단)
                _log.debug("viewer 호스트 비허용 — skip %s", _redact(viewer_url))
                continue
            try:
                viewer.goto(viewer_url, wait_until="domcontentloaded", timeout=25000)
                viewer.wait_for_timeout(1500)
                imgs = viewer.evaluate(r"""() => {
                    const out = [];
                    document.querySelectorAll('img').forEach(im => {
                        if (im.src && im.src.includes('fbcdn') &&
                            (im.naturalWidth||im.width||0) > 300)
                            out.push({src: im.src,
                                      area:(im.naturalWidth||0)*(im.naturalHeight||0)});
                    });
                    return out;
                }""") or []
            except Exception as exc:
                _log.debug("viewer 페이지 처리 실패 — %s", type(exc).__name__)
                continue
            if not imgs:
                continue
            imgs.sort(key=lambda x: -x.get("area", 0))
            chosen = imgs[0]["src"]
            p = _redact(chosen)
            if p in seen_path:
                continue
            seen_path.add(p)
            full_urls.append(chosen)
    finally:
        try:
            viewer.close()
        except Exception as exc:
            _log.debug("viewer page.close 실패 — %s", type(exc).__name__)
    return full_urls


def _is_allowed_target(url: str) -> bool:
    from .scrape import _ALLOWED_TARGET_HOST
    return bool(_ALLOWED_TARGET_HOST.match(url))


def enrich_post_images(ctx: BrowserContext, permalink: str, *,
                       media_dir: str | Path | None = None,
                       max_photos: int = 80, deep: bool = False) -> dict:
    """permalink 1건 → 풀사이즈 이미지 보강 결과 dict.

    인증된 ctx(auth.authenticated_context)로 permalink 재방문 → viewer 풀사이즈 회수
    → article 백업 → 우선순위 dedup → media_dir 지정 시 다운로드(upgrade-only).
    deep=True면 앨범 set 전체 확장(과수집 위험, 옵트인). 기본은 article 한정.
    반환: {permalink, text, image_urls, local_images, fullsize_label, photo_count}.
    """
    url = resolve_target(permalink)  # facebook.com 외 거부(입력검증/SSRF)
    media: Path | None = None
    if media_dir is not None:
        media = Path(media_dir)
        media.mkdir(parents=True, exist_ok=True)

    page = ctx.new_page()
    label = "none"
    text = ""
    image_urls: list[str] = []
    _log.info("이미지 보강 시작 — 페이지 로딩 %s", _redact(url))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector('div[role="article"]', timeout=8000)
        except Exception:
            _log.debug("article 미로딩(계속) %s", _redact(url))
        page.wait_for_timeout(900)
        # 리졸브된 URL(share→실 permalink)에서 글 주인 추출 → 포커스 글 article만 스코프
        owner = _owner_token(page.url)
        _log.info("포커스 글 주인=%s (포커스 글 article에만 한정 수집)", owner or "미상(전체폴백)")
        click_see_more(page)
        text = extract_post_text(page)

        viewer_urls = collect_photo_viewer_urls(page, ctx, owner_token=owner,
                                                max_photos=max_photos, deep=deep)
        article_urls = collect_article_images(page, owner_token=owner)
        if viewer_urls:
            label = "fullsize_viewer"
        elif article_urls:
            label = "article_thumbnail"

        seen_path: set[str] = set()
        for u in (viewer_urls + article_urls):  # viewer(풀사이즈) 우선
            p = _redact(u)
            if p in seen_path:
                continue
            seen_path.add(p)
            image_urls.append(u)
    finally:
        try:
            page.close()
        except Exception as exc:
            _log.debug("page.close 실패 — %s", type(exc).__name__)

    local_images: list[str] = []
    if media is not None and image_urls:
        ok = fail = 0
        for u in image_urls:
            sp, status = download_fullsize(ctx.request, u, media)
            if sp:
                local_images.append(sp)
                ok += 1
                if status in ("fetched", "upgraded"):  # stp 제거로 원본 회수 성공
                    label = "largest_cdn" if _LABEL_RANK[label] < _LABEL_RANK["largest_cdn"] else label
            else:
                fail += 1
        if fail:
            _log.warning("풀사이즈 %d/%d 다운로드 실패 %s", fail, ok + fail, _redact(url))

    if not image_urls:
        _log.warning("이미지 0건 — 사진 없는 포스트이거나 로그인월/접근차단/FB DOM 변경일 수 있음 %s",
                     _redact(url))
    _log.info("이미지 보강 완료 urls=%d local=%d label=%s %s",
              len(image_urls), len(local_images), label, _redact(url))
    return {
        "permalink": permalink,
        "text": text,
        "image_urls": image_urls,
        "local_images": local_images,
        "fullsize_label": label,
        "photo_count": len(image_urls),
    }
