"""Fast single-post Threads scraper: grabs author thread + visible comments from
the initial embedded JSON and graphql responses. No deep reply-chain crawl.

Usage:
    python -m adapters.threads.fast_scrape <url> <out.json> [--download]

With --download, images/videos are saved under ./downloads/<author>_<code>/ and
each post in the JSON gets a `downloaded` list of local paths. Download right
away — the Threads CDN URLs are signed and expire within hours.
"""
import json, asyncio, sys, os
from pathlib import Path
from playwright.async_api import async_playwright
from parsel import Selector
from .media_utils import extract_media, extract_text_blocks, text_from_blocks, download_media, iter_thread_posts

_ROOT = Path(__file__).resolve().parents[2]  # 사본 루트(.env.local 위치)
_LEGACY_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "threads_cookies.json")
def _login_path(key: str) -> Path | None:
    """로그인 파일 경로: 사본 루트 `.env.local`의 `key` → 같은 이름의 환경변수 순. 미설정이면 None.

    상대경로는 사본 루트 기준으로 푼다. 어댑터 사이 공용 규칙이지만 각 어댑터가 sipher 내부를
    import 하지 않도록 어댑터마다 같은 함수를 둔다.
    """
    value = None
    env_local = _ROOT / ".env.local"
    if env_local.is_file():
        # utf-8-sig: 메모장 등이 붙인 BOM이 첫 키 이름에 섞이지 않게 한다.
        for line in env_local.read_text(encoding="utf-8-sig").splitlines():
            name, sep, raw = line.strip().partition("=")
            if sep and name.strip() == key:
                value = raw.strip().strip('"').strip("'") or None
    value = value or os.environ.get(key)
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else _ROOT / path


def _cookie_file() -> str:
    """`THREADS_COOKIES_FILE`이 있으면 그 자리, 없으면 기존 위치(어댑터 폴더 안). 읽기·로그인 저장 공용."""
    return str(_login_path("THREADS_COOKIES_FILE") or _LEGACY_COOKIE_FILE)


COOKIE_FILE = _cookie_file()

# 브라우저 확장(Cookie-Editor·EditThisCookie 등) JSON의 sameSite → Playwright 값. 그 밖의 값은 빼고 넘긴다.
_SAME_SITE = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}


def _to_pw_cookie(c, where: str) -> dict:
    """Playwright JSON·확장 JSON 항목 하나 → add_cookies 모양. 쿠키 값은 오류 문구에 싣지 않는다."""
    if not isinstance(c, dict):
        raise ValueError(f"threads: 쿠키 파일 {where} 항목이 객체가 아니다")
    missing = [k for k in ("name", "value", "domain") if k not in c]
    if missing:
        raise ValueError(f"threads: 쿠키 파일 {where} 항목에 {', '.join(missing)} 필드가 없다")
    expires = c.get("expires", c.get("expirationDate"))
    out = {
        "name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path") or "/",
        "expires": -1 if c.get("session") or expires is None else float(expires),
        "httpOnly": bool(c.get("httpOnly")), "secure": bool(c.get("secure")),
    }
    same_site = _SAME_SITE.get(str(c.get("sameSite") or "").lower())
    if same_site:
        out["sameSite"] = same_site
    return out


def _netscape_cookies(text: str) -> list[dict]:
    """넷스케이프 cookies.txt(탭 7칸). `#HttpOnly_` 접두 줄은 httpOnly 쿠키다(curl·yt-dlp 관례)."""
    out = []
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _subdomains, path, secure, expires, name, value = parts
        try:
            exp = float(expires)
        except ValueError:
            exp = 0
        out.append({"name": name, "value": value, "domain": domain, "path": path or "/",
                    "expires": exp if exp > 0 else -1, "httpOnly": http_only,
                    "secure": secure.upper() == "TRUE"})
    return out


def load_cookies(path) -> list[dict]:
    """쿠키 파일을 내용으로 판별해 Playwright `add_cookies` 모양으로 돌려준다.

    받는 형식: Playwright JSON(`context.cookies()` 배열 또는 `storage_state`의 `cookies`), 브라우저 확장
    JSON(`expirationDate`·`session`·`sameSite: no_restriction|lax|strict|unspecified`), 넷스케이프 cookies.txt.
    모르는 형식이면 ValueError. 오류 문구에는 파일 이름만 싣고 쿠키 값·전체 경로는 싣지 않는다.
    """
    name = Path(path).name
    text = Path(path).read_text(encoding="utf-8-sig")
    if text.lstrip()[:1] in ("[", "{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"threads: 쿠키 파일 {name}을 JSON으로 읽지 못했다(줄 {e.lineno})") from None
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]  # Playwright storage_state
        if not isinstance(data, list):
            raise ValueError(f"threads: 쿠키 파일 {name}의 JSON이 쿠키 배열이 아니다")
        return [_to_pw_cookie(c, f"{name} {i}번") for i, c in enumerate(data)]
    cookies = _netscape_cookies(text)
    if not cookies:
        raise ValueError(
            f"threads: 쿠키 파일 {name}의 형식을 알 수 없다 — Playwright JSON, 브라우저 확장 JSON, "
            "넷스케이프 cookies.txt 중 하나여야 한다"
        )
    return cookies


