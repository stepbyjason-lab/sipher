# 08. Packaging — Sipher

- **상태:** 정본(round-15) · **작성일:** 2026-07-02
- **관계:** `docs/01-overview.md` §10 "패키징(공유)"의 프로필 개념을 실제
  산출물(requirements-lite/full.txt, setup/make-dist 스크립트)로 구체화한
  **정본**이다. §10과 이 문서가 다르게 서술하면 이 문서가 정본(§10에는
  포인터 1줄만 유지).

---

## 1. 프로필 정의

두 프로필 모두 **같은 코드 트리**를 공유한다. 차이는 (a) 설치할
`requirements-*.txt`와 (b) 로그인 세션 셋업 여부뿐이다.

경계 축: **개인 로그인 세션 필요 여부**(`docs/01-overview.md` §10의 "키·쿠키는
못 피함" 정신을 이 라운드에서 재해석·확정).

| 프로필 | 정의 | 대상 |
|---|---|---|
| **LITE** | core + naver_blog + youtube + tiktok + web. 공개 콘텐츠 + 무료 API OCR(Gemini 키). 개인 로그인 세션 불필요. | 팀원·지인 — 공유 쉬움 |
| **FULL** | LITE + threads + facebook + instagram + local whisper 전사. 브라우저 로그인 세션·GPU 필요. LITE도 `GROQ_API_KEY`만 있으면 무료 Groq Whisper로 전사 가능(round-27, local 불필요). | 본인/신뢰 대상(local whisper) — 전사만이면 LITE + Groq 키로도 가능 |

## 2. 의존성 매트릭스 (실측)

| 어댑터 | 서드파티 의존성 | 개인 로그인 세션 | 프로필 |
|---|---|---|---|
| core | markitdown(옵션), **requests**(필수 — llm_free OCR) | — | 공통 |
| naver_blog | 없음(순수 stdlib `urllib`, requirements.txt 파일 자체가 없음) | 불필요 | LITE |
| youtube | yt-dlp(필수) + 옵션 transcript/comments API, ffmpeg(시스템) | 불필요 | LITE |
| tiktok | gallery-dl(subprocess 호출) | 불필요 | LITE |
| web | curl_cffi(tier1), pyyaml, playwright(tier2) | 불필요(로그인 없음) | LITE |
| threads | playwright, parsel | deep 크롤 시 권장 | FULL |
| facebook | playwright(+옵션 browser_cookie3) | 필수 | FULL |
| instagram | instaloader | 필수(익명 접근 대부분 403) | FULL |
| 전사(`core/transcribe.py`) | local: 시스템 도구(subprocess), GPU large-v3 권장 / Groq 폴백(round-27): **requests**(공통 의존에 이미 포함, SDK 신규 없음) | — | local=FULL 권장(옵션), Groq=LITE도 `GROQ_API_KEY`만 있으면 가능 |

## 3. setup 스크립트 사용법

```bash
# bash
scripts/setup.sh [lite|full] [--browsers]

# PowerShell
scripts/setup.ps1 [-Profile lite|full] [-Browsers]
```

- 기본 프로필은 `lite`.
- `.venv/`를 생성(이미 있으면 재사용 — 파괴하지 않음)하고 `requirements-<profile>.txt`를 설치한다.
- `full` 프로필이거나 `--browsers`/`-Browsers` 플래그가 있으면
  `python -m playwright install chromium`을 시도한다(실패해도 setup 자체는
  성공 처리 — 안내만).
- 종료 시 "필요한 것" 체크리스트(GEMINI_API_KEY, ffmpeg, whisper, 로그인 세션 등)를 출력한다.
- fail-fast: 잘못된 profile 인자·python 미발견 시 명확한 에러 메시지 + exit 1.

## 4. 시스템 의존성 (pip manifest로 표현 불가)

| 도구 | 용도 | 필요 어댑터 |
|---|---|---|
| ffmpeg | 포맷 병합·자막 변환 | youtube |
| whisper 계열 전사 도구(GPU large-v3 권장) | 음성 전사(local backend, 최우선) | core/transcribe.py(FULL 권장, 없으면 Groq 폴백) |
| ffmpeg(선택, Groq 경로) | 25MB 초과/영상 컨테이너를 오디오만 추출해 Groq 업로드(round-27) | core/transcribe.py — 없으면 해당 아이템 정직 skip |
| playwright chromium 브라우저 바이너리 | 헤드리스 브라우저 크롤 | threads, facebook, instagram(간접), web tier2 |

## 5. API 키 / 로그인 세션

| 항목 | 용도 | 필수 여부 |
|---|---|---|
| `GEMINI_API_KEY` | 무료비전 OCR(`core/llm_free.py`) | `--ocr` 옵션 사용 시 |
| `NVIDIA_NIM_API_KEY` | OCR 앙상블(round-24) 후보/judge용 무료 provider. https://build.nvidia.com (카드 불필요). 없으면 Gemini 단독 degrade. **OCR 신뢰성 백업은 멀티계정이 아니라 이 멀티-provider 앙상블로** — 각 provider 약관 내 | 선택(앙상블) |
| `GROQ_API_KEY` | 무료 전사 폴백(round-27, `core/transcribe.py`). local whisper가 없거나 개별 아이템에서 실패했을 때만 사용 — `whisper-large-v3-turbo`→`whisper-large-v3`(429 시) 순. https://console.groq.com (카드 불필요). **단일 키** 안에서 Groq가 제공한 모델별 무료 버킷만 사용(ToS 내, 멀티계정/멀티키 우회 아님) | 선택(local 없을 때 대체) |
| threads 로그인 세션 | deep 크롤 시 안정성 향상 | 권장(FULL) |
| facebook 로그인 세션(persistent context/cookies) | 인증 콘텐츠 접근 | 필수(FULL) |
| instagram 로그인 세션 | 익명 접근이 거의 항상 403 | 사실상 필수(FULL) |

키는 루트 `.env.local`(gitignore 대상)에 설정한다. setup 스크립트는 **키 값을
로그·파일에 출력하지 않으며**, 필요성 안내만 한다.

## 6. Graceful Degradation 표

도구가 없어도 sipher는 막지 않고 정직 라벨로 degrade한다(`docs/01-overview.md` §10 원칙):

| 기능 | 도구 없을 때 라벨 | 코드 위치 |
|---|---|---|
| 로컬 문서 변환(pdf/docx 등) | `meta.conversion_label = "skipped_no_tool"` | `core/local.py`, `core/markitdown_local.py` |
| 무료비전 OCR | `meta.ocr_label`(provider 키 없으면 미호출) | `core/normalize.py` |
| 전사(local whisper → Groq 폴백, round-27) | `meta.transcript_label = "skipped_no_tool"`(둘 다 없음) — 있으면 `meta.transcript_backend`에 `"local"`/`"groq"` 표기 | `core/normalize.py`, `core/transcribe.py` |
| facebook 풀사이즈 이미지 회수 | `meta.fullsize_label` | `adapters/facebook/__init__.py` |
| instagram 접근 실패 | `InstagramAccessError.access_label`(예: `"anonymous_blocked"`) | `adapters/instagram/__init__.py` |
| facebook 댓글(옵트인 안 함/추출 실패) | `meta.comments_label`: `not_collected`(기본) / `fetch_failed`(추출 실패, `none`=댓글0과 구분, round-16) | `adapters/facebook/__init__.py`, `scrape.py` |

값 taxonomy 전체 정본은 `docs/04-architecture.md` §4.4(API/데이터 계약) 참조.

## 7. 공개 배포판 빌드 절차

```bash
# bash
scripts/make-dist.sh

# PowerShell
scripts/make-dist.ps1
```

- `git archive --worktree-attributes --format=tar.gz -o dist/sipher-<rev>.tar.gz HEAD`로
  **추적 파일만** 담는다 — `.gitignore`된 `.env.local`/`.fbprofile/`/`.fbmedia/`는 애초에
  git 추적 대상이 아니므로 이 시점에 이미 미포함. `--worktree-attributes`는 작업트리의
  `.gitattributes`(export-ignore)까지 적용해 커밋 전/후 모두 제외를 보장하는 안전장치다.
- 단 `.handoff/`는 **git 추적 대상**이라 별도 장치가 필요하다. `.gitattributes`가
  다음을 `export-ignore`로 선언한다:
  ```
  .handoff/ export-ignore
  .gitattributes export-ignore
  scripts/make-dist.sh export-ignore
  scripts/make-dist.ps1 export-ignore
  ```
  `git archive`는 `export-ignore` 항목을 tarball에서 자동 제외한다.
- **왜 제외하는가**: `.handoff/`에는 개인 수집 도구의 레포명·개인정보 이력이
  잔존한다(round-13이 앞면 파일만 일반화, 과거 이력 자체는
  git history에 남음). 공개 배포판에는 절대 포함되면 안 된다.
- make-dist 스크립트는 선언을 **믿기만 하지 않고 실제로 검증**한다 —
  산출된 tarball 내용을 `tar tzf`로 나열해 `.handoff/` 항목 개수를 세고,
  0이 아니면 tarball을 삭제하고 실패(exit 1) 처리한다. 이것이 "공개 제외"
  요구의 실행 메커니즘이자 안전장치다.
- `dist/`는 `.gitignore`에 포함되어 있어 빌드 산출물이 커밋되지 않는다.

## 8. 비목표 (명시)

- `pyproject.toml` / pip-installable 단일 패키지 / `setup.py` / Docker —
  `docs/01-overview.md` §10 "모놀리식 번들 ❌" 원칙 + 미논의(YAGNI).
- 실제 공개 push / 레포 공개 전환 — 이번 범위는 dist 빌드까지.
- CI/CD·GitHub Actions·릴리스 자동화.

## 9. LICENSE — MIT (round-18 확정)

루트 `LICENSE` = **MIT**(Copyright (c) 2026 stepbyjason-lab). 사용자가 사용한
다른 도구들(vendored threads=MIT, web-engine=MIT, instaloader=MIT, markitdown=MIT,
yt-dlp=Unlicense)과 호환. `gallery-dl`(GPL-2.0)은 subprocess 호출만 하므로 전파
없음(`adapters/tiktok/requirements.txt`·`adapters/README.md` §라이선스 원칙 참조).

- 루트 `LICENSE`는 **sipher 자체 코드**를 커버한다.
- vendored/third-party 컴포넌트는 각자 라이선스 유지(`adapters/threads/LICENSE`,
  `adapters/web/engine/LICENSE` 원본 고지 보존). LICENSE 파일 §Third-party components에 명시.
