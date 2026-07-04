<!-- Language: English · [한국어](README.ko.md) -->

# Sipher

**Throw any URL or file at it — get back clean, normalized content.**<br>
**아무 URL이나 파일을 던지면 — 깨끗하게 정규화된 콘텐츠로 돌려줍니다.**

Sipher is a single entry point for pulling content out of social media, the web, and
local files. Instead of remembering which scraper works for which platform, you run
one command and get a consistent, structured result every time.

Sipher는 SNS·웹·로컬 파일에서 콘텐츠를 꺼내는 **단일 진입점**입니다. 어느 플랫폼에
어느 스크래퍼를 써야 하는지 매번 고민할 필요 없이, 명령 하나로 항상 같은 구조의
결과를 얻습니다. ([한국어 문서 전체 보기 →](README.ko.md))

**All AI enrichment (vision OCR, speech transcription) runs on free tiers and local
models — $0 in API costs on the default setup. Paid APIs are strictly opt-in.**<br>
**AI 보강(비전 OCR·음성 전사)까지 전부 무료 티어 + 로컬 모델로 — 기본 설정 기준
API 비용 $0. 유료 API는 직접 켜야만 동작하는 옵트인입니다.**

> **Sipher** = *siphon* (pull it in) + *(de)cipher* (clean it up).
> Siphon any URL, decipher it into clean content.

```bash
python -m core fetch "https://www.threads.net/@someone/post/XXXX"
```

```
→ body text, comments, media, and metadata —
  as readable Markdown (default) or structured JSON (--json)
```

---

## Why

Every platform needs a different tool — `yt-dlp` for YouTube, `gallery-dl` for TikTok,
a headless browser for Threads, a mobile API for Naver Blog. You end up picking (and
mis-picking) a different scraper every time.

Sipher fixes that with **one rule: give it a URL, it routes to the right extractor.**

- **One interface, every source.** 6 platforms + a generic web fallback + local files,
  all returning the *same* normalized shape.
- **Deterministic-first, $0.** Typed text is read straight from the page (free).
  Text in images goes through a free vision-OCR ensemble. Audio/video goes through
  local Whisper with a free Groq fallback. Even the heavy AI is free by default —
  see "The free AI stack" below.
- **Honest by design.** Every result is labeled with what actually happened —
  `done`, `partial`, `fetch_failed`, `skipped_no_tool`. No silent failures, no faked
  "success" when a step was skipped or blocked.
- **A thin router, not a rewrite.** Sipher wraps battle-tested tools; it doesn't
  reimplement scraping.

---

## The free AI stack — $0 by default

Sipher's AI enrichment is designed to run **end-to-end without a paid key**. It buys
reliability by stacking *free providers* (a multi-provider ladder), not by adding a
credit card.

| Stage | Models | Cost |
|---|---|---|
| Body & comments | Deterministic parsing — no LLM, read straight from the page | Free |
| Image OCR | **Free ensemble**: `gemini-2.5-flash` + `google/gemma-4-31b-it` + `nvidia/nemotron-nano-12b-v2-vl` (NVIDIA NIM) candidates, cross-checked by a free judge (`gemma-4`). Measured more accurate than any single model on Korean cards | Free tier |
| Audio/video transcription | **Local first**: faster-whisper `large-v3` → **free fallback**: Groq `whisper-large-v3-turbo` (then `whisper-large-v3` on quota). Video gets its audio extracted via ffmpeg before upload | Local / free tier |
| Paid fallback | `claude-sonnet-4-5` — only runs if **you** set `OCR_PAID_FALLBACK=claude` | Opt-in |

- When a free quota runs out, sipher leaves an **honest skip/degrade label** instead
  of silently charging you.
