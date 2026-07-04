# 출처 (vendoring 스탬프)

- **upstream:** https://github.com/vdite/threads-scraper (MIT License, © 2026 vdite)
- **fork:** https://github.com/stepbyjason-lab/threads-scraper
- **local upstream mirror:** `threads-scraper` (읽기 전용 원본 — sipher에서 수정 금지)
- **이식 커밋 (fork에서 원본 위에 추가된 사용자 수정 3건):**
  - `983f563` — feat: extract and download image/video media (`media_utils.py` + `--download` 플래그)
  - `2d282e7` — fix: scope extraction to the target thread, drop relatedPosts feed (`iter_thread_posts`가 추천 피드 오염을 제거)
  - `f6a19ef` — feat: add tiered dispatcher (`scrape.py` — fast pass 우선, 불완전 시 deep crawl로 에스컬레이션)
- **이식일:** 2026-07-01
- **이식 방식:** vendored as-is — 스크래퍼 로직(파싱·크롤 전략)은 리팩터하지 않고 그대로 복사, 내부 import만
  패키지 상대 import(`from . import media_utils` 등)로 수정. submodule이 **아님**(sipher는 git 미초기화 —
  `git submodule`을 쓸 수 없어 vendoring이 유일한 선택지이기도 함).
- **vendored 파일:** `scrape.py`(티어 디스패처) · `fast_scrape.py`(단일 패스) ·
  `threads_scraper.py`(재귀 크롤 v1) · `threads_scraper_v2.py`(재귀 크롤 v2, 로그인/쿠키 지원) ·
  `media_utils.py`(미디어 추출·다운로드 + `iter_thread_posts` 피드 필터)
- **라이선스 의무:** MIT는 재배포 시 저작권 고지 + permission notice 동봉을 요구 →
  `adapters/threads/LICENSE`에 원본 MIT 전문(© 2026 vdite) 동봉 완료.

## 알려진 벤더 이슈 (수정하지 않음 — 문서 경고만)

- **`scrape.py`의 `run_deep()`(L50-52)과 `main()`(L67-68, L78-79)이 `max_pages=100`을
  하드코딩**한다. 벤더 CLI(`python scrape.py <url> --deep`)를 어댑터 경유 없이 직접
  실행하면 `--max-pages` 상당의 옵션이 없어 사용자가 max_pages를 지정할 방법이 없다.
- **sipher 어댑터 경유(`adapters/threads/__init__.py:_run_scrape`)는 영향 없음.** `_run_scrape`가
  벤더의 `run_deep()`을 호출하지 않고 `scrape_threads_recursive(url, max_pages=)`를 직접
  호출해 이 하드코딩을 우회한다(round-02 픽스, Gate5 PASS_WITH_FOLLOWUPS).
- **벤더 무수정 원칙 때문에 코드는 고치지 않는다** — 이 경고는 향후 누군가 벤더
  `scrape.py`를 CLI로 직접 실행하거나 `run_deep()`을 재사용할 때 동일 버그가
  재현됨을 미리 알리기 위한 문서 표기다.
- 출처: `.handoff/rounds/round-02-core-router-review.md` P2 finding, Deferred Follow-Up 1.
