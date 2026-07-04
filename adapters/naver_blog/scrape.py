r"""
sipher-naver-blog 수집 — 자작 수집 도구 naver_blog_scrape 골격 이식.

변경점(설계 docs/00-overview §8):
- 본문/이미지는 **데스크톱 PostView**(blog.naver.com)로 가져온다 → postfiles/blogfiles 호스트
  (자작 수집 도구는 모바일 PostView라 mblogthumb w966 캡에 묶였음).
- 이미지 회수는 images.py 호스트별 분기(blogfiles=원본, postfiles=w966).
- 출력은 sipher 정규화 스키마(__init__.normalize 참조).

패키지 `naver_blog`의 모듈 — 정식 relative import 사용. CLI 실행: `python -m adapters.naver_blog.cli`.

견고성/보안 가드(멀티렌즈 수렴):
- 입력 검증: blog_id `[A-Za-z0-9_-]+`, log_no `\d+` (URL 경로 주입·path traversal 차단).
- 영상 URL 호스트 화이트리스트(SSRF 차단) — 이미지는 images.is_blog_image가 이미 가드.
- HTTP 응답 크기 상한(메모리/디스크 고갈 차단), 스트리밍 + 원자적 쓰기(부분 파일 방지).
- 정규식은 fail-fast(ReDoS 회피), HTML 엔티티 unescape.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import images

_log = logging.getLogger(__name__)

_UA_PC = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36"
)
_UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
)
_LIST_API = "https://m.blog.naver.com/api/blogs/{blog_id}/post-list"
_POSTVIEW_PC = "https://blog.naver.com/PostView.naver"

# 입력 검증 (URL 경로 주입 / path traversal 차단). 길이 상한으로 로그폭탄/과장 URL 방지.
_VALID_BLOG_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VALID_LOG_NO = re.compile(r"\d{1,20}")
_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp)(?:\?|$)", re.I)

# 응답 크기 상한 (자원 고갈 차단)
_MAX_HTML = 16 << 20   # 16 MB
_MAX_MEDIA = 300 << 20  # 300 MB
_CHUNK = 1 << 16

# HTML 안에서 blog 이미지 CDN URL을 찾는 패턴 (images._CDN_RE와 동일 호스트군)
_IMG_IN_HTML = re.compile(
    r'https?://(?:postfiles|blogfiles|blogthumb|mblogthumb)(?:-phinf)?'
    r'\.pstatic\.net/[^\s"\'<>\\]+',
    re.I,
)
_VIDEO_RE = re.compile(r'https?://[^\s"\'<>\\]+\.(?:mp4|m3u8)[^\s"\'<>\\]*', re.I)
# 영상 다운로드 허용 호스트 (SSRF 차단 — naver 계열만)
_ALLOWED_VIDEO_HOST = re.compile(
    r'^https?://(?:[\w-]+\.)*(?:pstatic\.net|naver\.com|navercorp\.com)/', re.I)

# og 메타: 속성 순서 무관 2-pass (property↔content 순서 가정 안 함)
_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_PROP = re.compile(r'property="([^"]*)"', re.I)
_ATTR_CONT = re.compile(r'content="([^"]*)"', re.I)
# SE3 본문 단락 — fail-fast 본문 매칭(ReDoS 회피: </p> 직전까지만)
_SE_PARA = re.compile(
    r'<p[^>]+class="[^"]*se-text-paragraph[^"]*"[^>]*>((?:(?!</p>).)*)</p>',
    re.I | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\f\v]+")
_NL = re.compile(r"\n{3,}")


def _valid_blog_id(blog_id: str) -> str:
    if not blog_id or not _VALID_BLOG_ID.fullmatch(blog_id):
        raise ValueError(f"유효하지 않은 blog_id: {blog_id!r}")
    return blog_id


def _valid_log_no(log_no: str) -> str:
    s = str(log_no)
    if not _VALID_LOG_NO.fullmatch(s):
        raise ValueError(f"유효하지 않은 logNo: {log_no!r}")
    return s


def _redact(url: str) -> str:
    """로그용: 쿼리(서명 파라미터) 제거 — 불변식 2(서명 URL 전체를 로그에 안 남김)."""
    return url.split("?", 1)[0]


def _http_get(url: str, *, mobile: bool, max_bytes: int,
              timeout: int = 30) -> tuple[int, bytes, bool]:
    """(status, body[≤max_bytes], truncated). 청크 스트리밍으로 max_bytes 초과분은 받지 않는다.
    truncated=True면 본문이 상한에서 잘린 것(다운스트림이 인지하도록 전파). 오류는 status=-1."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA_MOBILE if mobile else _UA_PC,
        "Referer": "https://blog.naver.com/",
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "ko,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            chunks: list[bytes] = []
            total = 0
            truncated = False
            while True:
                chunk = r.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:  # 상한 초과분은 버리고 절단(메모리 고갈 차단)
                    chunks.append(chunk[: max_bytes - (total - len(chunk))])
                    _log.warning("응답 상한(%dB) 초과 — 절단 %s", max_bytes, _redact(url))
                    truncated = True
                    break
                chunks.append(chunk)
            return r.status, b"".join(chunks), truncated  # r.status: HTTPResponse(3.0+)
    except urllib.error.HTTPError as e:
        _log.warning("HTTP %s %s", e.code, _redact(url))
        return e.code, b"", False
    except urllib.error.URLError as e:
        _log.warning("요청 네트워크 오류 %s — %s", _redact(url), type(e).__name__)
        _log.debug("네트워크 오류 상세 — %s", e.reason)  # reason은 인프라 정보 가능 → debug
        return -1, b"", False
    except Exception as exc:
        _log.warning("요청 예외 %s — %s", _redact(url), type(exc).__name__)
        _log.debug("요청 예외 상세", exc_info=True)
        return -1, b"", False


