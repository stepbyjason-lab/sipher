# sipher-threads 어댑터 — Overview

- **상태:** 구현(build) · **작성:** 2026-07-01 · **출처 원본:** `vdite/threads-scraper` fork(vendored, MIT)
- **정규화 계약:** `fetch(url) -> { source, platform, body_text, comments[], ocr_text[], transcript, media_paths[], meta }`
- **경계:** 어댑터는 수집·정규화만. 노트 합성=`note-factory`, 라우팅=`sipher`. sipher 내부 미-import(추출 가능).

---

## 1. 역할 (overview §5 라우팅 매트릭스)

| 원글 텍스트 | 댓글(중첩 스레드) | 이미지/영상 | 완전성 판단 |
|---|---|---|---|
| **body_text**(root post.text) | **comments[]** — naver_blog/facebook과 달리 실제로 채움 | `--download` 시 media_paths | fast pass vs deep 크롤 자동/수동 승격 |

Threads의 고유값은 **중첩 댓글**이다. naver_blog(댓글 API 범위 밖)나 facebook(댓글 미수집 설계)과
달리, threads는 vendored 스크래퍼가 이미 reply 트리를 추적하는 재귀 크롤러를 갖고 있어
`comments[]`를 실질적으로 채운다.

## 2. 핵심 결정 (2026-07-01 확정)

- **새 스크래퍼를 만들지 않는다 — vendoring.** `vdite/threads-scraper`(MIT)를 사용자가
  fork해 3가지 개선(미디어 다운로드, relatedPosts 오염 fix, 티어 디스패처)을 이미 얹어놨다.
  이 세 커밋이 코어 가치이므로 scratch 재작성보다 이식이 압도적으로 저렴하다.
- **스크래퍼 로직 리팩터 금지.** 이식 시 내부 import만 패키지 상대 import로 수정했다
  (`import media_utils` → `from . import media_utils`). 파싱·크롤 전략·휴리스틱은 원본 그대로.
- **티어 디스패처를 그대로 노출.** `fetch(deep=False)`(기본)는 fast pass(~10초), 실제로
  불완전해 보이면 `auto=True`로 자동 승격하거나 `deep=True`로 처음부터 재귀 크롤 강제.
  fast/deep 선택 기준은 vendored `scrape.assess()`의 reply_count 휴리스미틱 그대로 사용.
- **comments[]는 flat list, 순서·트리 구조 비보존.** vendored 스크래퍼는 결과를
  `{id: post}` map으로 수집한다(원본이 이미 dict 병합 방식) — 중첩 depth/parent-child
  관계 자체는 원본 스키마에 없다. root(`code` 일치)를 body_text로 분리하고 나머지를
  댓글로 채우되, 각 댓글의 `reply_count`(그 댓글에 달린 대댓글 수)는 보존해 상위 계층이
  필요 시 재구성할 단서를 남긴다.
- **미디어는 opt-in.** `--download` 없이는 URL만 카운트(image_count/video_count),
  `media_paths`는 빈 리스트. CDN URL은 서명·시간제한(`media_utils.py` 원본 docstring 근거)이라
  다운로드는 스크랩 직후에만 유효.
- **인증(쿠키) 필요 여부:** 공개 계정의 공개 포스트는 쿠키 없이도 fast_scrape가 동작(원본
  README 기준 비로그인 크롤 지원). 비공개 계정/연령제한/rate-limit 회피에는 `threads_scraper_v2.py`의
  `--login` 쿠키 저장 플로우가 필요 — 어댑터는 이 쿠키 파일(`threads_cookies.json`, vendored
  스크래퍼가 자기 디렉토리에서 찾음)이 있으면 자동으로 쓰고 없으면 비로그인으로 시도한다.

## 3. 모듈 구조

