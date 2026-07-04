r"""
sipher-web 어댑터 — 독립 도구(패키지). 임의 http(s) 아티클 URL을 **범용 폴백**으로
받아 본문 텍스트를 추출하는 2-tier 어댑터(round-10 contract §①②).

공개 API: `fetch(url) -> 정규화 JSON dict` (sipher 라우터/단독 CLI 공용).
sipher 내부(core.*)를 import 하지 않는 깨끗한 경계(threads/instagram/tiktok과
동일 원칙) — `adapters.web.engine`(vendored 서브패키지)만 내부 의존.

정규화 스키마: { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }
설계: docs/00-overview.md. round-10 contract(.handoff/rounds/round-10-absorb-web-contract.md §①②).

## 2-tier 설계

- **Tier1**: `adapters.web.engine`(vendored insane-search engine, `_SOURCE.md` 참조)을
  Python import로 호출(`enable_playwright=False` — engine 내장 Playwright MCP 폴백은
  절대 트리거하지 않음). curl_cffi 기반 WAF 프로파일 그리드가 정적 HTML을 확보한다.
- **Tier2**: Tier1 결과가 "SSR 껍데기 의심"(§_looks_like_ssr_shell)이면
  `adapters.web.render.render_js`(Python playwright, headless Chromium)로 승격해
  JS 실행 후 DOM을 재추출한다. `js="auto"`(기본)/`js=False`(tier1 고정)/
  `js=True`(강제 승격) 3-way opt.

보안(round-10 Post-Review Fix, P0 — 독립 리뷰가 `js=True` 강제 경로에서 engine의
SSRF 방어가 완전히 우회됨을 라이브 재현, `.handoff/rounds/round-10-absorb-web-review.md`
참조): engine 자체의 SSRF 방어(private/loopback/link-local/reserved/metadata IP
차단 + 리다이렉트 매 hop 재검증, `engine/safety.py`)가 tier1(js=auto/false)에서는
1차 방어선으로 기능하지만, **그것만으로는 충분하지 않다** — tier2로 승격하는
`js=True` 강제 경로가 engine의 실패(`engine_ok=False`, SSRF 차단 포함)를 무시하고
그대로 playwright에 URL을 넘겼기 때문이다. 이제는 3중 방어를 어느 경로든 전부
통과해야 한다:

1. `parse_url()`이 스킴(http/https만)·호스트 존재 1차 검증에 더해
   `engine.safety.classify_url()`(engine의 SSRF 판정 로직 재사용)로 사설/루프백/
   링크로컬/메타데이터 IP 여부를 **어댑터 레벨에서 독립적으로** 재검증한다 —
   engine 호출 전에 걸리므로 engine 경로가 어떻게 바뀌어도 이 가드는 무관하게 선다.
2. `fetch()`의 tier2 승격 판정이 engine의 SSRF 신호(`engine_result.trace`의
   `ssrf_blocked:`/`ssrf_redirect_blocked:` 사유)를 존중한다 — `js=True`여도
   tier1이 SSRF로 실패했다면 즉시 `content_label="ssrf_blocked"`로 실패 처리하고
   playwright를 아예 호출하지 않는다.
3. `render.py`의 `render_js()` 자체가 독립 SSRF 관문이다(진입 시 재검증 + 매 요청
   route 가로채기 + 최종 URL 재검증, `render.py` docstring 참조) — 어댑터의
   `parse_url`/승격 판정에 구멍이 있어도 이 함수가 마지막 방어선이 된다.

즉 "engine을 1차 방어선으로 신뢰"라는 원래 설계는 유지하되, 그 신뢰가 `js=True`
강제 경로에서 깨지지 않도록 어댑터·render.py 양쪽에 **독립 가드**를 추가로 둔다.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit

from .engine import fetch as _engine_fetch
from .engine import safety as _safety
from .render import RenderError, SSRFBlockedError, is_available as _playwright_available, render_js

__all__ = ["fetch", "parse_url", "normalize"]

_log = logging.getLogger(__name__)

JsMode = Literal["auto", True, False]
ContentLabel = Literal["ok", "ssr_shell_only", "js_rendered", "failed", "ssrf_blocked"]

_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_TIMEOUT_S = 25
_DEFAULT_JS_TIMEOUT_S = 30

# "SSR 껍데기 의심" 휴리스틱(round-10 contract §3 — 보수적으로 OR 조건, 오탐(과다
# 승격)을 누락(SPA를 tier1로만 처리)보다 선호).
_SSR_SHELL_MIN_CHARS = 200
_SPA_MARKERS = re.compile(
    r"__NEXT_DATA__|__NUXT__|id=[\"']root[\"']|JavaScript is required|"
    r"enable\s+JavaScript|please enable javascript",
    re.I,
)


class _TextExtractor(HTMLParser):
    """표준 라이브러리 html.parser 기반 최소 본문 추출기.

    <script>/<style>/<noscript> 내부 텍스트는 본문에서 제외한다(외부 의존 추가 없이
    표준 라이브러리만으로 태그 스트립 — round-10 contract §2). 정교한 리더뷰(readability)
    알고리즘이 아니라 "태그를 걷어낸 순수 텍스트"만 제공하는 최소 구현이다.
    """

    _SKIP_TAGS = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def get_text(self) -> str:
        return "\n".join(self._chunks)


def strip_html(html: str) -> str:
    """raw HTML → 본문 텍스트(태그·script·style 제거). 파싱 실패 시 빈 문자열
    (예외를 올리지 않는다 — 이 함수는 이미 확보된 HTML에서 텍스트만 뽑는 순수
    변환이라 실패해도 호출자가 진행할 수 있어야 한다)."""
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception as e:  # noqa: BLE001 — 깨진 HTML도 최대한 살린 텍스트를 반환
        _log.warning("web: HTML 파싱 중 오류(부분 결과 사용): %s", e)
    return parser.get_text()


def _looks_like_ssr_shell(*, body_text: str, verdict: str, raw_html: str) -> bool:
    """Tier1 결과가 JS 없이는 본문이 비어 보이는 SSR 껍데기인지 보수적으로 판정.

    셋 중 하나라도 해당하면 True(OR 조건 — round-10 contract §3):
    1. 태그 스트립 텍스트가 `_SSR_SHELL_MIN_CHARS`자 미만
    2. engine verdict가 "weak_ok"(강한 성공이 아님)
    3. raw HTML에 SPA 마커(__NEXT_DATA__/__NUXT__/root div/노스크립트 경고) 존재
    """
    if len(body_text.strip()) < _SSR_SHELL_MIN_CHARS:
        return True
    if verdict == "weak_ok":
        return True
    if _SPA_MARKERS.search(raw_html or ""):
        return True
    return False


def _engine_ssrf_reason(engine_result) -> str | None:  # noqa: ANN001 — engine.FetchResult, 지연 임포트 경계라 타입힌트 생략
    """engine_result.trace를 훑어 SSRF 차단 사유(`ssrf_blocked:...` /
    `ssrf_redirect_blocked:...`)가 있으면 그 문자열을, 없으면 None을 반환한다.

    round-10 Post-Review Fix(P0): engine의 `transport.py`(`SessionPool.request`/
    `_fetch_following`)는 최초 URL과 모든 리다이렉트 hop을 `safety.classify_url()`로
    검증해 실패 시 `Attempt.error`에 `ssrf_blocked:<reason>` 또는
    `ssrf_redirect_blocked:<reason>`을 남긴다. `js=True` 강제 승격이 tier1의
    이 신호를 무시하지 않도록, `fetch()`가 승격을 결정하기 전에 이 헬퍼로 신호
    유무를 먼저 확인한다."""
    for attempt in getattr(engine_result, "trace", None) or []:
        err = getattr(attempt, "error", None) or ""
        if err.startswith("ssrf_blocked:") or err.startswith("ssrf_redirect_blocked:"):
            return err
    return None


def parse_url(url: str) -> str:
    """http(s) URL만 허용(SSRF 방어 — round-10 Post-Review Fix 이후 어댑터 레벨
    독립 가드로 강화). 스킴이 http/https가 아니거나 호스트가 없거나, 사설/루프백/
    링크로컬/예약/멀티캐스트/메타데이터 IP(또는 그런 IP로 DNS 해석되는 호스트)면
    ValueError. 통과 시 원본 문자열 그대로 반환(engine이 자체적으로 canonical
    처리를 하므로 threads/instagram처럼 별도 canonical 재구성을 하지 않는다 —
    tiktok 어댑터의 "URL 자체가 입력 단위" 패턴과 동일).

    이 함수의 IP 검사는 `engine.safety.classify_url()`(engine이 curl_cffi 요청·
    리다이렉트 hop마다 쓰는 동일 판정 로직)을 그대로 재사용한다 — 새 판정 로직을
    중복 구현하지 않고, engine 경로를 타지 않는 호출(예: tier2 render_js 강제
    진입)에서도 어댑터 자체가 독립적으로 같은 기준을 적용하기 위함이다."""
    if not isinstance(url, str):
        raise ValueError("URL은 문자열이어야 합니다")
    s = url.strip()
    if not s:
        raise ValueError("빈 URL")
    if len(s) > 4096:
        raise ValueError("URL이 너무 깁니다")
    parts = urlsplit(s)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(f"http(s) URL만 지원합니다(스킴: {parts.scheme or '(없음)'!r})")
    if not parts.hostname:
        raise ValueError("URL에 호스트가 없습니다")
    allow_private = _safety.allow_private_default()
    ok, reason = _safety.classify_url(s, allow_private)
    if not ok:
        raise ValueError(f"SSRF 방어에 의해 차단된 URL입니다({reason}): {s!r}")
    return s


def fetch(
    url: str,
    *,
    js: JsMode = "auto",
    media_dir: str | None = None,  # noqa: ARG001 — 이번 스코프는 미디어 다운로드 없음(계약 §비목표), 시그니처만 공통 유지
    timeout: int = _DEFAULT_TIMEOUT_S,
) -> dict:
    """임의 http(s) 아티클 URL → 정규화 JSON dict.

    - Tier1: `adapters.web.engine.fetch(url, enable_playwright=False, timeout=timeout)`
      로 정적 HTML 확보(curl_cffi WAF 그리드, engine 내장 SSRF 방어).
    - Tier1 결과가 SSR 껍데기 의심이고 `js != False`(기본 "auto") 또는 `js is True`
      (강제)면 Tier2(`adapters.web.render.render_js`)로 승격.
    - `media_dir`은 이번 스코프에서 미사용(비목표 — 계약 §비목표, 시그니처만 다른
      어댑터와 공통 형태 유지해 라우터 kwargs 통과를 단순하게 둠). 실제로 전달돼도
      무시된다.

    SSRF 가드(round-10 Post-Review Fix, P0): `parse_url()`이 이미 어댑터 레벨
    독립 검사를 통과시켰어도, tier1(engine)이 리다이렉트 hop에서 사설 IP를
    발견해 실패했을 수 있다(`ssrf_blocked:`/`ssrf_redirect_blocked:` trace).
    `js=True` 강제 승격이라도 이 신호가 있으면 tier2(playwright)를 아예 호출하지
    않고 `content_label="ssrf_blocked"`로 즉시 실패 처리한다 — "js=True는 tier1
    실패를 무시하고 마지막 기회를 준다"는 취지는 콘텐츠 파싱 실패·타임아웃 등
    비-보안 실패에만 적용되고, SSRF 실패는 별도로 취급한다(모듈 docstring §보안
    참조). `render_js()` 자체도 독립 SSRF 관문이라 이 단계를 건너뛰어도 최종
    적으로는 차단되지만, 여기서 조기에 막아 불필요한 브라우저 launch를 피한다.
    """
    canonical = parse_url(url)

    engine_result = _engine_fetch(canonical, enable_playwright=False, timeout=timeout)
    raw_html = engine_result.content or ""
    body_text = strip_html(raw_html)
    engine_ok = engine_result.ok
    ssrf_reason = _engine_ssrf_reason(engine_result)

    tier_used = 1
    js_error: str | None = None

    # tier1이 진짜 실패(engine_ok=False — SSRF 차단·타임아웃·전 시도 소진 등)한
    # 경우는 "SSR 껍데기"가 아니라 "실패"다 — 빈 body_text(<200자)가 항상
    # shell_suspected=True를 유발하므로, engine_ok가 False일 때 shell 판정을
    # 그대로 라벨에 쓰면 SSRF 차단조차 "js_shell"로 오분류된다(회귀 버그였음).
    # 두 실패 사유를 명확히 분리한다: engine_ok=False → "failed"(tier2로도 못
    # 살아나면 그대로), engine_ok=True인데 내용이 얕음 → "ssr_shell_only".
    shell_suspected = _looks_like_ssr_shell(
        body_text=body_text, verdict=engine_result.verdict, raw_html=raw_html
    )
    should_promote = (js is True) or (js == "auto" and engine_ok and shell_suspected)

    if js is True and not engine_ok:
        # 강제 렌더(js=True)는 tier1이 완전히 실패했어도 시도한다 — 사용자가
        # 명시적으로 요청한 경로이므로 시간 낭비를 감수하고 마지막 기회를 준다.
        # 단, SSRF로 실패한 경우는 예외(바로 아래에서 should_promote를 되돌림)
        # — "마지막 기회"는 콘텐츠/네트워크 실패에만 해당하지 보안 차단에는
        # 해당하지 않는다.
        should_promote = True

    if ssrf_reason is not None:
        # tier1이 SSRF로 실패했다면 js=True든 auto든 tier2 승격 자체를 금지한다
        # — engine의 SSRF 판정을 신뢰하는 한, 이미 차단된 URL을 playwright로
        # 다시 열어보는 시도 자체가 우회 경로가 된다.
        should_promote = False
        js_error = f"tier2 승격 거부 — tier1 SSRF 차단 신호 존중: {ssrf_reason}"
        _log.warning("web: %s", js_error)

    if should_promote:
        if not _playwright_available():
            js_error = "playwright 미설치 — tier2 승격 skip"
            _log.info("web: %s", js_error)
        else:
            try:
                rendered_html = render_js(canonical, timeout=_DEFAULT_JS_TIMEOUT_S)
                rendered_text = strip_html(rendered_html)
                if len(rendered_text.strip()) > len(body_text.strip()):
                    body_text = rendered_text
                    raw_html = rendered_html
                    tier_used = 2
                    engine_ok = True  # tier2가 살려냈으면 최종적으로는 성공
                else:
                    # 렌더링해도 개선이 없으면 tier1 결과를 유지(더 긴 쪽 채택 —
                    # 렌더 실패로 빈 페이지가 나오는 경우까지 방어).
                    pass
            except SSRFBlockedError as e:
                # render.py 자체의 독립 SSRF 관문이 차단한 경우 — "js_rendered"로
                # 위장하지 않고 명시적으로 ssrf_blocked 라벨을 붙인다(정직 라벨
                # 원칙, round-10 Post-Review Fix).
                ssrf_reason = e.reason
                js_error = str(e)
                _log.warning("web: tier2 SSRF 차단(render.py 독립 관문): %s", e)
            except RenderError as e:
                js_error = str(e)
                _log.warning("web: tier2 렌더 실패 — tier1 결과 유지: %s", e)

    if ssrf_reason is not None:
        content_label: ContentLabel = "ssrf_blocked"
        body_text = ""
        raw_html = ""
        tier_used = 1
        engine_ok = False
    elif tier_used == 2:
        content_label = "js_rendered"
    elif not engine_ok:
        content_label = "failed"
    elif shell_suspected:
        content_label = "ssr_shell_only"
    else:
        content_label = "ok"

    return normalize(
        source=url,
        canonical_url=canonical,
        body_text=body_text,
        tier_used=tier_used,
        content_label=content_label,
        engine_ok=engine_result.ok if ssrf_reason is None else False,
        engine_verdict=engine_result.verdict,
        final_url=engine_result.final_url,
        js_error=js_error,
    )


def normalize(
    *,
    source: str,
    canonical_url: str,
    body_text: str,
    tier_used: int,
    content_label: ContentLabel,
    engine_ok: bool,
    engine_verdict: str,
    final_url: str,
    js_error: str | None,
) -> dict:
    """정규화 스키마 조립. 공개 API. `meta`에 tier/verdict/final_url/content_label을
    정직하게 기록한다(round-10 contract §1 — "정직 라벨" 원칙). 웹 아티클은 댓글
    개념이 없으므로 `comments=[]` 고정, 미디어 다운로드는 이번 스코프 밖이라
    `media_paths=[]` 고정(둘 다 후속 인리치 단계 대상 아님)."""
    return {
        "source": source,
        "platform": "web",
        "body_text": body_text,
        "comments": [],
        "ocr_text": [],
        "transcript": None,
        "media_paths": [],
        "meta": {
            "tier": tier_used,
            "verdict": engine_verdict,
            "final_url": final_url or canonical_url,
            "content_label": content_label,
            "engine_ok": engine_ok,
            "js_error": js_error,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
    }
