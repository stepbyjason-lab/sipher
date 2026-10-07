<!-- 언어: 한국어 · [English](README.md) -->

# Sipher

> **현재 공개 버전: v0.1.7**

**아무 URL이나 파일을 던지면 — 깨끗하게 정규화된 콘텐츠로 돌려줍니다.**

Sipher는 SNS·웹·로컬 파일에서 콘텐츠를 꺼내는 **단일 진입점**입니다. 어느 플랫폼에
어느 스크래퍼를 써야 하는지 매번 고민할 필요 없이, 명령 하나로 **항상 같은 구조의
결과**를 얻습니다.

**AI 보강(비전 OCR·음성 전사)까지 전부 무료 티어 + 로컬 모델로 돌아갑니다 —
기본 설정 기준 API 비용 $0. 유료 API는 직접 켜야만 동작하는 옵트인입니다.**

> **Sipher** = *siphon*(빨아들이다) + *(de)cipher*(해독·정제하다).
> 아무 URL이든 빨아들여, 깨끗한 콘텐츠로 해독합니다.

```bash
python -m core fetch "https://www.threads.net/@someone/post/XXXX"
```

```
→ 본문·댓글·미디어·메타데이터를
  사람이 읽는 Markdown(기본) 또는 구조화 JSON(--json)으로
```

---

## 왜

플랫폼마다 도구가 다릅니다 — YouTube는 `yt-dlp`, TikTok은 `gallery-dl`, Threads는
헤드리스 브라우저, 네이버 블로그는 모바일 API. 매번 다른 스크래퍼를 (잘못) 고르게 됩니다.

Sipher는 **딱 하나의 규칙**으로 이걸 없앱니다: **URL만 주면 알맞은 추출기로 라우팅.**

- **인터페이스 하나, 모든 소스.** 7개 플랫폼 + 범용 웹 폴백 + 로컬 파일이 전부
  *같은* 정규화 구조로 나옵니다.
- **deterministic-first, $0.** 타이핑된 글은 페이지에서 바로 읽고(무료), 이미지
  속 글은 무료 비전 OCR 앙상블, 음성/영상은 로컬 Whisper → 무료 Groq 폴백.
  무거운 AI조차 무료가 기본값 — 아래 "무료 AI 스택" 참조.
- **정직한 라벨.** 모든 결과에 실제로 무슨 일이 있었는지 라벨이 붙습니다 —
  `done`·`partial`·`fetch_failed`·`skipped_no_tool`. 조용한 실패도, 건너뛴 단계를
  "성공"이라 속이는 일도 없습니다.
- **얇은 라우터, 재구현 아님.** 검증된 도구들을 묶을 뿐, 스크래핑을 새로 짜지 않습니다.

---

## 무료 AI 스택 — 기본값 기준 $0

Sipher의 AI 보강은 **유료 키 없이 끝까지 돌아가도록** 설계됐습니다. 신뢰성은 결제를
늘리는 대신 **무료 provider를 여러 개 겹쳐서**(멀티-provider 사다리) 얻습니다.

| 단계 | 모델 | 비용 |
|---|---|---|
| 본문·댓글 추출 | 결정적 파싱 — LLM 안 씀, 페이지에서 바로 읽음 | 무료 |
| 이미지 OCR | `.env.local`의 **writer/judge 로스터**(`OCR_CANDIDATES`/`OCR_JUDGES`, `provider:model` 목록 — 코드에 내장된 로스터는 없음). 살아 있는 첫 writer가 후보 하나를 만들고, 살아 있는 첫 judge가 이미지를 직접 보며 그 후보를 교정 — 검증 안 된 결과 없음, 다수결 없음. 권장 로스터(`.env.example` 참조)는 Google Gemini/Gemma writer + NIM Gemma/Muse judge(없으면 Google로 폴백). 카드 이미지 8장을 2회씩 돌려 16/16 통과(누락 0·배경 유입 0·생각 과정 유입 0) | 무료 티어 |
| 음성/영상 전사 | **로컬 우선**: faster-whisper `large-v3` → **무료 폴백**: Groq `whisper-large-v3-turbo`(한도 시 `whisper-large-v3`). 영상은 ffmpeg로 오디오만 추출해 업로드 | 로컬 / 무료 티어 |
| 유료 폴백 | 설정된 무료 writer가 전부 소진된 뒤에만 고려됨. `OCR_PAID_FALLBACK=claude`와 `CLAUDE_OCR_MODEL`(필수, 내장 기본값 없음)을 직접 설정해야 켜짐. 결과도 `OCR_JUDGES`를 거침 | 옵트인 |