def _http_text(url: str, params: dict | None = None, mobile: bool = False,
               timeout: int = 30) -> tuple[int, str, bool]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    status, body, truncated = _http_get(url, mobile=mobile, max_bytes=_MAX_HTML,
                                        timeout=timeout)
    return status, body.decode("utf-8", errors="replace"), truncated


def fetch_post_list(blog_id: str, max_posts: int = 10000,
                    sleep: float = 0.4) -> list[dict]:
    """모바일 post-list API로 포스트 메타 목록 수집(페이지네이션)."""
    _valid_blog_id(blog_id)
    items: list[dict] = []
    page = 1
    while len(items) < max_posts:
        status, text, _ = _http_text(_LIST_API.format(blog_id=blog_id),
                                     params={"categoryNo": "0", "itemCount": "30",
                                             "page": str(page)}, mobile=True)
        if status != 200 or not text:
            _log.warning("post-list 조기 종료 blog=%s page=%d status=%s 누적=%d",
                         blog_id, page, status, len(items))
            break
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            _log.warning("post-list JSON 파싱 실패 blog=%s page=%d 누적=%d",
                         blog_id, page, len(items))
            break
        result_obj = parsed.get("result")
        if not isinstance(result_obj, dict):  # API 구조 변경 감지(무음 0건 종료 방지)
            _log.warning("post-list 응답 구조 예상 밖 blog=%s page=%d keys=%s",
                         blog_id, page, list(parsed.keys())[:5])
            break
        batch = result_obj.get("items", [])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 30:
            break
        page += 1
        time.sleep(sleep)
    return items[:max_posts]


def _extract_images(html_text: str) -> list[str]:
    """본문 HTML에서 blog 이미지 CDN URL 추출(base 기준 dedup, 호스트 화이트리스트)."""
    out, seen = [], set()
    for m in _IMG_IN_HTML.finditer(html_text):
        u = m.group(0)
        if not images.is_blog_image(u):
            continue
        base = u.split("?", 1)[0]
        if base in seen:
            continue
        seen.add(base)
        out.append(u)
    return out


def _extract_videos(html_text: str) -> list[str]:
    """영상 URL 추출 — naver 계열 호스트만(SSRF 차단), base 기준 dedup."""
    out, seen = [], set()
    for m in _VIDEO_RE.finditer(html_text):
        u = html.unescape(m.group(0))  # &amp; 등 인코딩된 URL 정규화
        if not _ALLOWED_VIDEO_HOST.match(u):
            _log.debug("영상 URL 호스트 비허용 — skip: %s", _redact(u))
            continue
        base = _redact(u)
        if base in seen:
            continue
        seen.add(base)
        out.append(u)
    return out


def _clean(fragment: str) -> str:
    """HTML 조각 → 태그 제거 후 엔티티 unescape + 공백 정규화.

    순서 주의: 태그를 **먼저** 제거하고 그 다음 unescape — 그래야 본문에 escape된
    `&lt;x&gt;`가 unescape 후 가짜 태그로 오인·제거되지 않는다.
    """
    text = html.unescape(_TAG.sub(" ", fragment))
    text = text.replace("\xa0", " ").replace(chr(0x200b), "")  # nbsp→space, zero-width 제거
    return _WS.sub(" ", text).strip()


