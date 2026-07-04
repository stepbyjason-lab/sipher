# sipher-youtube 어댑터 — Overview

- **상태:** 구현(build) · **작성:** 2026-07-01 · **출처 원본:** 없음(yt-dlp 얇은 래핑)
- **정규화 계약:** `fetch(url) -> { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }`
- **경계:** 어댑터는 수집·정규화만. 노트 합성=`note-factory`, 라우팅=`sipher`. sipher 내부 미-import(추출 가능).

---

## 1. 역할 (overview §5 라우팅 매트릭스)

| 캡션/본문(타이핑) | 첫·고정 댓글 | 카드 이미지 글 | 영상 |
|---|---|---|---|
| **설명란(yt-dlp 메타)** → `body_text` | 옵션(`youtube-comment-downloader`) | — | **다운로드 + whisper/자막**(전사는 다운스트림) |

YouTube는 새 스크래퍼가 필요 없다. **yt-dlp**(공개 OSS, Unlicense)가 메타·미디어·자막·**라이브 채팅**까지 처리하므로,
이 어댑터는 yt-dlp를 얇게 래핑해 정규화 스키마로 변환한다.

## 2. 핵심 결정 (2026-06-30 / 07-01 확정)

- **MCP 비의존.** youtube MCP(Node 서버 + YouTube Data API 키 + 쿼터)를 **요구하지 않는다.**
  MCP가 주던 값은 전부 경량 대체로 커버됨(2026-07-01 실측):
  - 설명란·메타·챕터·**most-replayed 히트맵**·engagement → **yt-dlp `-J`** (키 불필요, `heatmap`·`chapters` 네이티브 필드)
  - 정제 전사 → **`youtube-transcript-api`**(MIT, 키 불필요) — 옵션
  - 댓글 → **`youtube-comment-downloader`**(MIT, 키 불필요) — 옵션
  - 채널/플레이리스트/검색 → yt-dlp `--flat-playlist` / `ytsearchN:` (필요 시 follow-up)
  > 근거: most-replayed는 YouTube 네이티브(yt-dlp `heatmap`, len≈100 실측) — SponsorBlock(CC BY-NC-SA 비상업)이 **아님**.
  > 전 대체재 라이선스 = Unlicense/MIT → sipher LITE 공유가 100% pip, 서버·키 zero.
- **새 스크래퍼 개발 안 함** — yt-dlp를 subprocess(`sys.executable -m yt_dlp`, PATH 비의존)로 호출.
- **`--from-start` opt-in 플래그** — yt-dlp `--live-from-start` 래핑.
  - 기본(플래그 없음): 라이브를 **입력 시점부터**(yt-dlp 기본). Hitomi Downloader 동작.
  - `--from-start`: 라이브를 **처음부터**. 비라이브 영상엔 무영향(yt-dlp 무시).
- **`--with-chat` opt-in** — 라이브 채팅을 `live_chat` 자막 트랙으로 다운로드(`<id>.live_chat.json`).
  끝난 라이브의 replay(전체) + 진행 중 라이브(`--from-start`와 함께 처음부터)를 캡처. MCP·Data API로는 불가한 영역.
- **transcript는 어댑터가 기본으론 안 채움** — `transcript: None`(whisper 다운스트림). `--with-transcript`면 `youtube-transcript-api`로 채움(없으면 None → whisper 폴백).

## 3. 모듈 구조

```
adapters/youtube/
├── docs/00-overview.md
├── __init__.py     # 공개 API: fetch · parse_url · normalize (yt-dlp 없이 import 가능)
├── scrape.py       # yt-dlp subprocess 래핑: probe · download · download_live_chat
├── transcript.py   # (옵션) youtube-transcript-api → 정제 전사. 미설치 시 graceful None
├── comments.py     # (옵션) youtube-comment-downloader → 상위/고정 댓글. 미설치 시 graceful []
├── cli.py          # python -m adapters.youtube.cli fetch <URL> [...]
└── requirements.txt
```

## 4. 보안·견고성 (멀티렌즈)

- **인자 인젝션/SSRF 차단:** `parse_url`이 YouTube 호스트 + 11자 `video_id`(`[A-Za-z0-9_-]{11}`)만 통과.
  yt-dlp에는 **검증된 id로 재구성한 canonical URL**(`https://www.youtube.com/watch?v=<id>`)만 넘기고,
  positional 앞에 `--`를 둔다. 원본 URL은 provenance용 `source`에만 보존.
- **graceful degradation:** yt-dlp 미설치 → `YtdlpError`("yt-dlp 설치 필요"). 옵션 pip 미설치 → 로그 후 None/[].
- **타임아웃:** 메타 probe 상한. 다운로드는 라이브 특성상 장시간 가능(기본 무제한, `--timeout`로 상한).
- **결정적 출력 경로:** `-o "%(id)s.%(ext)s"` → id 기준 glob으로 산출물(영상/자막/채팅) 분리 회수.

## 5. 정직 라벨 (overview §12.5)

