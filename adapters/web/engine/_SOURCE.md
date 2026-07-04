# 출처 (vendoring 스탬프)

- **upstream:** https://github.com/fivetaku/insane-search.git (MIT License, © 2026 fivetaku)
- **vendored subfolder:** `skills/insane-search/engine/`(리포 루트 기준 상대경로) —
  리포 전체가 아니라 `engine/` 서브디렉토리만 이식.
- **클론 커밋 SHA:** `3a2f6c8563c2a5c329c586f225ba68b0241ff227`
  (`docs(readme): add MIT License section to all 5 language READMEs`, 2026-06-28)
- **설치일:** 2026-07-02
- **이식 방식:** vendored as-is — **무수정 원칙**. `engine/` 하위 전체 파일(`.py` ·
  `waf_profiles.yaml` · `templates/` · `tests/`)을 그대로 복사, `__pycache__`만 제외.
  코드 리팩터·로직 변경 없음(`diff -rq` 로 소스와 바이트 동일 확인 완료, round-10
  result에 기록).
- **가져오지 않은 것(계약대로 제외):** 리포 루트의 `setup/`(설치 스크립트) ·
  `SKILL.md`(Claude Skill 래퍼) · `references/` · 다국어 `README.*.md` · `assets/`
  — sipher는 `engine/`의 순수 코드 경로(`--no-playwright` 상당, Python import)만
  사용하므로 스킬/설정 계층은 불필요.
- **호출 방식:** sipher `adapters/web/__init__.py`가 `adapters.web.engine`을 Python
  `import`로 직접 호출(`from .engine import fetch as engine_fetch`). subprocess/CLI
  경유 아님 — engine의 `--json` CLI 출력은 본문(content)을 의도적으로 생략하므로
  (`FetchResult.to_dict()`가 `content_length`만 노출), 본문을 얻으려면 Python
  객체(`FetchResult.content`)에 직접 접근해야 한다(round-10 contract §0/§2 근거).
- **호출 모드:** 항상 `enable_playwright=False`(엔진 자체의 Playwright MCP 폴백
  경로는 절대 트리거하지 않음 — 무인 배치 환경에서 MCP 세션에 의존할 수 없으므로).
  JS 렌더가 필요하면 sipher 자체의 Tier2(`adapters/web/render.py`, Python
  playwright)로 별도 승격한다 — engine의 내장 playwright 경로와는 무관한 별개 구현.
- **라이선스 의무:** MIT는 재배포 시 저작권 고지 + permission notice 동봉을 요구 →
  `adapters/web/engine/LICENSE`에 원본 MIT 전문(© 2026 fivetaku) 동봉 완료.

## 벤더 코드 특징 (수정 없이 그대로 활용 — 참고용 요약)

- `engine/safety.py` — SSRF 방어(private/loopback/link-local/reserved/metadata IP
  차단, DNS-rebinding 방어, 리다이렉트 매 hop 재검증). sipher web 어댑터는 이를 1차
  방어선으로 신뢰하고 별도 재구현하지 않는다(round-10 contract §5).
- `engine/fetch_chain.py` — WAF 프로파일 그리드 기반 fetch 오케스트레이션. 공개
  진입점은 `fetch(url, **kwargs) -> FetchResult`.
- `engine/learning.py` — 호스트별 성공 라우트 학습 캐시(`~/.insane_search/learned.json`,
  자기 정리·TTL·상한 있는 바운디드 JSON). 사이트별 코드 분기 없음(No-Site-Name Rule).
- `engine/waf_profiles.yaml` — WAF 지문(패턴 기반, 사이트명 아님) 데이터.

## 알려진 특성 (버그 아님 — 문서 경고만)

- `FetchResult.to_dict()`는 `content`(본문)를 의도적으로 생략한다(`content_length`만
  노출) — engine 자체가 "untrusted web content"를 caller에 그대로 흘려보내지 않으려는
  설계 의도. sipher는 이 dict를 쓰지 않고 `FetchResult.content` 속성에 직접 접근한다.
- `engine/__main__.py`(CLI)는 `must_invoke_playwright_mcp` 힌트를 stderr에 출력하는
  로직이 있으나, sipher는 이 CLI를 호출하지 않으므로(Python import만 사용) 해당
  안내는 무관하다.