def _og(html_text: str, prop: str) -> str | None:
    """og:<prop> content — 속성 순서 무관(2-pass)."""
    for m in _META_TAG.finditer(html_text):
        tag = m.group(0)
        pm = _ATTR_PROP.search(tag)
        if pm and pm.group(1).lower() == prop:
            cm = _ATTR_CONT.search(tag)
            if cm:
                return html.unescape(cm.group(1)).strip()
    return None


def _extract_title_text(html_text: str) -> tuple[str | None, str]:
    """og:title + 본문 텍스트. 단락(se-text-paragraph) 우선, 없으면 og:description 폴백."""
    title = _og(html_text, "og:title")
    paras = [p for p in (_clean(m.group(1)) for m in _SE_PARA.finditer(html_text)) if p]
    if paras:
        text = _NL.sub("\n\n", "\n".join(paras)).strip()
    else:  # 폴백: og:description(요약) — 비-SE3 구버전 본문 대체
        text = _og(html_text, "og:description") or ""
    return title, text


def scrape_post(blog_id: str, log_no: str, *, item: dict | None = None) -> dict:
    """포스트 1개 → 중간 dict(이미지 URL 포함, 다운로드 전). item=목록 메타(옵션)."""
    _valid_blog_id(blog_id)
    log_no = _valid_log_no(log_no)
    status, html_text, truncated = _http_text(
        _POSTVIEW_PC, params={"blogId": blog_id, "logNo": log_no})
    if status != 200 or not html_text:
        raise RuntimeError(f"PostView 실패 blog={blog_id} logNo={log_no} status={status}")
    title, text = _extract_title_text(html_text)
    item = item or {}
    return {
        "blog_id": blog_id,
        "log_no": log_no,
        "title": title or item.get("titleWithInspectMessage") or item.get("title"),
        "add_date": item.get("addDate"),
        "category": item.get("categoryName"),
        "comment_count": item.get("commentCount"),
        "read_count": item.get("readCount"),
        "like_count": item.get("sympathyCnt") or item.get("likeItCnt"),
        "url": f"https://blog.naver.com/{blog_id}/{log_no}",
        "body_text": text,
        "body_truncated": truncated,  # HTML 상한 절단 시 본문 일부 손실(다운스트림 인지용)
        "image_urls": _extract_images(html_text),
        "video_urls": _extract_videos(html_text),
    }


def download_media(post: dict, media_dir: str | Path) -> tuple[list[str], str]:
    """이미지(호스트별 회수)+영상 다운로드 → (media_paths, image_size_label).

    log_no는 파일명 stem이라 숫자 검증(path traversal 차단). 실패는 집계해 warning.
    """
    media = Path(media_dir)
    media.mkdir(parents=True, exist_ok=True)
    log_no = _valid_log_no(post["log_no"])
    paths: list[str] = []
    labels: list[str] = []

    img_urls = post.get("image_urls", [])
    img_fail = 0
    for i, url in enumerate(img_urls):
        data, label, _used = images.fetch_image(url)
        if data is None:
            _log.debug("이미지 회수 실패 %s img%02d — %s", log_no, i, label)
            img_fail += 1
            continue
        out = media / f"{log_no}_img{i:02d}{_img_ext(url)}"
        if _atomic_write(out, data):
            paths.append(str(out))
            labels.append(label)
        else:
            img_fail += 1
    if img_fail:
        _log.warning("이미지 %d/%d 회수 실패 logNo=%s", img_fail, len(img_urls), log_no)

    vid_urls = post.get("video_urls", [])
    vid_fail = 0
    for i, url in enumerate(vid_urls):
        out = media / f"{log_no}_vid{i:02d}{'.m3u8' if '.m3u8' in url else '.mp4'}"
        if _download_raw(url, out):
            paths.append(str(out))
        else:
            vid_fail += 1
    if vid_fail:
        _log.warning("영상 %d/%d 다운로드 실패 logNo=%s", vid_fail, len(vid_urls), log_no)

    return paths, _summarize_label(labels)


def _img_ext(url: str) -> str:
    m = _EXT_RE.search(url)
    if m:
        return "." + m.group(1).lower()
    _log.debug("확장자 추출 실패 — .jpg 가정: %s", _redact(url))
    return ".jpg"


# 성공 라벨만 집계되므로 실패 라벨은 불필요하나, 키 누락 시 0으로 안전 처리.
_LABEL_RANK = {"original": 3, "original_w3840": 3, "w966_ceiling": 2,
               "thumbnail_fallback": 1}


def _summarize_label(labels: list[str]) -> str:
    if not labels:
        return "none"
    return max(labels, key=lambda x: _LABEL_RANK.get(x, 0))


