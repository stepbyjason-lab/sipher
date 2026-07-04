r"""
sipher-fb 인증 — 3겹 전략 (persistent context 기본).

자작 수집 도구(비공개)는 ① 수동 cookies.txt(몇 주마다 export·갱신)만 썼다 — 일반 사용자 진입장벽 최대.
gallery-dl/yt-dlp(③ browser 쿠키 직접 읽기)·huzaifa-hb(④ 도구 브라우저 로그인 1회) 패턴을 도입해
DX를 교체. 우회 알고리즘은 내부 도구에서 이식, 인증 UX는 검증된 공개 패턴.

설계 근거: adapters/facebook/docs/01-security-intake.md §3.

  ④ persistent  — `launch_persistent_context(user_data_dir)`. `login()` 1회 후 세션 영구. 기본.
  ③ browser     — browser_cookie3로 설치 브라우저 쿠키 직접 읽기. ⚠️ Chrome v127+ 암호화 → firefox 우선.
  ① cookies_txt — Netscape 파일. headless/서버 fallback.

이 모듈은 호출 측이 연 `sync_playwright()` 안에서 **인증된 BrowserContext**를 만들어 돌려준다.

보안(불변식 2): 쿠키 값은 로그/예외 메시지에 절대 싣지 않는다. 반환되는 쿠키 컨테이너는
`_RedactedCookieList`로 감싸 repr/str에 값이 노출되지 않게 한다 — 호출 측이 실수로 print/log해도
세션 토큰이 새지 않는다.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Playwright
else:  # 런타임 placeholder — get_type_hints()가 NameError 없이 동작(playwright 선택 import 유지)
    Browser = BrowserContext = Playwright = object

_log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36"
)
DEFAULT_VIEWPORT = {"width": 1366, "height": 900}
DEFAULT_LOCALE = "ko-KR"
_SESSION_COOKIE = "c_user"  # 로그인 시 존재하는 FB 세션 식별 쿠키
# ③/① 모드에서 지원하는 브라우저 화이트리스트 (getattr dispatch 안전)
_ALLOWED_BROWSERS = {"firefox", "chrome", "chromium", "edge", "opera", "brave", "vivaldi"}


class AuthError(RuntimeError):
    """인증 실패 — 호출 측이 사용자에게 복구 안내를 보여줄 수 있게 메시지를 담는다.

    민감정보(쿠키 값·전체 경로·서명 URL)는 메시지에 넣지 않는다(불변식 2).
    """


class _RedactedCookieList(list):
    """Playwright add_cookies가 받는 list와 호환되지만 repr/str에 값이 안 보이는 컨테이너."""

    def __repr__(self) -> str:  # noqa: D401
        return f"<RedactedCookieList: {len(self)} cookies>"

    __str__ = __repr__


class _RedactedCookie(dict):
    """Playwright add_cookies가 읽는 dict와 동일하지만 repr/str에서 value를 마스킹.

    개별 원소를 print/log해도(예: `for c in cookies: print(c)`) 세션 토큰이 안 샌다.
    실제 값은 내부에 유지 → `c["value"]` 구독은 평문 반환(add_cookies가 소비).

    정책: **value만** 마스킹한다(불변식 2의 보호 대상 = 세션 토큰 = value). name/domain/path는
    비밀이 아니라 평문 표시. ⚠️ json.dumps/dict()/items()는 마스킹 우회 — 쿠키 객체를
    직렬화하지 말 것(add_cookies 소비 외 용도 없음).
    """

    def __repr__(self) -> str:  # noqa: D401
        inner = ", ".join(
            f"{k!r}: {'***' if k == 'value' else v!r}" for k, v in self.items()
        )
        return "{" + inner + "}"

    __str__ = __repr__


# ── 로그용 redaction (불변식 2: 쿠키 값·서명 URL 전체를 로그에 남기지 않는다) ──

def redact_url(url: str) -> str:
    """서명된 CDN URL을 로그에 남길 때 쿼리(서명·토큰)를 잘라낸다."""
    if not url:
        return url
    base = url.split("?", 1)[0]
    return base + (" ?…redacted…" if "?" in url else "")


# ── ④ persistent context (기본) ──

def open_persistent_context(p: Playwright, profile_dir: str | Path, *,
                            headless: bool = True, locale: str = DEFAULT_LOCALE,
                            ua: str = DEFAULT_UA) -> BrowserContext:
    """user_data_dir 기반 영구 컨텍스트. 세션이 프로필 폴더에 남아 재로그인 불필요."""
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=headless,
        locale=locale,
        user_agent=ua,
        viewport=DEFAULT_VIEWPORT,
    )


def login(p: Playwright, profile_dir: str | Path, *, timeout_sec: int = 300,
          poll_sec: float = 2.0) -> bool:
    """헤드풀 창을 열어 사용자가 FB 로그인하게 하고, 세션 쿠키가 잡히면 저장 후 종료.

    반환: 제한시간 내 로그인 감지 True / 미감지 False.
    goto/세션 조회 실패는 AuthError로 래핑해 raw Playwright 메시지 대신 복구 안내를 준다.
    """
    import time

    try:
        ctx = open_persistent_context(p, profile_dir, headless=False)
    except Exception as e:
        raise AuthError(f"로그인 창 초기화 실패 ({type(e).__name__}) — chromium/프로필 경로 확인.") from e
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded",
                      timeout=45000)
        except Exception as e:
            raise AuthError("FB 접속 실패 — 네트워크 또는 접속 차단 확인.") from e
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            state = _session_state(ctx)
            if state is True:
                return True
            if state is None:
                # 컨텍스트가 죽었으면 300초 헛도는 대신 즉시 실패
                raise AuthError("로그인 창 세션 조회 실패 — 창이 닫혔는지 확인 후 재시도.")
            time.sleep(poll_sec)
        return False
    finally:
        ctx.close()  # persistent 컨텍스트는 close 시 세션이 프로필에 flush됨(Chromium 동작 의존)


# ── ③ browser 쿠키 직접 읽기 ──

def cookies_from_browser(browser_name: str = "firefox") -> _RedactedCookieList:
    """설치된 브라우저(로그인 상태)에서 facebook.com 쿠키를 직접 읽어 Playwright 형식으로 변환.

    browser_cookie3 미설치 시 AuthError(자동 설치 금지 — 의존성 설치는 승인 게이트 대상).
    """
    if browser_name not in _ALLOWED_BROWSERS:
        raise AuthError(f"지원하지 않는 브라우저: {browser_name!r} "
                        f"({', '.join(sorted(_ALLOWED_BROWSERS))})")
    try:
        import browser_cookie3  # optional dependency
    except ImportError as e:
        raise AuthError(
            "browser_cookie3 미설치 — `--auth persistent`(권장) 사용하거나, "
            "`pip install browser_cookie3` 후 재시도. (자동 설치는 승인 게이트 대상)"
        ) from e
    try:
        loader = getattr(browser_cookie3, browser_name)  # 화이트리스트 통과분만
    except AttributeError as e:  # 라이브러리 버전이 해당 브라우저 미지원 시 raw 대신 AuthError
        raise AuthError(f"browser_cookie3가 {browser_name!r}를 지원하지 않습니다 — firefox 권장.") from e
    jar = loader(domain_name="facebook.com")
    out = _RedactedCookieList()
    skipped = 0
    for c in jar:
        if "facebook.com" not in (c.domain or ""):
            skipped += 1
            continue  # 라이브러리 필터를 신뢰하지 않고 재확인(비-FB 도메인 주입 차단)
        try:
            out.append(_to_pw_cookie(c.name, c.value, c.domain, c.path or "/",
                                     bool(getattr(c, "secure", False)),
                                     int(c.expires) if c.expires else -1))
        except ValueError as ve:  # 잘못된 도메인 쿠키는 건너뜀(crash 대신 graceful skip)
            _log.debug("cookies_from_browser: 쿠키 변환 건너뜀 — %s", ve)
            skipped += 1
    if skipped:
        _log.debug("cookies_from_browser: 제외 쿠키 %d개", skipped)
    if not out:
        raise AuthError(
            f"{browser_name!r}에서 facebook.com 쿠키 0개 — 해당 브라우저에 FB 로그인 상태인지 확인. "
            "(Chrome은 v127+ 암호화로 실패 가능 → firefox 권장)"
        )
    return out


# ── ① cookies.txt (Netscape) fallback ──

def load_netscape_cookies(path: str | Path) -> _RedactedCookieList:
    """Netscape cookies.txt → Playwright 쿠키 (자작 수집 도구 load_cookies_for_playwright 이식).

    경로/내용은 사용자 본인 입력이라 경로 화이트리스트는 두지 않되(정당 위치 다양),
    에러 메시지에 전체 경로를 노출하지 않는다(불변식 2: 공유 시 PII).
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise AuthError("cookies.txt 파일을 찾을 수 없습니다.") from e
    except (OSError, UnicodeDecodeError) as e:
        raise AuthError(f"cookies.txt 읽기 실패 ({type(e).__name__}).") from e

    cookies = _RedactedCookieList()
    skipped = 0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            skipped += 1
            continue
        domain, _include_subdomain, cpath, secure, expires, name, value = parts
        if "facebook.com" not in domain:
            continue
        try:
            expires_int = int(expires)
        except ValueError:
            expires_int = -1
        try:
            cookies.append(_to_pw_cookie(name, value, domain, cpath,
                                         secure.upper() == "TRUE",
                                         expires_int if expires_int > 0 else -1))
        except ValueError as ve:  # 잘못된 도메인 쿠키는 건너뜀(crash 대신 graceful skip)
            _log.debug("cookies.txt: 쿠키 변환 건너뜀 — %s", ve)
            skipped += 1
    if skipped:
        _log.debug("cookies.txt: 건너뛴 줄/쿠키 %d개", skipped)
    if not cookies:
        raise AuthError("cookies.txt에 facebook.com 쿠키가 없습니다.")
    return cookies


