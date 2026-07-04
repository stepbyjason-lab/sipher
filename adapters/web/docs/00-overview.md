# sipher-web — 웹 아티클 어댑터 · 설계 Overview

- **상태:** 구현(round-10) · **작성:** 2026-07-02 · **위치:** `adapters/web`
- **이식 원본:** `adapters/web/engine/`(vendored, insane-search engine, MIT — `_SOURCE.md` 참조).
  어댑터 본체(`__init__.py`/`render.py`/`cli.py`)는 신규 작성(벤더링 아님).
- **이 문서가 충족하는 마디 floor:** PRD(§1·2) · 아키텍처(§4) · 데이터 모델(§6) · 위험/보안(§7).

---

## 1. 한 줄 정의

**어느 기존 6플랫폼(youtube/threads/facebook/naver_blog/instagram/tiktok) host에도
매칭되지 않는 임의 http(s) 아티클 URL을 던지면 → 본문 텍스트를 정규화 JSON으로
돌려주는 범용 폴백 도구. 정적 페이지는 순수 코드로, JS로만 렌더되는 SPA는 필요할
때만 헤드리스 브라우저로 승격한다.**

## 2. 범위

| 한다 ✅ | 안 한다 ❌ |
|---|---|
| 정적 HTML 아티클 본문 추출(Tier1, curl_cffi WAF 그리드) | 미디어(이미지/영상) 다운로드 |
| SSR 껍데기 의심 시 JS 렌더 자동 승격(Tier2, Python playwright) | 댓글 수집(웹 아티클은 댓글 개념 없음) |
| `js=auto/true/false` opt 3-way | 멀티페이지 아티클 병합·페이지네이션 |
| host 화이트리스트가 아닌 **범용 폴백**으로 라우터 결선 | Playwright **MCP**(Claude 세션 도구) 경로 사용 |

## 3. De-risk Spike 핵심 사실 (round-10 contract §0 전문 참조)

insane-search engine(github.com/fivetaku/insane-search, commit `3a2f6c85...`)을
example.com·Reddit에서 실측 성공, LLM 호출 0회, `--no-playwright`(순수 코드)로
완결. 한계: JS를 실행하지 않아 Next.js SSR 같은 SPA에서는 껍데기만 받는다(Notion
실측: 가독 텍스트 2.3KB/응답 486KB). engine 자체의 JS경로는 Playwright **MCP**
의존이라 무인 배치 불가 — sipher는 별도로 Python `playwright`(sync API, chromium
headless)를 Tier2로 구현해 이 간극을 메운다. sipher 환경에 `curl_cffi`/`pyyaml`/
`playwright` 전부 이미 설치돼 있음을 확인(추가 설치 불요).

## 4. 아키텍처

```
입력: 임의 http(s) URL(6플랫폼 host 미매칭)
   │
   ▼ core.detect_platform() 실패 → core.fetch()가 "web" 폴백으로 위임
   │
   ▼ parse_url — 스킴(http/https만)·호스트 존재 검증(2차 SSRF 방어선)
   │
   ▼ Tier1: adapters.web.engine.fetch(url, enable_playwright=False)
   │     curl_cffi WAF 프로파일 그리드. engine 내장 SSRF 방어(1차선,
   │     private/loopback/link-local/reserved/metadata IP 차단 + 리다이렉트
   │     매 hop 재검증) 적용됨.
   │     → raw HTML → html.parser 기반 태그 스트립 → body_text
   │
   ▼ SSR 껍데기 의심 판정(길이<200자 OR verdict=weak_ok OR SPA 마커 존재)
   │     js="auto"이고 의심 → 승격 / js=True → 무조건 승격 / js=False → 승격 안 함
   │
   ▼ (승격 시) Tier2: adapters.web.render.render_js(url)
   │     Python playwright, headless chromium, networkidle 대기 → page.content()
   │     → 재추출한 텍스트가 더 길면 채택, 아니면 tier1 결과 유지
   │
출력: 정규화 JSON(§6, meta.tier=1|2, meta.content_label로 정직 라벨)
```

## 5. 유저 플로우

1. `sipher-web fetch <URL>` 또는 sipher 라우터 호출(`python -m core fetch <URL>`,
   6플랫폼에 매칭 안 되고 http(s)면 자동으로 web으로 위임) → §4 파이프라인.