- 무료 한도가 소진되면 조용히 과금되는 대신 **정직한 skip/degrade 라벨**을 남깁니다.
- NVIDIA NIM 키는 [build.nvidia.com](https://build.nvidia.com)에서 카드 등록 없이 무료 발급.

---

## 무엇을 할 수 있나

| 소스 | 얻는 것 |
|---|---|
| **Threads** | 본문·원글 작성자 후속글·접힌 rich-text 카드를 기본 수집. 원글 작성자 후속글은 출처 시각이 있을 때 작성자의 실제 게시 시간순으로 반환. continuation은 stderr progress를 내며 45초 예산 뒤에도 확보분을 정직한 `partial` 결과로 반환. 타인 댓글은 `--all-comments`, 조건부 deep 크롤은 `--auto`, 전체 reply tree는 `--deep`으로 명시. |
| **YouTube** | 설명·메타·미디어, `--from-start`(라이브 처음부터), 라이브 채팅 replay, (옵션) 자막·댓글. |
| **Facebook** | 본문, **풀사이즈 사진**(라이트박스 우회 + 숨은 `+N`장), 영상, **댓글 본문**(정직한 신뢰도 라벨). |
| **Instagram** | 캡션·미디어·메타. 로그인 세션 필요(익명 접근 차단) — access 라벨로 정직 보고. |
| **TikTok** | 캡션·통계·메타, (옵션) 영상 다운로드. |
| **X** | 원글 본문, 작성자가 이어 쓴 글(`author_thread[]`), 인용한 포스트(`quoted[]` — 인용 아티클은 제목·표지·이미지까지), 저자 대댓글과 타인 답글(`comments[]`, 항목마다 `kind` 표시), 원본 해상도 사진·영상. **X 로그인 쿠키 필수**(`X_COOKIES_FILE`, 아래 "X 로그인 쿠키" 참조) — 없으면 빈 성공이 아니라 `AuthRequired` 오류로 알립니다. `--max-replies N`(기본 100)은 타인 답글에만 상한을 겁니다. |
| **네이버 블로그** | 모바일 API 목록 + 본문 + 메타 + 원본 해상도 이미지. (순수 표준 라이브러리 — 무의존.) |
| **일반 웹 아티클** | 7개 플랫폼에 안 걸리는 모든 것의 범용 폴백. 2-tier: 빠른 정적 fetch → SSR 껍데기면 JS 렌더 브라우저. SSRF 방어 내장. |
| **로컬 파일** | PDF/DOCX/PPTX/XLSX/CSV/이미지/음성/영상 → 문서 변환 + OCR + 전사로 텍스트화. |

### 보강 (opt-in)

- `--ocr` — 이미지 속 텍스트 추출. 설정된 writer 하나가 후보를 만들고 설정된 judge가
  이미지와 대조해 교정한다(NIM 키 없으면 judge 로스터가 Google 항목으로 폴백) — 로스터
  구성은 위 "무료 AI 스택" 참조.
- `--transcribe` — 음성/영상 전사. 로컬 Whisper 우선, 없거나 실패하면 **무료 Groq
  Whisper로 자동 폴백** — GPU 없는 머신도 Groq 키 하나로 전사 가능.

### 이미지가 주인 게시물: 배경음은 전사하지 않는다

Instagram·Facebook·TikTok의 사진 게시물·카드뉴스·carousel에는 배경음이 깔려 있는 경우가
많습니다. sipher는 이 배경음을 전사하지 않습니다. `transcript`는 `null`로 남고,
`meta.transcript_label`은 `skipped_ambient_audio`이며, 오디오 파일 경로는
`meta.ambient_audio_paths`에 따로 담깁니다. 이 세 플랫폼 결과에는 `meta.content_primary`
(`visual`·`mixed`·`spoken`, 미디어 개수를 모르면 `null`)도 붙고, `visual`일 때만 전사를
건너뜁니다.

### OCR 실패 기록: `meta.ocr_errors`

OCR이 실패한 이미지가 있으면 `meta.ocr_errors`에 실패한 이미지마다 `media_path`, 원인
`reason`, 그리고 `attempts`가 담깁니다. `attempts`는 실패한 프로바이더 호출마다
`{provider, reason}` 한 건씩이며, writer와 judge(시도했다면 유료 폴백 포함)가 모두
들어갑니다. 실패한 이미지가 없으면 이 키는 없습니다. `reason`은 `timeout`·`http_5xx`·
`http_4xx`·`bad_json`·`bad_response`·`network`·`rate_limited`·`quota_exhausted`·
`empty_response`·`writer_failed`·`judge_failed`·`file_missing`·`unexpected`·`other` 중
하나입니다. `meta.ocr_label`은 받은 이미지가 전부 성공했을 때만 `done`입니다 — 어댑터가
신고한 이미지 수가 실제로 받은 수보다 적어도 같습니다.

### 수집 일부가 실패할 때

한 단계가 실패해도 이미 모은 데이터는 버리지 않습니다.

- **YouTube** — 미디어 폴더를 만들지 못해도 제목·설명·챕터는 그대로 돌려주고,
  `meta.video_label`(채팅을 요청했다면 `meta.chat_label`도)이 `download_failed`가 됩니다.
- **OCR 대기 예산** — rate-limit 쿨다운을 기다리는 시간은 **게시물 하나당**
  `OCR_MAX_TOTAL_WAIT`(기본 600초)까지입니다. 다음 게시물은 예산을 처음부터 다시 받습니다.
- **유료 폴백 동의 질문** — 설정된 무료 writer가 모두 소진되면 대화형 터미널에서 유료
  폴백을 쓸지 한 번 묻습니다. 60초 안에 답이 없으면 "아니오"로 보고 다음으로 넘어가며,
  같은 프로세스에서는 다시 묻지 않습니다.

### Threads 진행 상태와 partial 결과

Threads 진행 event는 **stderr** JSON Lines로 나오고 stdout에는 최종 Markdown 또는 JSON만
나옵니다. `meta.author_thread.resolution.status == "partial"`은 수집 실패가 아닙니다.
root와 확보된 원글 작성자 후속글은 유효하며, continuation 시간 예산 안에 해소되지 않은 범위는
`partial_reason`과 `unresolved_candidates`로 확인합니다.

Python 호출자는 `core.fetch()` 또는 `adapters.threads.fetch()`에 `progress=callback`을 넘겨
같은 lifecycle event dict를 직접 받을 수 있습니다.

### Threads 게시 시각

Threads 결과는 각 게시물의 원래 게시 시각을 `created_at_utc`(ISO 8601 UTC, 예:
`2025-08-28T03:12:45+00:00`)로 담습니다. 위치는 root `meta`, 모든 `author_thread[]` 항목,
모든 댓글입니다. 출처에 시각이 없으면 `null`이며, sipher가 페이지를 수집한 시각인
`fetched_at`으로 대신 채우지 않습니다.

### Threads 원글 작성자 후속글 순서

`author_thread[]`는 GraphQL·continuation 요청에서 글을 발견한 순서가 아니라 원글 작성자의 게시
시각순으로 반환됩니다. 두 글의 출처 시각이 같거나 시각을 알 수 없으면 기존 발견 순서를 유지합니다.
이 정책은 전체 중첩 reply tree를 재구성하지 않으며, `comments[]`에는 정렬을 적용하지 않습니다.

---

## 무엇 위에 서 있나 — 소스와 크레딧

"검증된 도구를 묶는다"의 실체입니다. 플랫폼별로 어떤 코드를 쓰는지, 무엇이 저자가
직접 만든 것이고 무엇이 오픈소스인지 그대로 밝힙니다:

| 플랫폼 | 기반 | 자체 제작 / 수정 |
|---|---|---|
| **Threads** | [vdite/threads-scraper](https://github.com/vdite/threads-scraper) (MIT) fork | [우리 fork](https://github.com/stepbyjason-lab/threads-scraper)에서 3건 수정: 미디어 추출·다운로드, 대상 스레드 스코핑(추천 피드 오염 제거), fast/deep 티어 디스패처. sipher는 rich-text·원글 작성자 continuation·progress·정직한 partial 결과를 더하며, 전체 토론 수집은 명시 opt-in. |
| **YouTube** | [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) | 얇은 래퍼 — `--from-start`·라이브 채팅 replay 결선과 정규화만 자체 |
| **Facebook** | **저자가 직접 제작** | 라이트박스 우회 풀사이즈 사진·숨은 `+N`장·댓글 수집 — 공개 대체재가 없어 직접 만듦 |
| **Instagram** | [instaloader](https://github.com/instaloader/instaloader) (MIT) | 라이브러리 직접 호출 + 정직한 access 라벨 계층은 자체 |
| **TikTok** | [gallery-dl](https://github.com/mikf/gallery-dl) (GPL-2.0) | subprocess 경계로 호출(코드 비결합) |
| **X** | [gallery-dl](https://github.com/mikf/gallery-dl) (GPL-2.0) | TikTok과 같은 subprocess 경계 호출. 대화를 본문·저자 이어쓰기·인용 포스트·답글로 가르는 로직, 답글 상한, 쿠키 오류 처리는 자체 |
| **네이버 블로그** | **저자가 직접 제작** | 순수 표준 라이브러리(무의존) — 모바일 API + 원본 해상도 이미지 |
| **일반 웹** | [fivetaku/insane-search](https://github.com/fivetaku/insane-search) engine (MIT, 무수정 vendored) | Tier1(WAF 그리드·SSRF 방어)은 engine 그대로. Tier2 JS-render와 자동 승격은 자체 |

vendored 코드는 어댑터 폴더의 `_SOURCE.md`(출처·커밋 SHA·수정 내역)와
`LICENSE`(원본 전문)로 추적됩니다 — 상세는 [adapters/README.md](adapters/README.md).

---

## 출력 구조

소스가 무엇이든 정규화 스키마 하나:

```json
{
  "source": "...",
  "platform": "threads | youtube | facebook | instagram | tiktok | naver_blog | x | web | local",
  "body_text": "...",
  "comments": [ { "author": "...", "text": "...", "likes": 0 } ],
  "ocr_text": [ { "media_path": "...", "text": "..." } ],
  "transcript": "... 또는 null",
  "media_paths": [ "media/..." ],
  "meta": { "...": "정직 라벨 + 플랫폼 메타데이터" }
}
```

일부 플랫폼은 이 구조에 키를 더합니다. Threads와 X는 작성자가 이어 쓴 글 `author_thread[]`를,
X는 인용한 포스트 `quoted[]`도 돌려줍니다. X의 필드 전체(받은 파일과 실측 `width`/`height`를
담은 `meta.media_files` 포함)는 `adapters/x/docs/00-overview.md`를 참조하세요.

기본은 사람이 읽는 Markdown, `--json`으로 기계용 구조, `--out FILE`로 파일 저장.

---

## 빠른 시작

```bash
git clone <repo-url> sipher
cd sipher

# LITE(기본) 또는 FULL 프로필 — venv 생성 + 의존성 설치
scripts/setup.sh lite            # bash
scripts/setup.ps1 -Profile lite  # PowerShell

# 실행
.venv/bin/python -m core fetch "<URL 또는 파일 경로>"
# Windows: .venv\Scripts\python.exe -m core fetch "<URL 또는 파일 경로>"
```

**프로필**

| 프로필 | 어댑터 | 대상 |
|---|---|---|
| **LITE** | core + 네이버블로그 + YouTube + TikTok + web | 공개 콘텐츠 + 무료 OCR·전사(Groq 키만으로 GPU 없이). 개인 로그인 세션 불필요 — 공유 쉬움. |
| **FULL** | LITE + Threads + Facebook + Instagram + X + Whisper | 브라우저 로그인 세션(X는 로그인 쿠키)·GPU 필요. 개인용. |

의존성 매트릭스·시스템 요구사항(ffmpeg·선택 의존 ffprobe·Whisper·Playwright 브라우저)·API 키는
**[docs/08-packaging.md](docs/08-packaging.md)** 참조.

### X 로그인 쿠키

X는 로그인한 세션에만 대화를 내주므로 X 어댑터에는 쿠키가 필요합니다. x.com에 로그인한
브라우저에서 쿠키를 **넷스케이프 형식** `cookies.txt`로 내보낸 뒤, `.env.local`(또는 같은
이름의 환경변수)에 그 파일 경로를 적습니다. 상대경로는 저장소 루트 기준입니다.

```bash
# .env.local
X_COOKIES_FILE=/path/to/x_cookies.txt
```

```bash
python -m core fetch "https://x.com/someone/status/XXXX" --json
```

쿠키 파일은 로그인 세션 그 자체입니다 — 저장소 밖에 두고 절대 커밋하지 마세요.
`X_COOKIES_FILE`을 설정하지 않았거나, 가리키는 파일이 없거나, 쿠키가 만료됐으면
`AuthRequired` 오류로 끝납니다. X 다운로드는 `gallery-dl`(LITE에 이미 설치됨)을 쓰고,
시스템에 `ffprobe`가 있으면 받은 미디어의 실제 크기를 잽니다(`meta.media_files[].size_source`가
`probe`). 없으면 X가 선언한 크기를 쓰고 `size_source`는 `sidecar`입니다.

---

## 언어

파이프라인은 **언어 중립**이며 사용자에게 자동으로 맞춰집니다:

- **첫 실행 시 OS locale을 자동 감지**해 `.env.local`에 `SIPHER_LANG`으로
  저장합니다 — 언제든 직접 수정 가능(예: `SIPHER_LANG=en`, `ja`, `ko`).
- 비전 OCR과 Whisper 전사가 이 설정을 따릅니다. 한국어는 PoC 검증된 프롬프트,
  그 외 언어는 언어중립 프롬프트를 씁니다.
- CLI 도움말과 `.env.example`은 한/영 병기입니다. 내부 문서(docs/)는 현재
  한국어입니다(도구 자체는 어디서나 동작).

---

## 문서

| 문서 | 내용 |
|---|---|
| [docs/08-packaging.md](docs/08-packaging.md) | 패키징·프로필·설치·의존성 매트릭스 |
| [adapters/README.md](adapters/README.md) | 어댑터 목록·라이선스 |
| `adapters/*/docs/` | 어댑터별 상세 문서 |

---

## 라이선스

[MIT](LICENSE). vendored·third-party 컴포넌트는 각자 라이선스를 유지합니다 —
`LICENSE`의 *Third-party components* 절과 [adapters/README.md](adapters/README.md) 참조.
