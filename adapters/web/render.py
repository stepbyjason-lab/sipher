r"""
sipher-web Tier2 — Python playwright를 이용한 JS-render 승격.

`adapters/web/__init__.py`의 tier1(insane-search engine, 순수 코드/curl_cffi
경로)이 "SSR 껍데기 의심"으로 판정한 경우에만 호출된다(opt-in 승격, round-10
contract §3). engine 자체에도 Playwright 폴백이 있지만 그것은 Claude 세션의
Playwright **MCP** 도구에 의존해 무인(unattended) 배치에서 쓸 수 없다
(round-10 contract §0 spike 근거) — 이 모듈은 그와 무관하게 sipher가 직접
`playwright` **Python** 패키지(sync API)를 호출하는 별도 구현이다.

SSRF 가드 (round-10 Post-Review Fix, P0): round-10 독립 리뷰가 `js=True` 강제
경로로 engine의 SSRF 방어(사설/루프백/링크로컬/메타데이터 IP 차단)를 완전히
우회하는 결함을 라이브 재현했다(`.handoff/rounds/round-10-absorb-web-review.md`
P0). 이 모듈은 더 이상 "호출자가 이미 검증한 URL만 준다"는 가정에 기대지 않는다
— `render_js()` 자체가 진입 시점에 `adapters.web.engine.safety.classify_url()`
(engine의 SSRF 판정 로직, 재사용)로 URL을 재검증하고, `page.goto()` 이후에도
`page.url`(리다이렉트를 모두 따라간 최종 URL)을 동일하게 재검증한다. 즉 이
모듈이 tier1/tier2 어느 경로에서 호출되든 통과하는 **독립 SSRF 관문**이다 —
어댑터의 `parse_url` 실수나 tier1 우회 시도가 있어도 이 함수 자체가 최종
방어선이 된다.
"""
from __future__ import annotations

import logging

from .engine import safety as _safety

_log = logging.getLogger(__name__)

__all__ = ["render_js", "RenderError", "SSRFBlockedError", "is_available"]

_DEFAULT_TIMEOUT_S = 30


class RenderError(RuntimeError):
    """playwright 미설치·브라우저 launch 실패·네비게이션/타임아웃 오류."""


