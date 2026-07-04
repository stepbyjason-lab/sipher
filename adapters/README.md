# Sipher Adapters — 독립 스크래퍼 도구 모음

이 디렉토리의 각 폴더는 **sipher와 별개로도 동작하는 독립 스크래퍼 도구**다. sipher 라우터가 어댑터로 호출하지만, 각각 자기 `docs/` · CLI 진입점 · 의존성을 가진 자급자족 프로젝트로 취급한다.

> 설계 의도: 지금은 sipher 한 레포 안에 통합(솔로 운영 + submodule 위생 회피)하되, 각 어댑터는 sipher 내부를 import 하지 않는 **깨끗한 경계**(`fetch(url) -> 정규화 JSON`)를 유지한다. 나중에 FB 어댑터가 standalone 공개 가치가 검증되면 `git subtree split`로 별도 레포 추출이 거의 공짜가 되도록.

## 어댑터 목록

| 어댑터 | 출처(이식 원본) | 고유값 | 상태 |
|---|---|---|---|
| [`facebook/`](facebook/) | **저자가 직접 만든** 자작 수집 도구(비공개 사적 레포 — 제3자 오픈소스 아님, 라이선스 의무 없음) `fb_scrape_playwright.py` + `fb_image_refetch.py` + `fb_video_refetch.py` | 라이트박스 우회 풀사이즈 사진 + 숨은 +N장 + 영상 capture (공개 대체재 없음) | 설계(design) |
| [`naver_blog/`](naver_blog/) | **저자가 직접 만든** 자작 수집 도구(위와 동일, 사적 레포 자기 이식) `naver_blog_scrape.py` | 모바일 API 목록 + 본문 + 메타 + (보완) 원본 이미지 | 설계(design) |
| [`youtube/`](youtube/) | vendored 없음 — [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp)(pip, Unlicense) 얇은 래핑 | 설명란·메타·미디어 + `--from-start`(라이브 처음부터) + 라이브 채팅 replay + (옵션)전사·댓글. **MCP 비의존, 100% pip(Unlicense/MIT)** | 구현(build) |
| [`threads/`](threads/) | [vdite/threads-scraper](https://github.com/vdite/threads-scraper) fork(vendored, MIT) — [우리 fork](https://github.com/stepbyjason-lab/threads-scraper) | **중첩 댓글**(comments[] 실제로 채움) + 미디어 다운로드 + 티어 디스패처(fast pass→불완전 시 deep 크롤 자동 승격) | 구현+라이브 검증(round-02 Gate5 PASS_WITH_FOLLOWUPS) |
| [`instagram/`](instagram/) | vendored 없음 — [instaloader/instaloader](https://github.com/instaloader/instaloader)(pip, MIT) 직접 호출 | 캡션·미디어·메타 + **로그인 세션 사실상 필수**(round-10 정정, IG가 익명 접근을 거의 항상 403 차단) + `InstagramAccessError.access_label`로 정직 판별 | 구현(round-09) → 로그인 필수 재포지셔닝(round-10 §④) |
| [`tiktok/`](tiktok/) | vendored 없음 — [mikf/gallery-dl](https://github.com/mikf/gallery-dl)(pip, **GPL-2.0**) subprocess 직접 호출(경계라 전파 없음, §라이선스 원칙 참조) | 캡션(desc)+통계+메타, 영상 다운로드 opt-in | 구현+라이브 검증(round-09) → 라이선스 표기 보완(round-10 §⑤) |
| [`web/`](web/) | `web/engine/`만 vendored — [fivetaku/insane-search](https://github.com/fivetaku/insane-search)(MIT) | 6플랫폼 host 미매칭 시 **범용 폴백**. Tier1(curl_cffi WAF 그리드, engine 내장 SSRF 방어) + Tier2(Python playwright JS-render, SSR 껍데기 의심 시 자동 승격) | 구현+라이브 검증(round-10) |

## 공통 규약

- **출력:** sipher 정규화 스키마 1종 — `{ source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }`
- **경계:** 어댑터는 수집·정규화만. 노트 합성은 `note-factory`(다운스트림), 라우팅은 `sipher`.
- **개발 절차:** 마디(`madi`) 게이트 준수 — 만들기 전(선행 설계 floor) → 만드는 중(구현 통제) → 만든 후(리뷰 수렴). 소스 변경은 Gate 4(Implementation Start) 승인 후.

## 라이선스 원칙 (round-10 §⑤ 명문화)

세 가지 소싱 방식이 있고, 각각 라이선스 의무가 다르다:

1. **Vendoring(소스 코드 복사)** — threads(MIT,
   [vdite/threads-scraper](https://github.com/vdite/threads-scraper)), web(MIT,
   [fivetaku/insane-search](https://github.com/fivetaku/insane-search)). 원본
   라이선스가 재배포 시 저작권 고지·permission
   notice 동봉을 요구하면(MIT류) `adapters/<name>/LICENSE`(전문)와
   `adapters/<name>/_SOURCE.md`(출처·커밋 SHA·이식일 스탬프)를 반드시 둔다.
2. **pip 라이브러리 직접 호출(import 또는 subprocess)** — youtube
   ([yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp), Unlicense/MIT), instagram
   ([instaloader/instaloader](https://github.com/instaloader/instaloader), MIT),
   tiktok([mikf/gallery-dl](https://github.com/mikf/gallery-dl), **GPL-2.0**).
   라이브러리 자체를 vendor하지 않으므로(사용자가 자기 pip 환경에 별도 설치)
   재배포 의무가 없다 — `requirements.txt`에 라이선스와 버전 핀만 명시하면
   충분하다. GPL 라이브러리라도 **subprocess(별도 프로세스) 호출은 동일
   프로세스 결합이 아니므로 GPL 전파 조건(파생 저작물)에 해당하지 않는다는
   것이 통설**이다(tiktok/gallery-dl 케이스, round-09 리뷰 P3 팔로우업).
   Python import로 직접 호출하는 경우(instagram/instaloader)는 MIT라 애초에
   전파 이슈가 없다.
3. **사적 레포 자기 이식** — facebook, naver_blog(둘 다 **저자가 직접 만든**
   자작 수집 도구(비공개) 사적 레포에서 이식). 제3자 오픈소스가
   아니라 **자기 자신의 이전 작업물**이므로 라이선스 의무 자체가 발생하지
   않는다(제3자 저작권이 개입하지 않음) — `_SOURCE.md`/`LICENSE`가 불필요한
   유일한 "이식" 케이스.

## 비목표 (어댑터 공통)

- 인물 단위 영구 아카이브 · 통합 갤러리 · 검색 UI · CLIP 임베딩 → **자작 수집 도구에 잔류** (이식 대상 아님)
- 노트/콘텐츠 합성 → `note-factory`