def _atomic_write(out: Path, data: bytes) -> bool:
    """tmp에 쓰고 rename — 중단 시 부분 파일이 최종 경로에 안 남게."""
    tmp = out.with_suffix(out.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(out)
        return True
    except Exception as exc:
        # 디스크 풀/권한 등 시스템 이슈 — 네트워크 실패와 구분되게 warning(로컬 경로는 비밀 아님)
        _log.warning("파일 쓰기 실패 %s — %s: %s", out.name, type(exc).__name__, exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception as ue:
            _log.debug("tmp 정리 실패 %s — %s", tmp.name, type(ue).__name__)
        return False


class _MediaTooLarge(Exception):
    """미디어가 _MAX_MEDIA 초과 — _download_raw 내부 신호(상한 시점에 이미 warning)."""


def _download_raw(url: str, out: Path, timeout: int = 60) -> bool:
    """스트리밍 다운로드(크기 상한) + 원자적 쓰기. 호스트 화이트리스트는 호출 전 보장."""
    if not _ALLOWED_VIDEO_HOST.match(url):  # 2중 방어(SSRF)
        _log.debug("다운로드 호스트 비허용 — skip: %s", _redact(url))
        return False
    req = urllib.request.Request(url, headers={"User-Agent": _UA_PC,
                                               "Referer": "https://blog.naver.com/"})
    tmp = out.with_suffix(out.suffix + ".tmp")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                _log.debug("미디어 HTTP %s — skip %s", r.status, _redact(url))
                return False
            total = 0
            with tmp.open("wb") as fh:
                while True:
                    chunk = r.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_MEDIA:
                        _log.warning("미디어 상한(%dB) 초과 — 중단 %s", _MAX_MEDIA, _redact(url))
                        raise _MediaTooLarge
                    fh.write(chunk)
        tmp.replace(out)
        return True
    except _MediaTooLarge:
        pass  # 이미 warning
    except urllib.error.HTTPError as exc:  # 4xx/5xx 영상 — 흔함, debug(노이즈 회피)
        _log.debug("미디어 HTTP %s — %s", exc.code, _redact(url))
    except OSError as exc:  # 네트워크(URLError)·디스크 풀/권한 — 운영 이슈는 가시화
        _log.warning("미디어 다운로드 오류 %s — %s", _redact(url), type(exc).__name__)
    except Exception as exc:  # 예상 못 한 오류(코드 버그) — 가시화
        _log.warning("미디어 다운로드 예외 %s — %s", _redact(url), type(exc).__name__)
        _log.debug("미디어 다운로드 예외 상세", exc_info=True)
    try:  # 실패 공통: 부분 tmp 정리
        tmp.unlink(missing_ok=True)
    except Exception as ue:
        _log.debug("tmp 정리 실패 %s — %s", tmp.name, type(ue).__name__)
    return False


def scrape_blog(blog_id: str, *, max_posts: int = 10000,
                media_dir: str | Path | None = None,
                sleep: float = 0.4) -> list[dict]:
    """블로그 전체(또는 max_posts) → 중간 post dict 리스트(+옵션 미디어 다운로드).

    개별 포스트 예외는 잡아 로그 후 continue — 한 포스트 실패가 전체 수집을 중단시키지 않는다.
    """
    _valid_blog_id(blog_id)
    posts = []
    for it in fetch_post_list(blog_id, max_posts=max_posts, sleep=sleep):
        log_no = it.get("logNo")
        if not log_no or not _VALID_LOG_NO.fullmatch(str(log_no)):
            _log.warning("logNo 이상/누락 — skip: %r", log_no)
            continue
        try:
            post = scrape_post(blog_id, str(log_no), item=it)
            if media_dir:
                post["media_paths"], post["image_size_label"] = download_media(post, media_dir)
        except ValueError as e:  # 데이터/검증 이상(코드 버그 가능) — error + 스택
            _log.error("포스트 데이터 이상 logNo=%s — %s", log_no, type(e).__name__,
                       exc_info=True)
            continue
        except Exception as e:  # 한 포스트 실패가 전체를 죽이지 않게(네트워크 등)
            _log.warning("포스트 건너뜀 logNo=%s — %s", log_no, type(e).__name__)
            _log.debug("포스트 건너뜀 상세", exc_info=True)
            continue
        if post.get("body_truncated"):  # 16MB 상한 절단 — 운영자 가시화(meta에만 두지 않음)
            _log.warning("본문 HTML 상한 절단(일부 손실) logNo=%s", log_no)
        posts.append(post)
        time.sleep(sleep)
    return posts
