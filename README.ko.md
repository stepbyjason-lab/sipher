<!-- 언어: 한국어 · [English](README.md) -->

# Sipher

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

- **인터페이스 하나, 모든 소스.** 6개 플랫폼 + 범용 웹 폴백 + 로컬 파일이 전부
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
| 이미지 OCR | **무료 앙상블**: `gemini-2.5-flash` + `google/gemma-4-31b-it` + `nvidia/nemotron-nano-12b-v2-vl`(NVIDIA NIM) 후보를 무료 judge(`gemma-4`)가 교차검증. 한국어 카드 실측에서 단일 모델보다 정확 | 무료 티어 |
| 음성/영상 전사 | **로컬 우선**: faster-whisper `large-v3` → **무료 폴백**: Groq `whisper-large-v3-turbo`(한도 시 `whisper-large-v3`). 영상은 ffmpeg로 오디오만 추출해 업로드 | 로컬 / 무료 티어 |
| 유료 폴백 | `claude-sonnet-4-5` — `OCR_PAID_FALLBACK=claude`로 **직접 켜야만** 동작 | 옵트인 |

- 무료 한도가 소진되면 조용히 과금되는 대신 **정직한 skip/degrade 라벨**을 남깁니다.
- NVIDIA NIM 키는 [build.nvidia.com](https://build.nvidia.com)에서 카드 등록 없이 무료 발급.

---

## 무엇을 할 수 있나

| 소스 | 얻는 것 |
|---|---|
| **Threads** | 본문, **중첩 댓글**, 미디어. Fast pass → 불완전하면 deep 크롤로 자동 승격. |
| **YouTube** | 설명·메타·미디어, `--from-start`(라이브 처음부터), 라이브 채팅 replay, (옵션) 자막·댓글. |
| **Facebook** | 본문, **풀사이즈 사진**(라이트박스 우회 + 숨은 `+N`장), 영상, **댓글 본문**(정직한 신뢰도 라벨). |
| **Instagram** | 캡션·미디어·메타. 로그인 세션 필요(익명 접근 차단) — access 라벨로 정직 보고. |
| **TikTok** | 캡션·통계·메타, (옵션) 영상 다운로드. |
| **네이버 블로그** | 모바일 API 목록 + 본문 + 메타 + 원본 해상도 이미지. (순수 표준 라이브러리 — 무의존.) |
| **일반 웹 아티클** | 6개 플랫폼에 안 걸리는 모든 것의 범용 폴백. 2-tier: 빠른 정적 fetch → SSR 껍데기면 JS 렌더 브라우저. SSRF 방어 내장. |
| **로컬 파일** | PDF/DOCX/PPTX/XLSX/CSV/이미지/음성/영상 → 문서 변환 + OCR + 전사로 텍스트화. |

### 보강 (opt-in)

- `--ocr` — 이미지 속 텍스트 추출. 기본은 **무료 멀티-provider 앙상블**(Gemini +
  NVIDIA NIM 후보를 무료 judge가 교차검증) — 한국어 카드 실측에서 단일 모델보다 정확.
  NIM 키 없으면 Gemini 단독으로 degrade.
- `--transcribe` — 음성/영상 전사. 로컬 Whisper 우선, 없거나 실패하면 **무료 Groq
  Whisper로 자동 폴백** — GPU 없는 머신도 Groq 키 하나로 전사 가능.

---

## 무엇 위에 서 있나 — 소스와 크레딧

"검증된 도구를 묶는다"의 실체입니다. 플랫폼별로 어떤 코드를 쓰는지, 무엇이 저자가
직접 만든 것이고 무엇이 오픈소스인지 그대로 밝힙니다:

| 플랫폼 | 기반 | 자체 제작 / 수정 |
|---|---|---|
| **Threads** | [vdite/threads-scraper](https://github.com/vdite/threads-scraper) (MIT) fork | [우리 fork](https://github.com/stepbyjason-lab/threads-scraper)에서 3건 수정: 미디어 추출·다운로드, 대상 스레드 스코핑(추천 피드 오염 제거), fast→deep 티어 디스패처 |
| **YouTube** | [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) | 얇은 래퍼 — `--from-start`·라이브 채팅 replay 결선과 정규화만 자체 |
| **Facebook** | **저자가 직접 제작** | 라이트박스 우회 풀사이즈 사진·숨은 `+N`장·댓글 수집 — 공개 대체재가 없어 직접 만듦 |
| **Instagram** | [instaloader](https://github.com/instaloader/instaloader) (MIT) | 라이브러리 직접 호출 + 정직한 access 라벨 계층은 자체 |
| **TikTok** | [gallery-dl](https://github.com/mikf/gallery-dl) (GPL-2.0) | subprocess 경계로 호출(코드 비결합) |
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
  "platform": "threads | youtube | facebook | instagram | tiktok | naver_blog | web | local",
  "body_text": "...",
  "comments": [ { "author": "...", "text": "...", "likes": 0 } ],
  "ocr_text": [ { "media_path": "...", "text": "..." } ],
  "transcript": "... 또는 null",
  "media_paths": [ "media/..." ],
  "meta": { "...": "정직 라벨 + 플랫폼 메타데이터" }
}
```

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
| **FULL** | LITE + Threads + Facebook + Instagram + Whisper | 브라우저 로그인 세션·GPU 필요. 개인용. |

의존성 매트릭스·시스템 요구사항(ffmpeg·Whisper·Playwright 브라우저)·API 키는
**[docs/08-packaging.md](docs/08-packaging.md)** 참조.

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