```
adapters/threads/
├── docs/00-overview.md
├── LICENSE                 # 원본 MIT 전문(© 2026 vdite) — vendoring 의무
├── _SOURCE.md              # 출처 스탬프(upstream·fork·이식 커밋 3건·이식일)
├── __init__.py             # 공개 API: fetch · parse_url · normalize (vendored 스크래퍼 위임)
├── scrape.py               # [vendored] 티어 디스패처: fast pass → 불완전 시 deep 크롤
├── fast_scrape.py          # [vendored] 단일 패스: 임베디드 JSON + graphql 응답 수집
├── threads_scraper.py      # [vendored] 재귀 크롤 v1(레거시, scrape.py가 v2를 씀)
├── threads_scraper_v2.py   # [vendored] 재귀 크롤 v2: 로그인 쿠키·진행률 바·show-more 클릭
├── media_utils.py          # [vendored] 미디어 추출·다운로드 + iter_thread_posts(피드 오염 fix)
├── cli.py                  # python -m adapters.threads.cli fetch <URL> [...]
└── requirements.txt
```

## 4. 보안·견고성

- **인자 인젝션/SSRF 차단:** `parse_url`이 threads.net/threads.com 호스트만 통과시키고,
  `@author/post/code` 경로에서 안전한 문자 집합(`[A-Za-z0-9_-]`)의 code만 추출한다.
  code 없는 프로필/홈 URL은 거부(ValueError) — youtube 어댑터의 "호스트 화이트리스트 +
  정규식 식별자 추출" 패턴과 동일. author에는 추가로 `..`/앞뒤 `.`를 거부한다(defense-in-depth).
- **canonical URL 재구성:** `fetch()`는 `parse_url`이 검증한 author/code로 canonical URL을
  재구성해 스크래퍼에 넘긴다(원본 url을 playwright goto로 그대로 넘기지 않음). query/fragment는
  이 재구성으로 자연히 제거된다. `normalize(..., source=url)`은 provenance를 위해 원본 url을
  그대로 보존한다.
- **media_dir는 신뢰 입력:** media_dir/max_pages는 로컬 사용자가 지정하는 신뢰 입력이다 —
  어댑터는 경로 containment를 하지 않는다(youtube 어댑터와 동일한 경계 원칙, 기존 유지).
- **playwright 지연 로드:** `parse_url`/`normalize`는 playwright 없이 import·테스트
  가능. 실제 `fetch()` 호출 시에만 vendored 스크래퍼가 playwright를 기동한다.
- **graceful degradation:** vendored 스크래퍼는 개별 페이지 실패를 삼키고(`except Exception:
  pass`) 계속 진행하는 방어적 설계다(원본 그대로 유지) — 어댑터 레벨에서 이를 감추지 않고
  `meta.completeness`(root_found/expected/captured/incomplete)로 정직하게 노출한다.

## 5. 정직 라벨

`meta.media_label`: `none`(미다운로드 또는 다운로드했지만 미디어 자체가 없음) ·
`downloaded`(전체 성공) · `partially_downloaded`(일부만 성공 — 다운로드된 수 < 전체
이미지/영상 수) · `download_failed`(미디어는 있는데 전부 다운로드 실패 — CDN 서명 만료 등)

`meta.media_complete`: `true`는 전체 미디어 개수와 실제 다운로드된 개수가 일치하고
1개 이상 존재할 때만. `media_label`이 `partially_downloaded`/`download_failed`/`none`이면
항상 `false`.

`meta.cookies_available`: vendored 스크래퍼가 사용하는 쿠키 파일(`threads_cookies.json`,
`fast_scrape.COOKIE_FILE`/`threads_scraper_v2.COOKIE_FILE`가 동일 경로 참조)의 존재 여부.
`false`면 비공개 계정/연령제한 포스트에서 fast pass가 실패하거나 불완전할 수 있다는 신호.

`meta.completeness`: vendored `scrape.assess()` 결과 + 어댑터가 덧붙인 필드 — `root_found` ·
`expected`(root의 reply_count) · `captured`(실제 수집된 댓글 수) · `incomplete`(captured <
expected) · `scrape_mode`(`"fast"` 또는 `"deep"`) · `max_pages`(deep일 때만, 크롤 상한).
fast pass만 돌렸는데 incomplete=True면 `--deep` 또는 `--auto` 재시도를 권장하는 신호.
deep 크롤도 `max_pages`로 절단될 수 있으므로 `incomplete=False`가 "완전 수집"을 보장하지
않는다 — 최소한 몇 페이지까지 돌았는지는 `max_pages`로 확인 가능.