# ── 공통 ──

def _to_pw_cookie(name: str, value: str, domain: str, path: str, secure: bool,
                  expires: int) -> _RedactedCookie:
    """Playwright add_cookies 형식 쿠키.

    httpOnly=True 강제 — 주입 쿠키를 페이지 JS(document.cookie)가 못 읽게 한다.
    크롤러는 JS의 쿠키 접근이 불필요하므로 전 쿠키에 적용해도 안전하고, FB 세션쿠키
    전체를 빠짐없이 덮는다(부분 화이트리스트 누락 방지). repr에 value 노출 차단.
    """
    dom = domain.rstrip(".")  # browser_cookie3가 가끔 주는 trailing dot 제거
    if not dom:  # "." / "" / ".." 등 빈 도메인 방어 (호출 측 facebook.com 필터의 2중 방어)
        raise ValueError(f"유효하지 않은 쿠키 도메인: {domain!r}")
    dom = dom if dom.startswith(".") else "." + dom
    return _RedactedCookie({
        "name": name,
        "value": value,
        "domain": dom,
        "path": path,
        "expires": expires,
        "httpOnly": True,
        "secure": secure,
        "sameSite": "Lax",
    })


def _session_state(ctx: BrowserContext) -> bool | None:
    """세션 상태 3값: True=세션 있음, False=세션 없음, None=조회 실패(컨텍스트 이상)."""
    try:
        cookies = ctx.cookies()
    except Exception as e:
        # 예외 메시지(e)에 쿠키 값이 섞일 수 있어 타입만 기록(불변식 2)
        _log.debug("ctx.cookies() 실패 — %s (메시지 생략)", type(e).__name__)
        return None
    for c in cookies:
        if c.get("name") == _SESSION_COOKIE and c.get("value"):
            return True
    return False