# 비로그인에게 가려진 글이면 Threads는 게시물 JSON 대신 이 제한 화면(「This content isn't available
# to everyone」)을 싣는다(INC 2026-10-23). 화면 문구는 locale을 따르지만 컴포넌트 이름은 안 바뀐다.
# ponytail: 이름 하나로 판정한다. Threads가 이름을 바꾸면 종전처럼 root_not_found로 떨어질 뿐이다.
RESTRICTED_PAGE_MARKER = "BarcelonaGeoBlockedErrorRoot"


def parse_post(p):
    if not isinstance(p, dict):
        return None
    try:
        text_blocks = extract_text_blocks(p)
        text = text_from_blocks(text_blocks)
        user = p.get("user") or {}
        author = user.get("username")
        images, videos = extract_media(p)
        # Author identifies the post; keep it if it has text OR media so that
        # image/video-only posts (no caption) are no longer dropped.
        if not author or (not text and not images and not videos):
            return None
        return {
            "id": p.get("id"),
            "code": p.get("code"),
            "text": text,
            "text_blocks": text_blocks,
            "author": author,
            "likes": p.get("like_count", 0),
            "reply_count": p.get("text_post_app_info", {}).get("direct_reply_count", 0),
            "taken_at": p.get("taken_at"),
            "images": images,
            "videos": videos,
        }
    except Exception:
        return None


async def _launch_browser(pw, *, headless: bool = True):
    try:
        return await pw.chromium.launch(channel="chrome", headless=headless)
    except Exception as exc:
        sys.stderr.write(f"[threads] Chrome launch failed ({exc}); falling back to bundled Chromium\n")
        return await pw.chromium.launch(headless=headless)


async def scrape(url, out, do_download=False, progress=None):
    found = {}

    def emit(event, **fields):
        if progress is None:
            return
        try:
            progress(event, **fields)
        except Exception:
            # 진행 관찰자는 수집 결과의 성공/실패를 바꾸지 않는다.
            pass

    async def heartbeat():
        while True:
            await asyncio.sleep(5)
            emit("fast_waiting", url=url)

    async with async_playwright() as pw:
        browser = await _launch_browser(pw, headless=True)
        ctx = await browser.new_context(locale="en-US")
        if os.path.exists(COOKIE_FILE):
            await ctx.add_cookies(load_cookies(COOKIE_FILE))
        page = await ctx.new_page()

        async def handle(resp):
            u = resp.url
            if any(k in u for k in ["graphql", "api/v1"]):
                try:
                    for po in iter_thread_posts(await resp.json()):
                        pp = parse_post(po)
                        if pp and pp.get("id"):
                            found[pp["id"]] = pp
                except Exception:
                    pass
        page.on("response", handle)

        ticker = asyncio.create_task(heartbeat())
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)  # let graphql land
            html = await page.content()
            if RESTRICTED_PAGE_MARKER in html:
                emit("fast_content_restricted", url=url)
            sel = Selector(text=html)
            for script in sel.xpath("//script/text()").getall():
                try:
                    s = script.find("{"); e = script.rfind("}") + 1
                    if s == -1 or e == 0:
                        continue
                    for po in iter_thread_posts(json.loads(script[s:e])):
                        pp = parse_post(po)
                        if pp and pp.get("id"):
                            found[pp["id"]] = pp
                except (json.JSONDecodeError, AttributeError):
                    continue
            # light scroll to pull a few more comments (3 rounds max)
            for _ in range(3):
                await page.mouse.wheel(0, 3000)
                await page.wait_for_timeout(1200)
        except Exception as ex:
            if progress is None:
                sys.stderr.write(f"ERR {url}: {ex}\n")
            else:
                emit("fast_error", url=url, error_type=type(ex).__name__)
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            await browser.close()

    posts = list(found.values())
    if do_download:
        count = download_media(posts, out_dir="downloads")
        if progress is None:
            sys.stderr.write(f"Downloaded {count} media file(s) into ./downloads/\n")
        else:
            emit("media_download_complete", url=url, downloaded_count=count)

    with open(out, "w", encoding="utf-8") as f:
        json.dump(posts, f, indent=2, ensure_ascii=False)
    if progress is None:
        sys.stderr.write(f"OK {out}: {len(posts)} posts\n")
    else:
        emit("fast_scrape_complete", url=url, post_count=len(posts))
    return posts


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_download = "--download" in sys.argv
    if len(args) < 2:
        print("Usage: python -m adapters.threads.fast_scrape <url> <out.json> [--download]")
        sys.exit(1)
    asyncio.run(scrape(args[0], args[1], do_download=do_download))