**root 포스트 미발견 시 실패 표면화:** `fetch()`는 `assessment.root_found`가 False면
`RuntimeError`를 raise한다(스크랩 완전 실패가 exit 0 빈 dict로 위장되지 않는다). 원인은
네트워크 오류, 쿠키 만료, 차단, 잘못된 URL 등일 수 있다. CLI는 이를 exit 1로 표면화한다.

**⚠️ 알려진 벤더 이슈(수정 안 함):** 벤더 `scrape.py`의 `run_deep()`/`main()`이
`max_pages=100`을 하드코딩한다 — 벤더 CLI(`python scrape.py <url> --deep`)를
어댑터 경유 없이 직접 실행하면 max_pages 지정이 불가하다. sipher 어댑터
경유(`fetch()`/`_run_scrape`)는 벤더의 `run_deep()`을 우회하므로 영향 없음(round-02
픽스). 벤더 무수정 원칙상 코드는 고치지 않는다. 상세: `_SOURCE.md`.

## 6. CLI

```
python -m adapters.threads.cli fetch <URL>
    [--media-dir DIR]     # 다운로드 대상(기본 downloads), --download와 함께
    [--deep]              # fast pass 생략, 재귀 크롤부터
    [--auto]              # fast pass 불완전 시 자동 deep 승격
    [--download]          # 이미지/영상 다운로드
    [--max-pages N]       # deep 크롤 최대 페이지 수(기본 100)
```

## 7. 의존성

- **필수:** `playwright`(BSD-3, 원본 의존성 — `playwright install chromium` 별도 필요),
  `parsel`(BSD-3, 임베디드 JSON `<script>` XPath 추출).

## 8. 라이선스·출처 (Attribution)

이 어댑터는 **외부 코드를 이식(vendoring)한다** — youtube 어댑터(런타임 의존만)와 달리
`vdite/threads-scraper`(MIT, © 2026 vdite)의 소스 5개 파일을 복사해 sipher 패키지 안에
포함시켰다. MIT는 재배포 시 저작권 고지 + permission notice 동봉을 요구하므로,
**`adapters/threads/LICENSE`에 원본 MIT 전문을 그대로 동봉**했다(코드 포함이라 표시 의무).

| 항목 | 내용 |
|---|---|
| upstream | https://github.com/vdite/threads-scraper (MIT, © 2026 vdite) |
| fork(사용자) | https://github.com/stepbyjason-lab/threads-scraper — 미디어 다운로드(`983f563`)·relatedPosts fix(`2d282e7`)·티어 디스패처(`f6a19ef`) 3커밋 추가 |
| 이식 방식 | vendored as-is, 내부 import만 상대 import로 수정(로직 미변경) |
| 이식일 | 2026-07-01 |
| 상세 | `_SOURCE.md` 참조 |

- **데이터 약관(정직 고지):** Threads를 공식 API가 아닌 페이지 렌더링/GraphQL 응답
  스크래핑으로 수집한다 — Threads(Meta) ToS 회색지대. youtube 어댑터의 overview §7과
  동일하게 "개인용 적합" 전제 위에서 사용한다. 대량/상업적 스크래핑, rate-limit 우회,
  비공개 계정 무단 접근은 이 어댑터의 의도된 사용 범위 밖이다.
- **원본 레포는 읽기 전용으로 유지:** `threads-scraper`는 fork의 로컬 미러이며
  이 이식 작업으로 수정하지 않았다. 향후 fork에 업스트림 변경이 반영되면 그 시점에
  수동으로 재-vendor(diff 확인 후 재복사 + `_SOURCE.md` 커밋 SHA 갱신) 필요.