- The NVIDIA NIM key is free at [build.nvidia.com](https://build.nvidia.com), no card required.

---

## What it can do

| Source | What you get |
|---|---|
| **Threads** | Post body, **nested comments**, media. Fast pass → auto-escalates to a deep crawl when incomplete. |
| **YouTube** | Description, metadata, media, `--from-start` (live from the beginning), live-chat replay, optional transcript & comments. |
| **Facebook** | Body, **full-size photos** (lightbox bypass + hidden `+N` shots), video, and **comment bodies** with honest confidence labels. |
| **Instagram** | Caption, media, metadata. Login session required (Instagram blocks anonymous access) — reported honestly via access labels. |
| **TikTok** | Caption, stats, metadata; optional video download. |
| **Naver Blog** | Mobile-API listing + body + metadata + original-resolution images. (Pure standard library — zero dependencies.) |
| **Any web article** | Generic fallback for anything the 6 platforms don't cover. Two-tier: fast static fetch → JS-rendered browser when the page is an SSR shell. Built-in SSRF defense. |
| **Local files** | PDF / DOCX / PPTX / XLSX / CSV / images / audio / video → text, via document conversion + OCR + transcription. |

### Enrichment (opt-in)

- `--ocr` — extract text from images. Default is a **free multi-provider
  ensemble** (Gemini + NVIDIA NIM candidates, cross-checked by a free judge) that
  measured better than any single model on Korean cards; falls back to Gemini
  alone without a NIM key.
- `--transcribe` — transcribe audio/video. Local Whisper first; if it's missing or
  fails, **auto-falls back to free Groq Whisper** — a machine with no GPU can still
  transcribe with just a Groq key.

---

## Built on — sources & credits

This is what "wraps battle-tested tools" actually means. Per platform, here is what
runs underneath — and what the author wrote from scratch vs. open source:

| Platform | Based on | Home-grown / modified |
|---|---|---|
| **Threads** | fork of [vdite/threads-scraper](https://github.com/vdite/threads-scraper) (MIT) | 3 modifications in [our fork](https://github.com/stepbyjason-lab/threads-scraper): media extraction & download, target-thread scoping (drops recommended-feed noise), fast→deep tiered dispatcher |
| **YouTube** | [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) | Thin wrapper — only the `--from-start` / live-chat-replay wiring and normalization are ours |
| **Facebook** | **Written by the author** | Lightbox-bypass full-size photos, hidden `+N` shots, comment collection — built from scratch because no public alternative exists |
| **Instagram** | [instaloader](https://github.com/instaloader/instaloader) (MIT) | Direct library calls + our honest access-label layer |
| **TikTok** | [gallery-dl](https://github.com/mikf/gallery-dl) (GPL-2.0) — video downloads are delegated internally to [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Called across a subprocess boundary (no code linkage) |
| **Naver Blog** | **Written by the author** | Pure standard library (zero deps) — mobile API + original-resolution images |
| **Generic web** | [fivetaku/insane-search](https://github.com/fivetaku/insane-search) engine (MIT, vendored unmodified) | Tier 1 (WAF grid, SSRF defense) is the engine as-is. Tier 2 JS-render and auto-escalation are ours |

Vendored code is tracked with a `_SOURCE.md` (upstream, commit SHA, modification log)
and the original `LICENSE` in each adapter folder — details in
[adapters/README.md](adapters/README.md).

---

## Output shape

One normalized schema, no matter the source:

```json
{
  "source": "...",
  "platform": "threads | youtube | facebook | instagram | tiktok | naver_blog | web | local",
  "body_text": "...",
  "comments": [ { "author": "...", "text": "...", "likes": 0 } ],
  "ocr_text": [ { "media_path": "...", "text": "..." } ],
  "transcript": "... or null",
  "media_paths": [ "media/..." ],
  "meta": { "...": "honest labels + platform metadata" }
}
```

Human-readable Markdown is the default; add `--json` for the machine shape,
`--out FILE` to write to disk.

---

## Quick start

```bash
git clone <repo-url> sipher
cd sipher

# LITE (default) or FULL profile — creates a venv and installs deps
scripts/setup.sh lite            # bash
scripts/setup.ps1 -Profile lite  # PowerShell

# Run
.venv/bin/python -m core fetch "<URL or file path>"
# Windows: .venv\Scripts\python.exe -m core fetch "<URL or file path>"
```

**Profiles**

| Profile | Adapters | For |
|---|---|---|
| **LITE** | core + Naver Blog + YouTube + TikTok + web | Public content + free OCR & transcription (Groq key, no GPU needed). No personal login sessions — easy to share. |
| **FULL** | LITE + Threads + Facebook + Instagram + Whisper | Needs browser login sessions / GPU. Personal use. |

See **[docs/08-packaging.md](docs/08-packaging.md)** for the full dependency matrix,
system requirements (ffmpeg, Whisper, Playwright browsers), and API keys.

---

## Language

The pipeline is **language-agnostic** and adapts to you automatically:

- **On first run, sipher detects your OS locale** and stores it as `SIPHER_LANG`
  in `.env.local` — edit it anytime (e.g. `SIPHER_LANG=en`, `ja`, `ko`).
- Vision OCR and Whisper transcription follow this setting. Korean uses a
  PoC-validated prompt; every other language gets a language-neutral one.
- CLI help and `.env.example` are bilingual (English + Korean). Internal docs
  are currently Korean (the tool itself works anywhere).

---

## Documentation

| Doc | Contents |
|---|---|
| [docs/08-packaging.md](docs/08-packaging.md) | Packaging, profiles, setup, dependency matrix |
| [adapters/README.md](adapters/README.md) | Adapter list & licensing |
| `adapters/*/docs/` | Per-adapter deep dives |

---

## License

[MIT](LICENSE). Vendored and third-party components keep their own licenses —
see the `LICENSE` file's *Third-party components* section and
[adapters/README.md](adapters/README.md).

---

<sub>한국어 안내는 **[README.ko.md](README.ko.md)** 를 참고하세요.</sub>