2. 기본(`js=auto`)은 정적 페이지면 빠르게 tier1만으로 끝나고, SPA 의심이면 자동으로
   느린 tier2(브라우저 렌더)로 승격 — 사용자가 URL 성격을 미리 몰라도 됨.
3. 명시적으로 `--js false`(tier1 고정, 빠름) 또는 `--js true`(항상 렌더, 느리지만
   확실) 선택 가능.

## 6. 데이터 모델 (정규화 출력)

```jsonc
{
  "source": "https://example.com/article",
  "platform": "web",
  "body_text": "<본문 텍스트>",
  "comments": [],          // 웹 아티클은 댓글 개념 없음 — 항상 빈 배열
  "ocr_text": [],
  "transcript": null,
  "media_paths": [],       // 이번 스코프는 미디어 다운로드 없음 — 항상 빈 배열
  "meta": {
    "tier": 1,                          // 1(정적) | 2(JS 렌더 승격됨)
    "verdict": "strong_ok",             // engine 자체 판정(strong_ok/weak_ok/...)
    "final_url": "https://example.com/article",  // 리다이렉트 최종 목적지
    "content_label": "ok",              // ok | ssr_shell_only | js_rendered | failed
    "engine_ok": true,
    "js_error": null,                   // tier2 시도 실패 시 사유(정직 기록)
    "fetched_at": "<ISO>"
  }
}
```

## 7. 위험 등급 / 보안 (인라인)

- **work scale: 등급 2.** 범용 폴백이 임의 URL을 열게 되므로 host 화이트리스트
  플랫폼 어댑터(등급 1)보다 상위 — SSRF 표면이 넓다.
- **자산:** 없음(로그인 세션·자격증명 없음, 익명 공개 페이지만 대상).
- **불변식:**
  1. **SSRF 1차 방어선 = engine 내장(`engine/safety.py`)** — private/loopback/
     link-local/reserved/multicast/unspecified IP 차단, DNS-rebinding 방어(hostname
     resolve 후 전체 A/AAAA 검사), 리다이렉트 매 hop 재검증. 어댑터는 이를 재구현
     하지 않고 신뢰한다(round-10 contract §5, 중복 구현 방지).
  2. **2차 방어선 = `parse_url`** — http(s) 스킴만, 호스트 존재 필수. `file://`/
     `javascript:`/스킴 없음은 즉시 거부.
  3. **`INSANE_ALLOW_PRIVATE` 환경변수를 어댑터가 설정하지 않는다** — engine
     기본값(default-deny)을 그대로 신뢰, 사용자 환경변수 override는 사용자 책임.
  4. **Tier2 playwright는 로컬 신뢰 실행** — threads/facebook의 기존 playwright
     사용과 동일 신뢰 경계. `render_js`는 이미 검증된 URL만 받는다는 전제(호출
     순서로 보장, 함수 자체는 재검증하지 않음).
  5. 외부 LLM 호출 없음 — 추출은 engine(코드)과 html.parser(표준 라이브러리)만.
- **게이트:** round-10 contract(de-risk spike 선행) → 구현 → result.

## 8. 알려진 한계 (2026-07-02 기준, 정직 기록)

- SSR 껍데기 판정 휴리스틱은 보수적(OR 조건)이라 **과다 승격**(진짜 짧은 정적
  페이지도 tier2로 넘어감)을 오탐보다 선호한다 — 정확도보다 누락 방지를 우선한
  설계 결정.
- Tier2 렌더링이 tier1보다 느리다(브라우저 launch + networkidle 대기) — `js=auto`
  기본값이 이 비용을 URL 성격에 따라 자동으로만 지불하도록 설계했지만, 오탐 시
  불필요한 지연이 발생할 수 있다.
- 미디어 다운로드·댓글 수집은 이번 스코프 밖(§2) — 필요 시 별도 라운드.
- `core.detect_platform()` 자체는 web을 인식하지 않는다(의도적 — round-10 contract
  §4). `python -m core detect <웹 URL>`은 여전히 `ValueError`를 던지고,
  `python -m core fetch <웹 URL>`만 web 폴백으로 성공한다 — 이 비대칭은 버그가
  아니라 설계다.