def _has_session(ctx: BrowserContext) -> bool:
    """세션 존재 여부(조회 실패는 False로 본다 — 호출 측이 None 구분이 필요하면 _session_state)."""
    return _session_state(ctx) is True


def authenticated_context(p: Playwright, mode: str = "persistent", *,
                          profile_dir: str | Path | None = None,
                          cookies_path: str | Path | None = None,
                          browser_name: str = "firefox",
                          headless: bool = True) -> tuple[BrowserContext, Browser | None]:
    """인증된 (context, browser) 를 돌려준다.

    persistent 모드: browser=None (context.close()로 정리).
    browser/cookies_txt 모드: browser 반환 (context.close() 후 browser.close() 필요).

    세션(c_user) 미존재 시 AuthError로 명확히 실패한다(로그인월 진입 전 차단).
    """
    if mode == "persistent":
        if not profile_dir:
            raise AuthError("persistent 모드는 profile_dir 필요")
        try:
            ctx = open_persistent_context(p, profile_dir, headless=headless)
        except Exception as e:
            raise AuthError(f"persistent 컨텍스트 초기화 실패 ({type(e).__name__}) — chromium/프로필 경로 확인.") from e
        state = _session_state(ctx)
        if state is None:
            ctx.close()
            raise AuthError("persistent 프로필 세션 조회 실패 — 컨텍스트 초기화 중일 수 있음, 재시도.")
        if state is False:
            ctx.close()
            raise AuthError("persistent 프로필에 FB 세션 없음 — 먼저 `login`으로 로그인하세요.")
        return ctx, None

    if mode == "browser":
        cookies = cookies_from_browser(browser_name)
    elif mode == "cookies_txt":
        if not cookies_path:
            raise AuthError("cookies_txt 모드는 cookies_path 필요")
        cookies = load_netscape_cookies(cookies_path)
    else:
        raise AuthError(f"알 수 없는 auth mode: {mode!r} (persistent/browser/cookies_txt)")

    browser = p.chromium.launch(headless=headless)
    try:
        ctx = browser.new_context(locale=DEFAULT_LOCALE, user_agent=DEFAULT_UA,
                                  viewport=DEFAULT_VIEWPORT)
        ctx.add_cookies(cookies)
        if not _has_session(ctx):
            raise AuthError(f"{mode!r} 쿠키에 FB 세션(c_user) 없음 — 로그인 상태 확인.")
    except BaseException:
        browser.close()  # 예외 시 브라우저 프로세스 누수 방지
        raise
    return ctx, browser
