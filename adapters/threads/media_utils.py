"""Shared media helpers for the Threads scrapers.

Threads reuses Instagram's media schema on its GraphQL `post` objects:
  - image_versions2.candidates : list of {url, width, height} (pick largest)
  - video_versions             : list of {url, width, height, type}
  - carousel_media             : list of sub-posts, each with its own media

The CDN URLs are SIGNED and time-limited (the `oe=` query param is an expiry),
so anything you want to keep must be downloaded shortly after scraping.
"""
import os
import re
import sys
import urllib.request
from typing import Dict, List, Tuple

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# Threads embeds a "related posts" recommendation feed in the same page/GraphQL
# payload as the real reply tree. An indiscriminate nested_lookup("post") pulls
# those unrelated posts in as if they were comments (confirmed: feed posts sit
# under a `relatedPosts` container, real replies under plain `thread_items`).
# Pruning these container keys keeps extraction scoped to the target thread.
FEED_CONTAINER_KEYS = {"relatedPosts", "related_posts"}


def iter_thread_posts(data, blocked_keys=FEED_CONTAINER_KEYS) -> List[Dict]:
    """Returns every `post` object in `data` except those inside a recommendation
    / related-posts container. Drop-in replacement for nested_lookup("post", data)
    that no longer treats Threads' "related posts" feed as replies.
    """
    out: List[Dict] = []

    def rec(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in blocked_keys:
                    continue  # prune the recommendation/feed subtree entirely
                if key == "post" and isinstance(value, dict):
                    out.append(value)
                rec(value)
        elif isinstance(node, list):
            for item in node:
                rec(item)

    rec(data)
    return out


def _best_url(candidates: List[Dict]) -> str:
    """Picks the highest-resolution URL from a list of media candidates."""
    best, best_w = None, -1
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        url, width = c.get("url"), c.get("width") or 0
        if url and width >= best_w:
            best, best_w = url, width
    return best


def extract_media(post_data: Dict) -> Tuple[List[str], List[str]]:
    """Returns (image_urls, video_urls) from a Threads post object.

    Handles single image/video posts and carousels. For a video node the
    cover image is intentionally skipped so it is not double-counted.
    """
    images: List[str] = []
    videos: List[str] = []

    def from_node(node: Dict):
        if not isinstance(node, dict):
            return
        video_versions = node.get("video_versions")
        if video_versions:  # video node: take the video, skip its cover image
            url = _best_url(video_versions)
            if url and url not in videos:
                videos.append(url)
            return
        url = _best_url((node.get("image_versions2") or {}).get("candidates"))
        if url and url not in images:
            images.append(url)

    carousel = post_data.get("carousel_media")
    if carousel:
        for item in carousel:
            from_node(item)
    else:
        from_node(post_data)

    return images, videos


def _ext_from_url(url: str, default: str) -> str:
    """Best-effort file extension from a CDN URL path (ignores query string)."""
    path = url.split("?", 1)[0]
    match = re.search(r"\.(jpg|jpeg|png|webp|mp4|mov)$", path, re.IGNORECASE)
    return "." + match.group(1).lower() if match else default


def _download_one(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://www.threads.com/"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def download_media(posts: List[Dict], out_dir: str = "downloads") -> int:
    """Downloads every image/video referenced in `posts`.

    Files are saved under `out_dir/<author>_<code>/`. Each post dict is
    annotated in place with a `downloaded` list of local file paths.
    Returns the total number of files successfully downloaded.

    Run this right after scraping: the source URLs expire within hours.
    """
    total = 0
    for post in posts:
        images = post.get("images") or []
        videos = post.get("videos") or []
        if not images and not videos:
            continue

        key = post.get("code") or post.get("id") or "post"
        folder = os.path.join(out_dir, f"{post.get('author', 'unknown')}_{key}")
        os.makedirs(folder, exist_ok=True)

        local_paths: List[str] = []
        for i, url in enumerate(images, 1):
            dest = os.path.join(folder, f"img_{i:02d}{_ext_from_url(url, '.jpg')}")
            try:
                _download_one(url, dest)
                local_paths.append(dest)
                total += 1
            except Exception as exc:
                sys.stderr.write(f"  [dl-fail img] {key} #{i}: {exc}\n")
        for i, url in enumerate(videos, 1):
            dest = os.path.join(folder, f"vid_{i:02d}{_ext_from_url(url, '.mp4')}")
            try:
                _download_one(url, dest)
                local_paths.append(dest)
                total += 1
            except Exception as exc:
                sys.stderr.write(f"  [dl-fail vid] {key} #{i}: {exc}\n")

        post["downloaded"] = local_paths
    return total
