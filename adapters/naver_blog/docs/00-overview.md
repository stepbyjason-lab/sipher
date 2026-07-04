# sipher-naver-blog — 네이버 블로그 어댑터 · 설계 Overview

- **상태:** 구현(T6·T8 완료, 실측 검증) · **작성:** 2026-06-30 · **위치:** `adapters/naver_blog`
- **이식 원본:** 자작 수집 도구(비공개) `scrapers/naver_blog_scrape.py`
- **이 문서가 충족하는 마디 floor(docs/26, app_development):** PRD(§1·2) · 아키텍처(§4) · 유저 플로우(§5) · 데이터 모델(§6) · 보안/위험(§7, 인라인 — 공개·무인증이라 등급 낮음).

---

## 1. 한 줄 정의 (PRD: 문제·해결)

**네이버 블로그 URL/블로그ID를 던지면 → 본문 텍스트 + 원본 이미지 + 영상 + 메타를 정규화 JSON으로 돌려주는 단일 도구.**

**문제(2026-06-30 spike로 정밀화):** 자작 수집 도구(비공개) 골격은 견고하나 이미지를 모든 호스트에 `?type=w966` 강제 → 호스트별로 손해가 다르다:

| Naver 이미지 CDN | 실측 거동 | 내부 도구(w966 강제) 결과 |
|---|---|---|
| **postfiles** (SE3 본문 인라인) | w966=실사진 천장 · w3840=동일(더 없음) · bare=placeholder | ✅ 적정(원본은 공개 CDN 미노출 = Naver 한계) |
| **blogfiles** (배너·첨부·구버전) | bare/w3840=**원본**(2000px+) · w966=**404** | ❌ **404로 누락** ← 회수 이득 지점 |
| **mblogthumb-phinf** (모바일) | w966 캡 | △ 모바일 경로 artifact |

→ **보완 = 호스트별 분기 + 데스크톱 PostView.** postfiles/mblogthumb는 w966(천장), **blogfiles는 bare/w3840로 원본 회수**(내부 도구가 놓친 것). 카메라 원본 전체 회수는 Naver가 공개 CDN에 안 줌(로그인 "원본 다운로드" 영역, 범위 밖).

## 2. 범위 (PRD: 핵심요구·비목표)

| 한다 ✅ | 안 한다 ❌ |
|---|---|
| 블로그 포스트 목록(모바일 API) + 본문 + 메타 | 인물 아카이브·갤러리·검색 → 자작 수집 도구에 잔류 |
| **원본 이미지 회수**(데스크톱 경로, w966→원본 보완) | 노트 합성 → note-factory |
| 영상 URL 수집·다운로드 | OCR·전사 → sipher 정규화 단계 |
| 동시성 fetch(scrapling AsyncFetcher) | 비공개 블로그(로그인 필요) |

**핵심요구(MVP 우선순위):**
1. 자작 수집 도구 수집 골격 이식(목록·본문·메타·동시성) — 검증된 부분 그대로
2. **이미지 호스트별 분기 레이어**(spike 완료) — 데스크톱 PostView + postfiles/mblogthumb는 w966, blogfiles는 bare/w3840 원본. download는 변형별 404 시 폴백 순서대로 재시도
3. sipher 정규화 출력

**비목표:** 위 표 ❌. 특히 인물 아카이브·통합 인덱스는 범위 밖.

## 3. 사용자 (PRD)

| 페르소나 | 사용 |
|---|---|
| 소유자(owner) | sipher 통해 네이버 블로그 URL 인제스션 |
| LITE 사용자 | 동일 — 공개 블로그라 인증 불필요(진입장벽 거의 없음) |

## 4. 아키텍처

```
입력: 네이버 블로그 URL 또는 blog-id (+옵션 logNo)
   │
   ▼ 1) 포스트 목록 (m.blog.naver.com/api/blogs/<id>/post-list, JSON)
   │     페이지네이션 30개/page, totalCount
   ▼ 2) 본문 HTML (m.blog.naver.com/PostView.naver, scrapling AsyncFetcher 동시성)
   │     se-main-container 본문 + og:title
   ▼ 3) 이미지 추출  ★보완 지점(spike 완료)★
   │     데스크톱 PostView(blog.naver.com) → postfiles/blogfiles 호스트
   │     호스트별 분기: postfiles→w966(천장) · blogfiles→bare/w3840(원본)
   │     download 변형 폴백: blogfiles는 [bare, w3840], postfiles는 [w966]
   ▼ 4) 영상 추출 (<video src>, .mp4/.m3u8)
   ▼ 다운로드 (정중한 직렬, CDN 예의)
   │
출력: 정규화 JSON (§6) + media/ 파일
```

## 5. 유저 플로우

1. `sipher-naver fetch <블로그URL>` 또는 sipher 라우터 호출 → §4 파이프라인.
2. 인증 불필요(공개 블로그). 비공개 블로그면 명확한 에러 후 skip.
3. 재실행 안전: 이미 받은 미디어 파일 존재 시 skip.

## 6. 데이터 모델 (정규화 출력)

```jsonc
{
  "source": "https://blog.naver.com/<id>/<logNo>",
  "platform": "naver_blog",
  "body_text": "<se-main-container 본문>",
  "comments": [],                 // 네이버 블로그 댓글은 별도 API(범위 밖, follow-up)
  "ocr_text": [],
  "transcript": null,
  "media_paths": ["media/<logNo>_img00.jpg", "media/<logNo>_vid00.mp4"],
  "meta": {
    "log_no": "...", "title": "...", "add_date": "...",
    "category": "...", "comment_count": 0, "read_count": 0, "like_count": 0,
    "image_size_label": "original | w966_ceiling | thumbnail_fallback",
    "fetched_at": "<ISO>"
  }
}
```

> `image_size_label`로 원본 회수 성공/폴백을 정직 표기.

## 7. 위험 등급 / 보안 (인라인)

- **work scale: 등급 1~2.** 공개 콘텐츠·**무인증**·자격증명 없음 → FB 대비 위험 낮음. 별도 security intake 문서 없이 본 절로 충족.
- **자산:** 다운로드 미디어(공개 블로그라 민감도 낮으나 타인 사진 가능 → 외부 업로드 ❌, 로컬 저장까지).
- **불변식:**
  1. media/ git 제외(.gitignore).
  2. 정중함: 청크/포스트 사이 sleep 유지(현 0.4s), CDN 예의. → 차단·DoS 회피.
  3. 정직 라벨: 원본 회수 실패 시 `image_size_label: w966_fallback`.
- **게이트:** Gate 1(이 문서) → Gate 4(이식 승인) → Gate 5(라운드 수락). 이미지 보완은 **de-risk spike 선행**(원본 경로 실측) 후 본 이식.

## 8. 이식 시 변경점 (자작 수집 도구 → sipher)

| 자작 수집 도구(비공개) | sipher-naver-blog |
|---|---|
| 하드코딩 로컬 아카이브 경로 | 인자/설정 기반 출력 경로 |
| `?type=w966` 고정(blogfiles 404 누락) | **호스트별 분기**: postfiles=w966, blogfiles=bare/w3840 원본 |
| 모바일 PostView(mblogthumb 캡) | **데스크톱 PostView**(postfiles/blogfiles) |
| posts.jsonl(내부 도구 포맷) | sipher 정규화 JSON(§6) |
| 갤러리/인덱스 연계 | 없음 |