class SSRFBlockedError(RenderError):
    """URL(요청 시점 또는 리다이렉트 최종 도달지)이 사설/루프백/링크로컬/메타데이터
    IP거나 비 http(s) 스킴이라 차단됨. `reason` 속성에 engine.safety.classify_url()의
    차단 사유 문자열(`ip_blocked:...` / `resolves_internal:...` / `scheme:...` 등)이
    담긴다 — 호출자가 이 예외를 다른 렌더 실패와 구분해 "failed"/"ssrf_blocked"
    라벨을 붙일 수 있도록."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _check_ssrf(url: str, *, stage: str) -> None:
    """engine의 SSRF 판정 로직을 재사용해 url을 검증. 차단되면 SSRFBlockedError."""
    allow_private = _safety.allow_private_default()
    ok, reason = _safety.classify_url(url, allow_private)
    if not ok:
        raise SSRFBlockedError(
            f"playwright SSRF 차단[{stage}] — 사설/루프백/링크로컬/메타데이터 IP 또는 "
            f"비 http(s) 스킴({reason}): {url!r}",
            reason=reason,
        )


def is_available() -> bool:
    """playwright 패키지 import 가능 여부만 확인(브라우저 바이너리 존재까지는
    확인하지 않음 — 실제 launch 시점에 RenderError로 드러남)."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def render_js(url: str, *, timeout: int = _DEFAULT_TIMEOUT_S) -> str:
    """URL을 headless Chromium으로 열어 JS 실행 후 렌더된 HTML을 반환한다.

    SSRF 가드(round-10 Post-Review Fix, P0 — 이 함수가 호출자와 무관한 독립
    관문): 3중으로 검증한다.
    1. 진입 시점에 요청 URL 자체를 `engine.safety.classify_url()`로 검증
       (`_check_ssrf(url, stage="entry")`) — 호출자가 어떤 경로로 왔든(tier1
       실패 후 승격이든 js=True 강제 진입이든) 사설 IP·비 http(s) 스킴이면
       여기서 즉시 `SSRFBlockedError`.
    2. 브라우저 내 모든 요청(메인 프레임 네비게이션 + 리다이렉트 각 hop +
       서브리소스)을 `page.route("**/*", ...)` 핸들러로 가로채 매번
       재검증한다 — 302 등으로 사설 IP에 도달하는 hop을 브라우저가 실제로
       요청을 보내기 전에 차단한다(engine의 `transport.py`가 curl_cffi
       레벨에서 하는 매-hop 재검증과 동형 방어를 playwright 레벨에서 재현).
    3. `page.goto()` 완료 후 `page.url`(모든 리다이렉트를 따라간 최종 URL)을
       다시 검증한다 — route 핸들러가 어떤 이유로든 못 잡은 경로가 있어도
       최종 방어선이 한 번 더 확인한다.

    - `sync_playwright()` 컨텍스트 매니저로 브라우저/컨텍스트/페이지를 모두
      with 블록 안에서 생성·정리한다(리소스 누수 방지 — 실패 시에도 반드시
      close된다).
    - `page.goto(url, wait_until="networkidle")`로 네트워크가 잠잠해질 때까지
      대기한 뒤 `page.content()`(DOM 직렬화 HTML, 하이드레이션 이후 상태)를
      반환한다.
    - 실패(브라우저 미설치, launch 실패, 타임아웃, 네비게이션 오류, SSRF 차단)는
      전부 `RenderError`(SSRF는 그 서브클래스 `SSRFBlockedError`)로 정직하게
      전파한다 — 빈 문자열을 성공처럼 반환하지 않는다(round 공통 정직 라벨 원칙).
      SSRF로 라우트 핸들러가 요청을 중단시키면 playwright는 그 프레임 네비게이션을
      일반 네비게이션 오류로 보고하므로, 그 경우도 `_ssrf_hit`에 사유를 기록해두고
      `PlaywrightError`/`PlaywrightTimeoutError` catch 블록에서 우선적으로
      `SSRFBlockedError`를 올린다(일반 렌더 실패로 뭉개지지 않도록).
    """
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
    except ImportError as e:
        raise RenderError(f"playwright 미설치 — `pip install playwright` 및 `playwright install chromium` 필요: {e}") from e

    # 1) 진입 시점 검증 — 호출자가 어디서 왔든 여기가 최종 관문.
    _check_ssrf(url, stage="entry")

    timeout_ms = max(int(timeout), 1) * 1000
    ssrf_hit: SSRFBlockedError | None = None

    def _route_handler(route) -> None:  # noqa: ANN001 — playwright Route 타입, 지연 임포트 경계라 타입힌트 생략
        nonlocal ssrf_hit
        req_url = route.request.url
        allow_private = _safety.allow_private_default()
        ok, reason = _safety.classify_url(req_url, allow_private)
        if not ok:
            if ssrf_hit is None:
                ssrf_hit = SSRFBlockedError(
                    f"playwright SSRF 차단[route] — 사설/루프백/링크로컬/메타데이터 "
                    f"IP 또는 비 http(s) 스킴({reason}): {req_url!r}",
                    reason=reason,
                )
            route.abort()
            return
        route.continue_()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                try:
                    page = context.new_page()
                    try:
                        # 2) 매 요청(리다이렉트 각 hop 포함) route 가로채기 검증.
                        page.route("**/*", _route_handler)
                        try:
                            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                        except (PlaywrightError, PlaywrightTimeoutError):
                            if ssrf_hit is not None:
                                raise ssrf_hit
                            raise
                        if ssrf_hit is not None:
                            # 메인 네비게이션은 아니었지만 서브리소스가 SSRF를
                            # 시도했다 — 결과를 신뢰하지 않고 정직하게 실패 처리.
                            raise ssrf_hit
                        # 3) 최종 도달 URL(모든 리다이렉트 반영) 재검증.
                        _check_ssrf(page.url, stage="final")
                        return page.content()
                    finally:
                        page.close()
                finally:
                    context.close()
            finally:
                browser.close()
    except SSRFBlockedError:
        raise
    except PlaywrightTimeoutError as e:
        raise RenderError(f"playwright 타임아웃({timeout}s): {e}") from e
    except PlaywrightError as e:
        raise RenderError(f"playwright 렌더 실패: {e}") from e
    except Exception as e:  # noqa: BLE001 — 브라우저 바이너리 부재 등 예상 밖 오류도 정직 전파
        raise RenderError(f"playwright 예상 밖 오류[{type(e).__name__}]: {e}") from e
