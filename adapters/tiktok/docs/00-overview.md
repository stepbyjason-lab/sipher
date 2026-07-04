# sipher-tiktok — TikTok 어댑터 · 설계 Overview

- **상태:** 구현(round-09) · **작성:** 2026-07-02 · **위치:** `adapters/tiktok`
- **이식 원본:** 없음(gallery-dl pip 라이브러리 subprocess 직접 호출 — 벤더링 아님)
- **이 문서가 충족하는 마디 floor:** PRD(§1·2) · 아키텍처(§4) · 데이터 모델(§6) · 위험/보안(§7).

---

## 1. 한 줄 정의

**공개 TikTok 영상 URL을 던지면 → 캡션(desc) + 메타(통계·작성자) + 미디어(옵트인
다운로드)를 정규화 JSON으로 돌려주는 도구.**

## 2. 범위

| 한다 ✅ | 안 한다 ❌ |
|---|---|
| 공개 영상 캡션(desc)·통계·작성자 메타 | 첫 댓글 수집(브라우저 어댑터 — 별도 라운드) |
| 영상 다운로드 opt-in(`download=True`) | 슬라이드쇼(포토 캐러셀) 개별 항목 심화 파싱 |
| `tiktok.com`/`vt.tiktok.com`/`vm.tiktok.com` 3종 host | OCR·전사(→ sipher 정규화 단계) |

## 3. De-risk Spike 핵심 사실 (round-09 contract 전문 참조)

`python -m gallery_dl --dump-json <공개 TikTok URL>` — **완전 성공**(2026-07-02
실측). stderr에 `[tiktok][info] Solving JavaScript challenge`가 출력되지만 이는
gallery-dl 내장 처리이고 exit code는 0. 반환 JSON은 `[[2, {payload}], ...]` 형태
(gallery-dl dispatch 튜플, `2`=미디어 항목)이며 `desc`(캡션 전문)·`stats`·`author`·
`authorStats`·`video.playAddr` 등 필요한 필드가 모두 확인됨. 댓글은
`comments: []`로 채워지지 않음(설계대로 비목표).

## 4. 아키텍처

```
입력: TikTok 영상 URL(정식 또는 vt/vm 단축 링크)
   │
   ▼ parse_url — tiktok.com(+www./vt./vm.) 호스트 검증만(SSRF 방어).
   │     video id 재구성 없음 — gallery-dl이 원본 URL을 그대로 해석(단축 링크 포함).
   │
   ▼ `python -m gallery_dl --dump-json -- <url>` (subprocess, list 인자, shell=False)
   │     stdout JSON 파싱 → 첫 미디어 payload(desc 필드 보유) 추출
   │
   ▼ (opt-in) download=True → `python -m gallery_dl -d <media_dir> -- <url>` 재호출
   │     (--dump-json은 다운로드하지 않으므로 별도 호출 — gallery-dl 자체 계약)
   │
출력: 정규화 JSON(§6) + (옵트인) 미디어 파일
```

## 5. 유저 플로우

1. `sipher-tiktok fetch <영상URL>` 또는 sipher 라우터 호출 → §4 파이프라인.
2. 인증 불필요(공개 영상). 실패(비공개/삭제/차단)는 `GalleryDlError`로 명확히 보고.
3. `--download`로 opt-in 시 media_dir에 영상 파일 저장.

## 6. 데이터 모델 (정규화 출력)

```jsonc
{
  "source": "https://www.tiktok.com/@user/video/<id>",
  "platform": "tiktok",
  "body_text": "<desc 캡션>",
  "comments": [],                 // 비목표(별도 라운드)
  "ocr_text": [],
  "transcript": null,
  "media_paths": ["downloads/<video>.mp4"],
  "meta": {
    "video_id": "...", "author": "...", "author_verified": false,
    "digg_count": 0, "comment_count": 0, "play_count": 0, "share_count": 0,
    "duration_sec": 0, "media_label": "none | downloaded | download_failed",
    "created_at_utc": "<ISO>", "fetched_at": "<ISO>"
  }
}
```

## 7. 위험 등급 / 보안 (인라인)

- **work scale: 등급 1~2.** 공개 콘텐츠·무인증. gallery-dl subprocess 호출이
  유일한 외부 프로세스 경계.
- **라이선스:** gallery-dl은 GPL-2.0(requirements.txt 참조). sipher는 이를
  vendor(코드 복사)하지 않고 별도 프로세스로 subprocess 호출만 한다 — 동일
  프로세스 결합이 아니므로 GPL 전파 조건에 해당하지 않는다는 것이 통설(round-09
  리뷰 P3, round-10 §⑤에서 문서화 완료). pip 사용자가 자기 환경에 직접
  설치하므로 재배포 의무도 없다.
- **불변식:**
  1. host 화이트리스트(`tiktok.com`/`vt.tiktok.com`/`vm.tiktok.com`만) — SSRF 방어.
  2. subprocess는 list 인자 + `shell=False` + URL 앞 `--`(옵션 오인/인자 인젝션 방어,
     core/transcribe.py·adapters/youtube 패턴과 동일).
  3. 외부 LLM 호출 없음 — 추출은 gallery-dl JSON 필드 매핑만.
- **게이트:** round-09 contract(de-risk spike 선행) → 구현 → result.

## 8. 알려진 한계

- 첫 댓글은 이 어댑터로 수집되지 않는다(비목표) — `meta.comment_count`는 통계
  수치일 뿐 `comments[]`는 항상 빈 리스트.
- 슬라이드쇼(여러 이미지) 영상의 경우 `--dump-json` 결과가 여러 미디어 항목을
  포함할 수 있으나, 이 어댑터는 desc를 보유한 첫 항목만 대표로 사용한다.
