# sipher-fb — Facebook 어댑터 · 설계 Overview

- **상태:** 설계(design) · **작성:** 2026-06-30 · **위치:** `adapters/facebook`
- **이식 원본:** 자작 수집 도구(비공개) `scrapers/fb_scrape_playwright.py` + `fb_image_refetch.py` + `fb_video_refetch.py`
- **이 문서가 충족하는 마디 floor(docs/26, app_development):** PRD(§1·2) · 아키텍처(§4) · 유저 플로우(§5) · 데이터 모델(§6). 보안 intake는 [01-security-intake.md](01-security-intake.md)에서 별도(등급 3 표면).

---

## 1. 한 줄 정의 (PRD: 문제·해결)

**Facebook 프로필/페이지 URL을 던지면 → 본문 + 풀사이즈 사진(라이트박스 우회) + 영상 + 메타를 정규화 JSON으로 돌려주는 단일 도구.**

**문제:** FB는 풀사이즈 사진을 별도 photo viewer/set 페이지에서만 노출하고(`oh=`/`oe=` 서명이 path와 묶임), +N장 hidden 사진은 article DOM에 없다. 일반 스크래퍼·gallery-dl·kevinzg/facebook-scraper·신규 huzaifa-hb(2026-06)까지 **전부 작은 썸네일만** 받거나 mbasic 의존으로 깨졌음(2026-06 실측). 자작 수집 도구(비공개)만 풀사이즈+숨은사진을 회수한다 — 그 IP를 sipher용 공개 어댑터로 이식한다.

## 2. 범위 (PRD: 핵심요구·비목표)

| 한다 ✅ | 안 한다 ❌ |
|---|---|
| FB URL → 포스트 본문·메타 추출 | 인물 영구 아카이브·갤러리·검색·CLIP → 자작 수집 도구에 잔류 |
| **풀사이즈 사진 회수**(fbid → photo viewer → set URL carousel) | 노트 합성 → note-factory |
| **숨은 +N장** 사진 복구 | OCR·전사 → sipher 정규화 단계(어댑터 밖) |
| **영상** network capture + 다운로드 | 비공개 그룹·로그인월 너머 콘텐츠 우회 |
| 인증: ④persistent context + ③cookies-from-browser (+①cookies.txt fallback) | 봇 탐지 회피용 anti-detect/프록시 로테이션 |

**핵심요구(MVP 우선순위):**
1. 본문 텍스트 + 풀사이즈 사진(우회) — 자작 수집 도구 알고리즘 동일 회수율
2. 영상 capture
3. 인증 UX를 cookies.txt 수동 → persistent context로 전환
4. (round-14, 옵트인) 댓글 본문 — `fetch(comments=True)`/`--comments` 지정 시
   초기 로드 댓글 + "답글 N개" 확장 최대 5회까지 `comments[]`에 채운다(기본은
   여전히 `comments=False` → `comments[]` 빈 배열, 하위호환). 전체 댓글·페이지네이션·
   2단계 이상 중첩 답글·정렬 변경은 비목표(아래).

**비목표:** 위 표 ❌ 전부. 인물 아카이브는 이 어댑터 범위 아님. 댓글 관련 추가
비목표(round-14): 전체 댓글 페이지네이션(정렬 변경·전체 로드), 2단계 이상 중첩
답글, 댓글 작성자 프로필 심화 정보(팔로워수 등, href만 보존).

## 3. 사용자 (PRD)

| 페르소나 | 사용 | 비고 |
|---|---|---|
| 소유자(owner) | sipher 통해 FB URL 인제스션 | FULL 프로필, 본인 FB 쿠키 |
| LITE 사용자(팀원·지인) | 동일하나 본인 FB 로그인 1회 필요 | persistent context 로그인 플로우로 진입장벽 최소화 |

## 4. 아키텍처

```
입력: FB 프로필/페이지/포스트 URL
   │
   ▼ [auth] 세션 확보 (01-security-intake 참조)
   │   ④ persistent context (user_data_dir, 로그인 1회) — 기본
   │   ③ cookies-from-browser (browser_cookie3) — 파워유저
   │   ① cookies.txt (Netscape) — headless/서버 fallback
   ▼
1) 포스트 수집 (fb_scrape_playwright 이식)
   scroll + div[role=article] 평가 → permalink·본문·이미지·영상 URL
   인증 context.request.get 으로 발견 즉시 다운로드 (CDN URL 1~2h 만료)
   ▼
2) 풀사이즈 보강 (fb_image_refetch 이식 — 핵심 IP)
   permalink → a[href*=fbid=] 수집 → photo viewer 방문
   → set URL(media/set/?set=pcb.X) carousel 전체 fbid(+N장 hidden)
   → 각 fbid viewer 네비게이션 → 최대 <img>(2048px급) src → 인증 다운로드
   ▼
3) 영상 보강 (fb_video_refetch 이식)
   network capture(.mp4/m3u8) + DOM scan → 인증 다운로드
   ▼
출력: 정규화 JSON (§6) + media/ 파일
```

