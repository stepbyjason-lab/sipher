r"""
sipher-fb 포스트 수집 — 자작 수집 도구 fb_scrape_playwright 골격 이식.

변경점(설계 docs/00-overview §4·§8):
- 하드코딩 개인 아카이브 경로 제거 → media_dir 인자화.
- 인물 특정 프로필 하드코딩 → 임의 FB 프로필/페이지 URL 입력.
- 내부 도구 posts_pw.jsonl 포맷 → 중간 post dict 리스트(T5 __init__.normalize가 정규화).
- 인증은 호출 측이 auth.authenticated_context로 연 BrowserContext를 주입(이 모듈은 세션 미관리).

핵심 알고리즘 보존(자작 수집 도구에서 이식):
- scroll + `div[role='article']` evaluate로 permalink·본문·이미지·영상 URL 수집.
- 발견 즉시 인증 `context.request.get`으로 다운로드 — FB CDN URL은 1~2h 내 만료된다.
- 영상 URL은 기록만(T4 refetch_video가 별도 다운로드). 풀사이즈 보강은 T3 refetch_images.

견고성/보안 가드(멀티렌즈 수렴 — Naver scrape.py 참조 구현의 9개 패턴):
1. 입력 검증: 프로필/페이지 ID 정규식 + 길이상한, 입력 URL은 facebook.com 호스트만(임의 goto 차단).
2. _redact(url): 모든 로그에서 쿼리(서명 oh/oe) 제거 — 불변식 2.
3. 다운로드 크기 상한(content-length 사전 + body 사후 2중) — 메모리/디스크 고갈 차단.
4. 원자적 쓰기(tmp→replace) + 실패 시 tmp 정리 — 부분 파일 방지. 재실행 안전(해시 파일 skip).
5. 다운로드/이미지 호스트 화이트리스트(SSRF) — fbcdn.net 계열만.
6. 에러 로그 레벨: 운영이슈(디스크/네트워크)=warning, 흔한 실패(4xx/과소)=debug, 예외=type만+exc_info(debug).
7. 포스트별 try/except(한 포스트 실패가 전체 중단 안 함) + 정중 지연(scroll 사이).
8. evaluate JS는 DOM 기반(정규식 파싱 최소) — likes/comments만 fail-fast 정규식 + html.unescape.
9. 정직 라벨: article DOM 이미지는 썸네일급 → image_label="article_cdn"(풀사이즈 회수는 T3). 허위 '원본' 금지.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import APIRequestContext, BrowserContext, Page

_log = logging.getLogger(__name__)

# 입력 검증 (URL 경로 주입 / 임의 호스트 goto 차단). 길이 상한으로 로그폭탄/과장 입력 방지.
# FB username: 영문/숫자/'.' (페이지·프로필), 레거시 profile.php는 URL 형태로만 허용.
_VALID_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.]{0,63}")
_ALLOWED_TARGET_HOST = re.compile(r"^https?://(?:[\w-]+\.)*facebook\.com/", re.I)
_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp)", re.I)

# CDN 호스트 화이트리스트 (SSRF 차단). 이미지=fbcdn 계열만, 영상도 동일 계열.
_ALLOWED_CDN_HOST = re.compile(r"^https?://(?:[\w-]+\.)*fbcdn\.net/", re.I)

# 좋아요/댓글 수 — fail-fast 정규식(ReDoS 회피: 숫자 그룹 직결)
_LIKES_RE = re.compile(r"(?:좋아요|Likes?)[^\d]{0,8}(\d[\d,]*)")
_COMMENTS_RE = re.compile(r"(?:댓글|Comments?)[^\d]{0,8}(\d[\d,]*)")

# 응답 크기 상한 (자원 고갈 차단)
_MAX_IMG = 50 << 20  # 50 MB (FB 사진 단건 상한 — 넉넉)

# 댓글 본문 후행 메타(시간·좋아요·답글달기·원본보기·좋아요수) 분리용 — fail-fast 라인 매칭
# (de-risk spike 05_extract_comments_final.py 실측: "...메시지\n15시간\n좋아요\n답글 달기\n
#  원본 보기(영어)\n9" 패턴, ko-KR locale 기준. en-US UI 대비 영문 상대시간도 포함 —
#  계약 Task-lens: "다국어(한국어/영어 UI) 양쪽에서 동작하는가"). 시간 표기(N시간/N분/N일/
#  N초/어제/방금, N hour(s)/N min(ute)(s)/N day(s)/N sec(ond)(s)/yesterday/just now)·
# 순수 숫자(좋아요수)·고정 UI 문구를 후행에서부터 제거한다.
_COMMENT_META_LINE_RE = re.compile(
    r"^(?:\d+\s*(?:초|분|시간|일|주|개월|년)|어제|방금|"
    r"\d+\s*(?:s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\b|"
    r"yesterday|just\s+now|"
    r"좋아요|Like|답글\s*달기|Reply|원본\s*보기\([^)]{0,20}\)|See\s+translation|"
    r"\d[\d,]{0,9})$", re.I)
# "답글 N개"/"답글 N개 모두 보기" 확장 버튼 텍스트(ko-KR + en-US 양쪽)
_REPLY_EXPAND_RE = re.compile(r"(?:답글\s*\d+개|\d+\s*repl(?:y|ies))", re.I)

# 댓글 확장(더보기 클릭) 보수적 상한 — 계약 §비목표(무한클릭 금지, 중첩 2단계 미만)
_MAX_EXPAND_CLICKS = 5
_EXPAND_CLICK_TIMEOUT_MS = 4000
_EXPAND_SETTLE_MS = 900

# article DOM에서 permalink·본문·이미지·영상을 한 번에 추출하는 평가 스크립트.
# (자작 수집 도구 evaluate 이식 — fbcdn 이미지 + width 필터, video src/source)
_COLLECT_JS = r"""() => {
    const out = [];
    const articles = document.querySelectorAll("div[role='article']");
    articles.forEach(a => {
        let permalink = null;
        a.querySelectorAll("a[href*='/posts/'], a[href*='/videos/'], a[href*='story_fbid=']").forEach(x => {
            if (!permalink && x.href) permalink = x.href;
        });
        const imgs = [];
        a.querySelectorAll("img").forEach(img => {
            if (img.src && img.src.includes("fbcdn") && img.width > 100) imgs.push(img.src);
        });
        const vids = [];
        a.querySelectorAll("video").forEach(v => {
            if (v.src) vids.push(v.src);
            v.querySelectorAll("source").forEach(s => { if (s.src) vids.push(s.src); });
        });
        const text = a.innerText ? a.innerText.slice(0, 2000) : "";
        out.push({permalink, images: imgs, videos: vids, text});
    });
    return out;
}"""

# 댓글 후보 article 수집 — de-risk spike(05_extract_comments_final.py) 실측 로직 이식.
# 본문 article(캡션 텍스트로 식별)을 기준점으로 **그 뒤에 오는** div[role='article']만
# 댓글 후보로 순회한다(§③: role=article이 본문·댓글 양쪽에 재사용되고, 뉴스피드 무관
# 포스트가 섞여 들어오므로 순수 인덱스 기반 구분은 오탐 위험 — 캡션 텍스트 매칭으로
# 본문 위치를 먼저 고정한 뒤 "그 이후"만 스캔).
# skeleton("읽어들이는 중...")은 텍스트/aria-label로 걸러 빈 댓글 항목이 안 섞이게 한다.
# 반환은 원시 텍스트(author_guess 포함 raw innerText)만 — 후행 메타 분리는 Python
# 쪽(_parse_comment_text)에서 pytest 가능하게 분리한다(계약 §검증계획 2).
_COLLECT_COMMENTS_JS = r"""(captionSnippet) => {
    const articles = Array.from(document.querySelectorAll("div[role='article']"));
    const captionGiven = !!captionSnippet;
    let bodyIdx = -1;
    if (captionGiven) {
        bodyIdx = articles.findIndex(a => (a.innerText || "").includes(captionSnippet));
    }
    const bodyMatched = bodyIdx !== -1;   // 캡션이 실제 article에 매칭됐는가(round-16: 저신뢰 신호)
    if (bodyIdx === -1) bodyIdx = 0;  // 캡션 매칭 실패 시 방어적 폴백(0번을 본문으로 가정)

    const candidates = articles.slice(bodyIdx + 1);
    const out = [];
    candidates.forEach((a, i) => {
        const ariaLabel = a.getAttribute("aria-label") || "";
        const status = a.getAttribute("role") === "status";
        const text = a.innerText || "";
        if (status || /읽어들이는\s*중|loading/i.test(ariaLabel) || /읽어들이는\s*중|loading/i.test(text)) {
            return;  // 로딩 skeleton — skip
        }
        if (!text.trim()) return;

        let profileHref = null;
        const link = a.querySelector("a[role='link'][href]");
        if (link && link.href) profileHref = link.href;

        out.push({raw_text: text.slice(0, 4000), profile_href: profileHref, index: i});
    });
    // round-16: bare array → 객체. body_matched/caption_given으로 idx0 폴백 저신뢰를 신호한다.
    return {candidates: out, body_matched: bodyMatched, caption_given: captionGiven};
}"""


def _decide_comments_label(*, raw_count: int, comment_count: int,
                           expand_hit_cap: bool, expand_interrupted: bool,
                           parse_fail: int, body_matched: bool,
                           caption_given: bool) -> str:
    """댓글 수집 결과 → comments_label(round-16 순수 함수, Playwright 없이 pytest 가능).

    값 집합: collected | partial | none | fetch_failed
    (login_required/not_collected은 호출측 __init__.py 책임).

    - `fetch_failed`: 후보 article은 있었으나 하나도 파싱 못함(추출 degrade — raw_count>0 & comment_count==0).
      evaluate 자체 예외도 호출측에서 이 값으로 반환한다. ('none'과 구분 = round-16 #4 목적)
    - `none`: 본문 이후 댓글 article이 0개(진짜 빈 상태 — raw_count==0).
    - `partial`: 1건 이상이나 불완전 신호 — 확장 상한/중단/일부 파싱실패, 또는 캡션이
      주어졌는데 본문 매칭 실패로 idx0 폴백(저신뢰, 뉴스피드 오탐 가능 — round-16 #5).
    - `collected`: 1건 이상 + 불완전 신호 없음 + (캡션 미지정 or 본문 매칭 성공).
    """
    if comment_count == 0:
        return "fetch_failed" if raw_count > 0 else "none"
    low_confidence_fallback = caption_given and not body_matched
    if expand_hit_cap or expand_interrupted or parse_fail or low_confidence_fallback:
        return "partial"
    return "collected"


def _redact(url: str) -> str:
    """로그용: 쿼리(oh/oe 서명·토큰) 제거 — 불변식 2(서명 URL 전체를 로그에 안 남김)."""
    return url.split("?", 1)[0] if url else url


def resolve_target(target: str) -> str:
    """프로필 ID 또는 FB URL → 정규 FB URL. facebook.com 외 호스트는 거부(임의 goto/SSRF 차단)."""
    if target.startswith(("http://", "https://")):
        if not _ALLOWED_TARGET_HOST.match(target):
            raise ValueError(f"facebook.com URL이 아닙니다: {_redact(target)!r}")
        return target
    if not _VALID_PROFILE.fullmatch(target):
        raise ValueError(f"유효하지 않은 프로필/페이지 ID: {target!r}")
    return f"https://www.facebook.com/{target}"


def _img_ext(url: str) -> str:
    m = _EXT_RE.search(_redact(url))
    if m:
        return "." + m.group(1).lower()
    _log.debug("확장자 추출 실패 — .jpg 가정: %s", _redact(url))
    return ".jpg"


def _img_filename(url: str) -> str:
    """쿼리 제거 base URL 해시 기반 파일명 — 재실행 시 동일 사진 dedup/skip."""
    base = _redact(url)
    h = hashlib.md5(base.encode("utf-8")).hexdigest()[:12]  # noqa: S324 (파일명 dedup용, 보안 해시 아님)
    return f"fb_{h}{_img_ext(url)}"


def _atomic_write(out: Path, data: bytes) -> bool:
    """tmp에 쓰고 rename — 중단 시 부분 파일이 최종 경로에 안 남게."""
    tmp = out.with_suffix(out.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(out)
        return True
    except OSError as exc:  # 디스크 풀/권한 — 네트워크 실패와 구분되게 warning(로컬 경로는 비밀 아님)
        _log.warning("파일 쓰기 실패 %s — %s: %s", out.name, type(exc).__name__, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError as ue:
            _log.debug("tmp 정리 실패 %s — %s", tmp.name, type(ue).__name__)
        return False


def download_image(req: APIRequestContext, url: str, media_dir: Path,
                   *, timeout_ms: int = 30000) -> str | None:
    """인증 컨텍스트로 이미지 1장 다운로드 → 로컬 경로(str) 또는 None.

    호스트 화이트리스트(SSRF) → 재실행 skip → 크기 상한(content-length 사전 + body 사후)
    → 원자적 쓰기. FB CDN URL은 단명하므로 발견 즉시 호출돼야 한다.

    ⚠️ Playwright APIResponse.body()는 스트리밍 미지원(전체를 메모리에 적재). content-length
    헤더가 있으면 body 받기 전 차단하지만, 헤더 누락 시엔 일단 메모리에 받은 뒤 사후 차단한다
    (Naver의 urllib 청크 스트리밍과 달리 한계). FB 사진은 단건이라 실무상 위험은 낮다.
    """
    if not _ALLOWED_CDN_HOST.match(url):
        _log.debug("이미지 호스트 비허용 — skip: %s", _redact(url))
        return None
    out = media_dir / _img_filename(url)
    if out.exists() and out.stat().st_size > 100:
        return str(out)  # 이미 받음(재실행 안전)
    try:
        resp = req.get(url, timeout=timeout_ms)
    except Exception as exc:  # Playwright 네트워크/타임아웃 — 예외 메시지에 URL 섞일 수 있어 type만
        _log.warning("이미지 요청 실패 %s — %s", _redact(url), type(exc).__name__)
        _log.debug("이미지 요청 예외 상세", exc_info=True)
        return None
    if resp.status != 200:
        _log.debug("이미지 HTTP %s — skip %s", resp.status, _redact(url))
        return None
    clen = resp.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > _MAX_IMG:  # 사전 차단(body 받기 전)
        _log.warning("이미지 크기상한 초과(content-length=%s) — skip %s", clen, _redact(url))
        return None
    try:
        body = resp.body()
    except Exception as exc:
        _log.warning("이미지 본문 읽기 실패 %s — %s", _redact(url), type(exc).__name__)
        _log.debug("이미지 본문 예외 상세", exc_info=True)
        return None
    if len(body) < 100:  # 1x1 픽셀/에러 플레이스홀더
        _log.debug("이미지 본문 과소(%dB) — skip %s", len(body), _redact(url))
        return None
    if len(body) > _MAX_IMG:  # content-length 누락 대비 사후 방어
        _log.warning("이미지 크기상한 초과(%dB) — skip %s", len(body), _redact(url))
        return None
    return str(out) if _atomic_write(out, body) else None


def _parse_count(text: str, pattern: re.Pattern) -> int | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:  # 정규식이 매칭했으나 변환 실패 — 방어적
        return None


def _post_key(post: dict) -> str | None:
    """중복 판정 키 — permalink 우선, 없으면 본문 머리말 + 이미지 수(약한 dedup)."""
    permalink = post.get("permalink")
    if permalink:
        return _redact(permalink)
    text = (post.get("text") or "")[:80]
    if not text:
        return None  # permalink·본문 둘 다 없으면 빈 카드 — skip
    return text + "|" + str(len(post.get("images") or []))


def _parse_comment_text(raw_text: str) -> dict:
    """댓글 article raw innerText → {author, text, likes}. 순수 함수(Playwright 미의존, pytest 가능).

    de-risk spike 실측 패턴: 첫 줄=작성자명, 마지막 줄들에 시간/좋아요/답글달기/
    원본보기/좋아요수가 붙어 나온다(예: "작성자\\n메시지\\n15시간\\n좋아요\\n답글 달기\\n
    원본 보기(영어)\\n9"). 뒤에서부터 후행 메타 라인을 제거하고, 남은 중간 라인을 본문으로
    합친다. 좋아요수는 후행 메타 중 마지막 순수 숫자 라인으로 추정(있으면).
    본문이 비면(=메타만 남으면) 파싱 실패로 간주해 text=""(호출측이 skip 판단).

    round-14 M1 픽스(post-review): 순수 숫자 라인(`_COMMENT_META_LINE_RE`의 마지막 대안
    `\\d[\\d,]{0,9}`)은 "좋아요수" 전용 신호가 아니라 댓글 본문 자체가 짧은 숫자
    ("9", "50000")여도 매칭된다. 본문이 숫자 한 줄뿐인 케이스에서 그 줄까지
    좋아요로 pop해버리면 본문이 통째로 사라진다(데이터 손실). 이를 막기 위해 뒤에서부터
    이어지는 메타-매칭 구간(run)을 먼저 스캔해 그 **시작 경계**를 구조로 판정한다:
    진짜 메타 블록은 항상 비-숫자 UI 크롬 마커(시간/좋아요/답글달기/원본보기 등)로
    시작한다(예: "...메시지\\n15시간\\n좋아요\\n...\\n9" — 크롬 마커가 body 바로
    뒤에 붙고, 숫자(좋아요수)는 그 뒤에만 나온다). 그러므로 run을 body 쪽에서부터
    훑어 **첫 크롬 마커가 나오는 지점부터**만 메타로 인정하고, 그 앞의 순수 숫자
    라인(들)은 크롬 마커가 전혀 없거나 크롬 마커보다 body에 더 가까우면 본문으로
    되돌린다. 크롬 마커가 전혀 없는 run(숫자 라인만)은 메타 블록이 아니라 본문
    자체로 보고 pop하지 않는다.
    """
    lines = [ln.strip() for ln in (raw_text or "").split("\n") if ln.strip()]
    if not lines:
        return {"author": None, "text": "", "likes": None}

    author = lines[0]
    body_lines = lines[1:]

    # 1) 뒤에서부터 연속으로 _COMMENT_META_LINE_RE에 매칭되는 구간의 길이를 먼저 센다
    #    (아직 pop하지 않음 — 구조 판정 전용 lookahead).
    run_len = 0
    while run_len < len(body_lines) and _COMMENT_META_LINE_RE.match(body_lines[len(body_lines) - 1 - run_len]):
        run_len += 1

    if run_len == 0:
        return {"author": author or None, "text": "\n".join(body_lines).strip(), "likes": None}

    meta_run = body_lines[len(body_lines) - run_len:]  # body 쪽이 먼저, 끝(좋아요수)이 나중

    # 2) run 안에서 body 쪽부터 첫 비-숫자 크롬 마커의 위치를 찾는다. 그 앞(더 body에
    #    가까운 쪽)의 숫자 라인들은 메타가 아니라 본문으로 되돌린다.
    meta_start = None
    for idx, ln in enumerate(meta_run):
        if not ln.replace(",", "").isdigit():
            meta_start = idx
            break

    if meta_start is None:
        # run 전체가 숫자 라인뿐 — 크롬 마커가 전혀 없다. 진짜 메타 블록이 아니라
        # 본문(짧은 숫자형 댓글)일 가능성이 높으므로 pop하지 않는다.
        return {"author": author or None, "text": "\n".join(body_lines).strip(), "likes": None}

    real_meta = meta_run[meta_start:]  # 첫 크롬 마커부터 끝까지만 진짜 메타
    restored_prefix = meta_run[:meta_start]  # 크롬 마커 이전 숫자 라인 — 본문으로 복원

    # 3) 진짜 메타 구간만 pop. 좋아요수는 그 안의 순수 숫자 라인(뒤에서부터 첫 매칭)으로 추정.
    del body_lines[len(body_lines) - run_len:]
    body_lines.extend(restored_prefix)
    likes: int | None = None
    for ln in reversed(real_meta):
        if ln.replace(",", "").isdigit():
            try:
                likes = int(ln.replace(",", ""))
            except ValueError:  # 방어적(isdigit True인데 int 변환 실패는 사실상 불가)
                likes = None
            break

    text = "\n".join(body_lines).strip()
    return {"author": author or None, "text": text, "likes": likes}


def extract_comments(page: Page, caption_snippet: str | None = None, *,
                     max_expand: int = _MAX_EXPAND_CLICKS) -> tuple[list[dict], str]:
    """현재 page(permalink 방문 완료)에서 댓글[] + 라벨을 추출. permalink 미방문 시 결과 미정.

    caption_snippet: 본문 article 식별용 캡션 텍스트 일부(§③ — 순수 인덱스 기반 구분은
    뉴스피드 오염에 취약하므로 필수 권장, 없으면 0번 article을 본문으로 방어적 가정).
    "답글 N개" 확장 버튼을 최대 max_expand회만 클릭(무한클릭 금지, 계약 §비목표).
    반환: (comments 원시 리스트[{author,text,likes,profile_href}], label).
    label ∈ {"collected", "partial", "none", "fetch_failed"}(round-16) —
    login_required/not_collected은 호출측(__init__.py) 책임. 판정은 _decide_comments_label.

    round-14 M2 픽스(post-review): 확장 루프가 빠져나오는 경로는 세 갈래다 —
    (a) 버튼이 더 이상 없음(count==0, 자연 완료), (b) max_expand 상한 도달,
    (c) 클릭/탐색 중 예외로 강제 중단(오버레이·DOM 변경 등, 아직 버튼이 남아
    있을 수 있음). 이전 구현은 (a)와 (c)가 동일하게 `break`로만 처리돼
    `expand_hit_cap`(=조건 b 전용) 판정에서 구분이 안 됐다 — 강제중단(c)이어도
    parse_fail=0이면 라벨이 "collected"(완전 수집)로 오분류될 수 있었다.
    (c)를 별도 `expand_interrupted` 플래그로 구분해 (b)와 함께 "미완료" 신호로
    묶는다 — 자연완료(a)만 "완료"로 인정한다.
    """
    # 지연로딩 대비: "답글 N개" 확장을 보수적 상한 안에서 클릭 → DOM 갱신 대기.
    expand_hit_cap = False
    expand_interrupted = False  # 예외로 인한 강제 중단(미완료) — 자연완료(버튼 소진)와 구분
    try:
        clicked = 0
        for _ in range(max_expand):
            buttons = page.locator("div[role='button'], span[role='button']").filter(
                has_text=_REPLY_EXPAND_RE)
            count = buttons.count()
            if count == 0:
                break  # 자연 완료 — 더 이상 확장할 버튼이 없음
            try:
                buttons.first.click(timeout=_EXPAND_CLICK_TIMEOUT_MS)
                clicked += 1
                page.wait_for_timeout(_EXPAND_SETTLE_MS)
            except Exception as exc:  # 클릭 대상 사라짐/오버레이 등 — 강제 중단, 버튼이 남아있을 수 있음
                _log.debug("답글 확장 클릭 실패(중단) — %s", type(exc).__name__)
                expand_interrupted = True
                break
        if clicked >= max_expand:
            expand_hit_cap = True
    except Exception as exc:  # locator 자체 실패 — 확장 미완료로 간주(치명적은 아니나 정직 라벨 유지)
        _log.debug("답글 확장 탐색 실패(계속) — %s", type(exc).__name__)
        expand_interrupted = True

    try:
        raw = page.evaluate(_COLLECT_COMMENTS_JS, caption_snippet)
    except Exception as exc:
        _log.warning("댓글 article 평가 실패 — %s", type(exc).__name__)
        return [], "fetch_failed"  # round-16: 추출 자체 실패 — 빈 상태("none")와 구분

    raw_candidates = raw.get("candidates") or []
    body_matched = bool(raw.get("body_matched"))
    caption_given = bool(raw.get("caption_given"))

    comments: list[dict] = []
    parse_fail = 0
    for cand in raw_candidates:
        parsed = _parse_comment_text(cand.get("raw_text") or "")
        if not parsed["text"]:
            parse_fail += 1
            continue
        href = cand.get("profile_href")
        comments.append({
            "id": _redact(href) if href else None,
            "author": parsed["author"],
            "text": parsed["text"],
            "likes": parsed["likes"],
            "reply_count": 0,  # 1단계까지만 확장(계약 §비목표) — 중첩 답글 수는 별도 미집계
            "media_paths": [],
            "profile_href": _redact(href) if href else None,
        })

    label = _decide_comments_label(
        raw_count=len(raw_candidates), comment_count=len(comments),
        expand_hit_cap=expand_hit_cap, expand_interrupted=expand_interrupted,
        parse_fail=parse_fail, body_matched=body_matched, caption_given=caption_given,
    )
    if caption_given and not body_matched and comments:
        _log.debug("본문 캡션 매칭 실패 — idx0 폴백으로 댓글 수집(저신뢰 partial) %d건", len(comments))
    return comments, label


def scrape_profile(ctx: BrowserContext, target: str, *,
                   media_dir: str | Path | None = None,
                   max_scrolls: int = 300, scroll_pause: float = 2.2,
                   settle_rounds: int = 3) -> list[dict]:
    """프로필/페이지 타임라인 → 중간 post dict 리스트.

    ctx는 호출 측이 auth.authenticated_context로 연 **인증된** BrowserContext.
    media_dir 지정 시 발견 즉시 이미지 다운로드(CDN 만료 회피), 미지정 시 URL만 수집.
    영상은 URL만 기록(T4). 개별 포스트 예외는 잡아 로그 후 continue.
    """
    url = resolve_target(target)
    media: Path | None = None
    if media_dir is not None:
        media = Path(media_dir)
        media.mkdir(parents=True, exist_ok=True)

    page = ctx.new_page()
    posts: list[dict] = []
    seen_keys: set[str] = set()
    seen_img: set[str] = set()
    seen_vid: set[str] = set()
    img_ok = img_fail = eval_fail = 0

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_selector("div[role='main']", timeout=15000)
        except Exception as e:
            # 로그인월/잘못된 URL — 명확히 실패(현재 URL은 redact)
            raise RuntimeError(
                f"main 컨테이너 미로딩 — 로그인월 또는 잘못된 URL일 수 있음 "
                f"(현재: {_redact(page.url)})"
            ) from e

        last_height = 0
        stagnant = 0
        for i in range(max_scrolls):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(scroll_pause)  # 정중함(불변식 4) + 지연 로딩 대기
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                stagnant += 1
                if stagnant >= settle_rounds:
                    _log.debug("타임라인 끝 도달 scroll=%d", i)
                    break
            else:
                stagnant = 0
            last_height = new_height

            try:
                raw_posts = page.evaluate(_COLLECT_JS)
            except Exception as e:  # 페이지 평가 실패(네비게이션 중 등) — 이번 라운드 skip
                eval_fail += 1
                _log.debug("article 평가 실패 scroll=%d — %s", i, type(e).__name__)
                continue

            for raw in raw_posts:
                key = _post_key(raw)
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)

                images = list(dict.fromkeys(raw.get("images") or []))  # 순서보존 dedup
                videos = list(dict.fromkeys(raw.get("videos") or []))
                local_images: list[str] = []
                if media is not None:
                    for iurl in images:
                        ibase = _redact(iurl)
                        if ibase in seen_img:
                            continue
                        seen_img.add(ibase)
                        saved = download_image(ctx.request, iurl, media)
                        if saved:
                            local_images.append(saved)
                            img_ok += 1
                        else:
                            img_fail += 1

                fresh_videos = [v for v in videos if _redact(v) not in seen_vid]
                seen_vid.update(_redact(v) for v in fresh_videos)

                text = html.unescape(raw.get("text") or "")
                posts.append({
                    "permalink": raw.get("permalink"),
                    "text": text,
                    "image_urls": images,
                    "video_urls": fresh_videos,
                    "local_images": local_images,
                    "image_label": "article_cdn" if local_images else "none",
                    "likes": _parse_count(text, _LIKES_RE),
                    "comments": _parse_count(text, _COMMENTS_RE),
                })

            if i % 10 == 0:
                _log.debug("scroll %d/%d posts=%d imgs=%d ok/%d fail vids=%d",
                           i + 1, max_scrolls, len(posts), img_ok, img_fail, len(seen_vid))
    finally:
        try:
            page.close()
        except Exception as e:
            _log.debug("page.close 실패 — %s", type(e).__name__)

    if img_fail:
        _log.warning("이미지 %d/%d 다운로드 실패 target=%s",
                     img_fail, img_ok + img_fail, _redact(url))
    if eval_fail:  # article 평가 실패가 누적되면 수집 누락 가능 — debug 합산을 가시화(P2)
        _log.warning("article 평가 %d회 실패(일부 라운드 수집 누락 가능) target=%s",
                     eval_fail, _redact(url))
    if not posts:
        # 0건은 빈 프로필일 수도, 로그인월/차단/DOM 변경일 수도 있다 — 조용한 빈 반환 금지(P1)
        _log.warning(
            "수집 0건 — 빈 프로필이거나 로그인월/접근차단/FB DOM 변경일 수 있음. "
            "target=%s (인증 세션·URL 확인)", _redact(url))
    _log.info("수집 완료 posts=%d imgs=%d vids=%d", len(posts), img_ok, len(seen_vid))
    return posts


def enrich_post_comments(ctx: BrowserContext, permalink: str, *,
                         caption_snippet: str | None = None,
                         max_expand: int = _MAX_EXPAND_CLICKS) -> dict:
    """permalink 1건 → 댓글 보강 결과 dict(round-14). enrich_post_images와 동일 골격.

    인증된 ctx로 permalink를 **재방문**(refetch_images/video와 별도 page — 기존 검증된
    경로 무변경, round-14 §제약)해 extract_comments()를 호출한다.
    caption_snippet 미지정 시 페이지 본문 텍스트 앞부분을 자동 사용(호출측이 이미 본문을
    안다면 넘겨서 재추출 생략 가능 — __init__.py가 img.get("text")를 전달).
    반환: {comments: list[dict], comments_label: str}.
    """
    url = resolve_target(permalink)  # facebook.com 외 거부(입력검증/SSRF) — extract_comments 진입 전 방어
    page = ctx.new_page()
    comments: list[dict] = []
    label = "none"
    _log.info("댓글 보강 시작 — 페이지 로딩 %s", _redact(url))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector('div[role="article"]', timeout=8000)
        except Exception:
            _log.debug("article 미로딩(댓글 보강 계속 시도) %s", _redact(url))
        page.wait_for_timeout(900)  # 지연로딩 정중 대기(scroll_pause와 동일 원칙)

        snippet = caption_snippet
        if not snippet:
            # refetch_images.extract_post_text와 동일 원리(가장 긴 article 본문)를
            # 로컬 evaluate로 인라인 — 순환 import 회피(refetch_images가 scrape를 import).
            try:
                longest = page.evaluate(r"""() => {
                    let best = ''; let bestLen = 0;
                    document.querySelectorAll('div[role="article"]').forEach(a => {
                        const t = a.innerText || '';
                        if (t.length > bestLen) { bestLen = t.length; best = t; }
                    });
                    return best.slice(0, 80);
                }""") or ""
                snippet = longest[:80] or None
            except Exception as exc:
                _log.debug("캡션 자동추출 실패(폴백 진행) — %s", type(exc).__name__)
                snippet = None

        comments, label = extract_comments(page, snippet, max_expand=max_expand)
    except Exception as exc:  # 페이지 로딩/네비게이션 실패 — 댓글 보강만 실패, 전체 fetch는 죽이지 않음
        _log.warning("댓글 보강 실패 %s — %s", _redact(url), type(exc).__name__)
        label = "fetch_failed"  # round-16: 로딩 실패 = 추출 실패, 빈 상태("none") 아님
    finally:
        try:
            page.close()
        except Exception as exc:
            _log.debug("page.close 실패 — %s", type(exc).__name__)

    _log.info("댓글 보강 완료 comments=%d label=%s", len(comments), label)
    return {"comments": comments, "comments_label": label}