`meta.video_label`: `none`(미다운) · `downloaded` · `downloaded_from_start`(--from-start 라이브) · `clipped`(--sections 시간구간 다운) · `download_failed`
`meta.chat_label`: `none` · `replay_full`(끝난 라이브 replay) · `live_captured`(진행 중) · `disabled`(채팅 없음/비활성) · `download_failed`(yt-dlp 실행 자체가 실패)
`meta.transcript_label`: `none`(--with-transcript 미사용) · `fetched` · `unavailable`(자막 자체가 없음 — 정직한 없음) · `fetch_failed`(네트워크/차단/라이브러리 미설치 등)
`meta.comments_label`: `none`(--with-comments 미사용) · `fetched` · `fetch_failed`(네트워크/파싱 실패 또는 라이브러리 미설치 — 부분 수집분은 `comments[]`에 유지될 수 있음)
`meta.live_status`(yt-dlp 원값 `is_live`/`was_live`/`post_live`/`not_live`/`is_upcoming`)로 라이브 여부 정직 표기.
`meta.engagement_label`: `computed`(view_count>0, `meta.engagement` 계산됨) · `zero_views`(view_count==0, 정상 — engagement는 None) · `unavailable`(view_count 결측/비수치, 정상 아님 — engagement도 None). `meta.engagement`만 보면 "조회수 0"과 "데이터 없음"이 둘 다 None으로 뭉개지므로 이 라벨로 구분한다.
`meta.auto_caption_langs`: 자동 생성 자막의 언어 코드 목록(`sorted(automatic_captions.keys())`). 수동 자막의 `meta.subtitle_langs`와 대칭 — `meta.auto_caption_available`(bool)은 유무만, 이 필드는 어떤 언어인지 알려준다.
`meta.from_start` vs `meta.video_label`: `from_start`는 **요청 의도**(호출 시 `--from-start`를 줬는지)일 뿐이며, 실제로 라이브를 처음부터 받았는지의 진실원천은 `meta.video_label=="downloaded_from_start"`다(비라이브 영상엔 의도해도 무시되어 `downloaded`로 남는다).

**`--sections`는 seek 가능한 VOD/종료된 라이브 전용** — 진행 중 라이브는 yt-dlp가 부분 다운로드
불가("This format cannot be partially downloaded")하여 `download_failed`로 정직 처리된다.

## 6. CLI

```
python -m adapters.youtube.cli fetch <URL>
    [--media-dir DIR]      # 지정해야 미디어/자막/채팅 다운로드(미지정=메타만)
    [--from-start]         # 라이브 처음부터(--live-from-start)
    [--no-video]           # 미디어 다운로드 생략
    [--no-subs]            # 자막 파일 생략
    [--sub-langs ko,en]    # 자막/전사 언어
    [--with-chat]          # 라이브 채팅(live_chat.json) — --media-dir 필요
    [--with-transcript]    # 정제 전사(youtube-transcript-api)
    [--with-comments]      # 상위/고정 댓글(youtube-comment-downloader)
    [--max-comments N]     # 댓글 개수(기본 20)
    [--timeout SEC]        # 다운로드 상한(라이브 무한대기 방지)
    [--sections "*0-300"]  # 시간 구간만(0~5분). VOD/종료라이브 전용, ffmpeg 필요
```

## 7. 의존성

- **필수:** `yt-dlp`(pip, Unlicense). ffmpeg 권장(포맷 병합·자막 변환) — 없으면 degrade.
- **옵션:** `youtube-transcript-api`(MIT) · `youtube-comment-downloader`(MIT). 미설치 시 해당 기능만 skip.

## 8. 라이선스·출처 (Attribution)

이 어댑터는 **외부 코드를 이식(vendoring)하지 않는다** — facebook/naver_blog와 달리 남의 소스를
복사하지 않고, 아래 도구들을 **런타임 의존**(yt-dlp는 subprocess 호출, 나머지는 옵션 pip import)으로
사용한다. 각 도구의 라이선스·저작권·원본 레포를 명시한다(마디 `docs/15 capability-adoption-and-license-review`).
LICENSE 원문 확인일: 2026-07-01.

| 도구 | 역할 | 라이선스 | 저작권 | 원본 레포 |
|---|---|---|---|---|
| **yt-dlp** | 필수 — 메타·미디어·자막·라이브채팅·`--live-from-start` | **The Unlicense**(퍼블릭 도메인) | yt-dlp project | https://github.com/yt-dlp/yt-dlp |
| **youtube-transcript-api** | 옵션 — 정제 전사(`--with-transcript`) | **MIT** | © 2018 Jonas Depoix | https://github.com/jdepoix/youtube-transcript-api |
| **youtube-comment-downloader** | 옵션 — 댓글(`--with-comments`) | **MIT** | © 2015 Egbert Bouman | https://github.com/egbertbouman/youtube-comment-downloader |

- **호환성·의무:** Unlicense(퍼블릭 도메인) + MIT 모두 상업·비상업 사용 가능. sipher는 이들 코드를
  **번들·재배포하지 않고**(사용자가 pip로 설치) 호출만 하므로 배포물에 라이선스 텍스트 동봉 의무는 없다.
  단 **본 문서로 출처·라이선스를 고지**하며, 향후 MIT 도구를 vendoring하게 되면 그 시점에 각
  `LICENSE`(저작권 + permission notice)를 포함해야 한다.
- **평가했으나 미채택:** youtube MCP(`github.com/wynandw87/claude-code-youtube-mcp`, README에 MIT
  선언이나 `LICENSE` 파일·`package.json` license 필드 부재)는 Node 서버 + YouTube Data API 키(ToS·쿼터)
  + SponsorBlock(CC BY-NC-SA, **비상업**) 의존이라 sipher 필수 의존에서 **제외**(§2). 코드 미사용 →
  이식·재배포 없음.
- **데이터 약관(정직 고지):** 위 도구들은 YouTube를 스크래핑한다(공식 API 아님) → YouTube ToS 회색지대.
  overview §7 "개인용 적합" 전제. most-replayed 히트맵은 **yt-dlp 네이티브**(라이선스 무관)이며
  SponsorBlock(비상업)이 아니다.