성능: post당 ~30초(사진 7장 평균). 속도 비요구(배치·야간·재시도).

## 5. 유저 플로우

1. **(최초 1회)** `sipher-fb login` → Chromium 창 → 사용자가 FB 로그인 → 세션이 profile dir에 저장. 이후 재로그인 불필요(만료 시 1회 반복).
2. `sipher-fb fetch <url>` 또는 sipher 라우터가 호출 → §4 파이프라인 → 정규화 JSON + media 경로 반환.
3. 재실행 안전: 이미 받은 미디어(해시 기준) skip.
4. 실패 경로: 로그인월/잘못된 URL → main 컨테이너 미로딩 감지 → 명확한 에러(현재 URL 표시) 후 중단.

## 6. 데이터 모델 (정규화 출력)

sipher 공통 스키마를 따른다:

```jsonc
{
  "source": "<원본 URL>",
  "platform": "facebook",
  "body_text": "<포스트 본문 합본>",
  "comments": [                   // comments=True(옵트인)일 때만 채워짐. 기본 []
    {
      "id": "<프로필 href 또는 null>",
      "author": "<작성자명 추정 또는 null>",
      "text": "<댓글 본문>",
      "likes": 0,
      "reply_count": 0,          // round-14는 1단계 답글까지만, 중첩 카운트는 미집계(0 고정)
      "media_paths": []          // round-14는 텍스트만(댓글 첨부 미디어 미수집)
    }
  ],
  "ocr_text": [],                 // 어댑터 밖(sipher 정규화)에서 채움
  "transcript": null,             // 영상 전사는 어댑터 밖
  "media_paths": ["media/fb_<hash>.jpg", "media/fb_vid_<id>.mp4"],
  "meta": {
    "permalink": "...",
    "likes": 0, "comment_count": 0,        // FB가 보고하는 댓글 "개수"(본문 정규식 파싱, 기존 필드)
    "comment_count_captured": 0,           // comments=True일 때만 non-null — 실제 comments[] 길이
    "photos_recovered": 0, "photos_hidden_recovered": 0,
    "fullsize_label": "fullsize_viewer | largest_cdn | thumbnail_only",
    "comments_label": "collected | partial | none | fetch_failed | login_required | not_collected",
    "auth_mode": "persistent | browser_cookie | cookies_txt",
    "fetched_at": "<ISO>"
  }
}
```

> `fullsize_label`은 huzaifa-hb의 정직 라벨 패턴 차용 — 풀사이즈 회수 실패 시 `thumbnail_only`로 정직 표기(허위 "원본" 주장 금지).
>
> `comments_label`(round-14, `comments_status` 대체; round-16에서 `fetch_failed` 추가) —
> `collected`(1건 이상 파싱 성공) · `partial`(확장 상한/중단, 일부 파싱 실패, 또는 캡션
> 매칭 실패로 idx0 폴백한 저신뢰 수집 — round-16 #5) · `none`(comments=True인데 본문 이후
> 댓글 article이 0개, 정상적인 빈 상태) · `fetch_failed`(round-16 #4 — 추출 자체 실패:
> evaluate 예외·페이지 로딩 실패·후보는 있으나 전부 파싱 불가. '댓글0'인 `none`과 구분) ·
> `login_required`(인증 실패 — 방어적 정의, 실제로는 AuthError로 fetch 자체가 먼저
> 실패하므로 이 라벨까지 도달하지 않음) · `not_collected`(comments=False, 기본값 —
> 기존 동작과 하위호환).

## 7. 위험 등급 / 게이트

- **work scale: 등급 2**(다파일 신규 구현·구조). **+ 보안 렌즈 등급 3**(FB 쿠키=자격증명, Playwright=외부 실행) → **security intake 필수**([01](01-security-intake.md)).
- **적용 게이트:** Gate 1(제품 정의·이 문서) → Gate 4(소스 이식 승인) → Gate 5(라운드 수락: 코드/보안 리뷰 + 실행 증거).
- **de-risk spike:** 인증 전환(persistent context로 FB 세션 실제 유지되는지) — 본 이식 전 별도 task 권장.

## 8. 이식 시 변경점 (자작 수집 도구 → sipher)

| 자작 수집 도구(비공개) | sipher-fb |
|---|---|
| 하드코딩 로컬 아카이브 경로 | 인자/설정 기반 출력 경로 |
| 인물 특정(`--profile <개인식별자>`) | 임의 FB URL 입력 |
| `cookies/facebook_cookies.txt` 수동 export | ④persistent + ③browser, ①txt는 fallback |
| posts_pw.jsonl 등 내부 도구 포맷 | sipher 정규화 JSON(§6) |
| 갤러리/인덱스 빌더 연계 | 없음(어댑터는 수집까지) |
