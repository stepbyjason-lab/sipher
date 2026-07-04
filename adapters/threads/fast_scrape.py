"""Fast single-post Threads scraper: grabs author thread + visible comments from
the initial embedded JSON and graphql responses. No deep reply-chain crawl.

Usage:
    python fast_scrape.py <url> <out.json> [--download]

With --download, images/videos are saved under ./downloads/<author>_<code>/ and
each post in the JSON gets a `downloaded` list of local paths. Download right
away — the Threads CDN URLs are signed and expire within hours.
"""
import json, asyncio, sys, os
from playwright.async_api import async_playwright
from parsel import Selector
from .media_utils import extract_media, download_media, iter_thread_posts

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "threads_cookies.json")


def parse_post(p):
    if not isinstance(p, dict):
        return None
    try:
        cap = p.get("caption") or {}
        text = cap.get("text")
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
            "author": author,
            "likes": p.get("like_count", 0),
            "reply_count": p.get("text_post_app_info", {}).get("direct_reply_count", 0),
            "taken_at": p.get("taken_at"),
            "images": images,
            "videos": videos,
        }
    except Exception:
        return None


async def scrape(url, out, do_download=False):
    found = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="en-US")
        if os.path.exists(COOKIE_FILE):
            with open(COOKIE_FILE) as f:
                await ctx.add_cookies(json.load(f))
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

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)  # let graphql land
            html = await page.content()
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
            sys.stderr.write(f"ERR {url}: {ex}\n")
        await browser.close()

    posts = list(found.values())
    if do_download:
        count = download_media(posts, out_dir="downloads")
        sys.stderr.write(f"Downloaded {count} media file(s) into ./downloads/\n")

    with open(out, "w", encoding="utf-8") as f:
        json.dump(posts, f, indent=2, ensure_ascii=False)
    sys.stderr.write(f"OK {out}: {len(posts)} posts\n")
    return posts


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_download = "--download" in sys.argv
    if len(args) < 2:
        print("Usage: python fast_scrape.py <url> <out.json> [--download]")
        sys.exit(1)
    asyncio.run(scrape(args[0], args[1], do_download=do_download))
