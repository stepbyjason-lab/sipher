# sipher-x — X(구 트위터) 어댑터 · Overview

- **상태:** 구현 · **위치:** `adapters/x`
- **이식 원본:** 없음(gallery-dl pip 라이브러리 subprocess 직접 호출 — 벤더링 아님, tiktok 어댑터와 같은 방식)
- **정규화 계약:** `fetch(url) -> { source, platform, body_text, author_thread[], quoted[], comments[], ocr_text[], transcript, media_paths[], meta }` — 공통 8키에 `author_thread[]`·`quoted[]` 두 키를 더한다.
- **경계:** 어댑터는 수집·정규화만. 노트 합성=`note-factory`, 라우팅=`sipher`. sipher 내부 미-import(추출 가능).

---

## 1. 한 줄 정의

**X 포스트 URL 하나를 던지면 → 원글 본문 + 저자가 이어 쓴 글 + 인용한 포스트(인용 아티클은 제목·표지·이미지까지) + 저자 대댓글 + 타인 답글 + 영상·사진 원본을 정규화 JSON으로 돌려주는 도구. X 로그인 쿠키가 필요하다.**

## 2. 범위

| 한다 ✅ | 안 한다 ❌ |
|---|---|
| `x.com`·`twitter.com`(+`www.`·`mobile.`)의 포스트(`/<계정>/status/<id>`, `/i/web/status/<id>`) URL | 프로필·타임라인·검색·리스트 수집 |
| 원글 본문, 저자 이어쓰기, 인용 포스트, 저자 대댓글, 타인 답글 | 인용 아티클의 **본문 전문** — gallery-dl이 제목·표지·이미지까지만 주고 본문은 링크로만 온다 |
| 영상·사진을 원본 해상도로 다운로드(옵트인) + 받은 파일의 실제 가로세로 기록 | 로그인 자동화(쿠키 파일을 사용자가 직접 준비) |
| 쿠키가 없거나 만료되면 빈 결과가 아니라 `AuthRequired` 오류 | X Spaces·DM·커뮤니티 |
| 타인 답글 개수 상한(`--max-replies`, 기본 100) | OCR·전사(→ sipher 정규화 단계가 `ocr_text`·`transcript`를 채운다) |

## 3. 쿠키 설정 (필수)

X는 로그인한 세션이 아니면 대화(이어쓰기·답글)를 주지 않는다. 쿠키 파일 경로를 한 번 지정해 두면 된다.

1. 브라우저에서 x.com에 로그인한 상태의 쿠키를 **넷스케이프 형식**(`cookies.txt`) 파일로 내보낸다.
2. 루트 `.env.local`(또는 같은 이름의 환경변수)에 그 파일 경로를 적는다. `.env.local`이 환경변수보다 먼저 읽힌다. 상대경로는 저장소 루트 기준이다.

```bash
# .env.local
X_COOKIES_FILE=/path/to/x_cookies.txt
```

3. 쿠키 파일은 로그인 세션 그 자체다. 저장소 밖에 두고 커밋하지 않는다.

다음 세 경우는 모두 `AuthRequired` 오류로 끝나며, 오류 문구가 원인(미설정·파일 없음·만료 의심)을 구분해 알려 준다. CLI에서는 `오류: AuthRequired: ...`와 종료 코드 1이다.

| 상황 | 동작 |
|---|---|
| `X_COOKIES_FILE` 미설정 | gallery-dl이 `AuthRequired`를 담아 정상 종료(종료 코드 0)하지만, 어댑터가 이를 빈 성공으로 내지 않고 오류로 올린다 |
| 설정했는데 그 파일이 없음 | gallery-dl을 부르기 전에 「쿠키 파일이 없습니다」 오류 |
| 쿠키 만료 | 위와 같이 `AuthRequired` 오류, 문구에 「쿠키가 만료됐을 수 있습니다」 안내 |

다운로드 단계에서 쿠키가 만료돼도 `media_label = "download_failed"`인 성공 결과가 아니라 같은 오류로 끝난다.

## 4. 사용법

```bash
# 라우터 — URL만 주면 X로 판별(smart 기본 ON이라 미디어도 받는다)
python -m core fetch "https://x.com/someone/status/1234567890" --json

# 타인 답글 상한을 바꾼다(기본 100)
python -m core fetch "https://x.com/someone/status/1234567890" --max-replies 30

# 어댑터 단독 CLI — 다운로드는 --download를 줄 때만
python -m adapters.x.cli fetch "https://x.com/someone/status/1234567890" --download --media-dir ./downloads
```

| 옵션 | 의미 |
|---|---|
| `--max-replies N` | **타인 답글**에만 거는 상한(0 이상 정수, 기본 100). 저자 이어쓰기·저자 대댓글은 자르지 않는다. 잘렸는지는 `meta.replies_truncated`로 안다 |
| `--download` / `--media-dir DIR` | 영상·사진 원본을 `DIR`(기본 `downloads`)에 받는다. `python -m core fetch`는 smart가 켜져 있으면 자동으로 받고, `--no-smart`면 `--download`를 명시해야 한다 |

Python에서는 `from adapters.x import fetch`(또는 `core.fetch`)로 `fetch(url, *, media_dir=None, download=False, max_replies=100)`을 부른다.

`python -m core fetch`의 기본 Markdown 출력은 본문·댓글·미디어·OCR·전사를 그린다. `author_thread[]`·`quoted[]`는 `--json`에서 확인한다.

## 5. 동작 방식

```
입력: x.com / twitter.com 포스트 URL
   │
   ▼ parse_url — host·경로 화이트리스트(SSRF 방어). 꼬리 파라미터(?s=46 등)를 떼고
   │     canonical `https://x.com/<계정>/status/<id>`를 다시 만든다 — 같은 포스트는 같은 결과.
   │
   ▼ `python -m gallery_dl --dump-json -o text-tweets=true -o conversations=true
   │     -o replies=true -o quoted=true --cookies <쿠키 파일> -- <canonical>`
   │     (subprocess, list 인자, shell=False) — 한 번의 호출로 원글·이어쓰기·인용·답글이 함께 온다.
   │
   ▼ 받은 트윗을 자리별로 나눈다(아래 표)
   │
   ▼ (옵트인) 남길 트윗의 미디어만 원본으로 다운로드(`--write-metadata` 사이드카 포함)
   │     → 받은 파일의 실제 가로세로를 잰다
   │
출력: 정규화 JSON(§6) + (옵트인) 미디어 파일
```

받은 트윗을 나누는 규칙은 다음과 같다.

| 트윗 | 들어가는 자리 |
|---|---|
| 요청한 포스트 | `body_text`(원문 그대로) |
| 작성자가 원글 또는 자기 이어쓰기에 단 답글 | `author_thread[]`(Threads 어댑터와 같은 필드) |
| 원글이 인용한 포스트 | `quoted[]`(답글이 아니다). 답글이 인용한 포스트는 그 답글 항목의 `quoted[]` 안에 붙는다 |
| 작성자가 그 밖의 트윗에 단 답글 | `comments[]`, `kind = "author_reply"`(자르지 않는다) |
| 타인의 답글 | `comments[]`, `kind = "reply"`(`max_replies`까지) |

한 트윗은 한 자리에만 들어간다. `comments[]`는 X가 준 대화 순서를 유지한다.

## 6. 출력 필드

```jsonc
{
  "source": "https://x.com/<계정>/status/<id>",
  "platform": "x",
  "body_text": "<원글 본문>",
  "author_thread": [ { /* item */ } ],
  "quoted": [ { /* item */ } ],
  "comments": [ { /* item + kind */ } ],
  "ocr_text": [],              // OCR을 돌렸을 때만 채워짐
  "transcript": null,          // 전사를 돌렸을 때만 채워짐
  "media_paths": ["downloads/..."],   // 다운로드했을 때만
  "meta": { }
}
```

**item**(`author_thread[]`·`quoted[]`·`comments[]` 공통)

| 필드 | 뜻 |
|---|---|
| `id` | 트윗 id(문자열) |
| `author` | 작성자 계정 이름(gallery-dl의 `author.name`) |
| `text` | 본문 |
| `created_at` | gallery-dl이 준 날짜 문자열(`YYYY-MM-DD HH:MM:SS`, 시간대 표기 없음) |
| `reply_to_id` | 답글이면 부모 트윗 id, 아니면 `null` |
| `likes` · `reply_count` | 좋아요 수 · 답글 수 |
| `media[]` | X가 선언한 미디어(`url`·`type`·`width`·`height`·`extension`). `type`은 `video`·`animated_gif`·`article:cover`·`article:image` 등 |
| `media_paths[]` | 받은 파일 경로(다운로드했을 때만) |
| `quoted[]` | 그 트윗이 인용한 포스트(같은 item 구조, 인용 사슬) |
| `article` | 인용 아티클일 때만 — gallery-dl이 준 아티클 메타(제목 포함)를 그대로 |
| `kind` | `comments[]`에만 — `author_reply` 또는 `reply` |

**meta**

| 필드 | 뜻 |
|---|---|
| `author` · `code` · `tweet_id` | 원글 작성자 · 원글 id(`code`와 `tweet_id`는 같은 값) |
| `created_at` · `lang` | 원글 게시 시각(gallery-dl 날짜 문자열) · 언어 |
| `likes` · `reply_count` · `retweet_count` · `quote_count` · `view_count` | 원글 통계 |
| `media[]` | 원글 미디어 선언 |
| `author_thread.count` · `quoted_count` · `author_reply_count` · `other_reply_count` | 자리별 개수 |
| `other_reply_total` · `max_replies` · `replies_truncated` | 받은 타인 답글 전체 수 · 상한 · 상한에 걸려 잘렸는지 |
| `image_count` · `video_count` · `has_video` | 선언된 미디어 개수(인용·이어쓰기·답글 미디어까지 합산) |
| `media_label` | `none` · `downloaded` · `partially_downloaded` · `download_failed`(다운로드했을 때 확정) |
| `video_label` | 영상이 있는데 전부 받지 못했을 때만 — `media_label`과 같은 값 |
| `media_files[]` | 다운로드했을 때만 — 받은 파일마다 아래 항목 |
| `cookies_used` | 쿠키 파일로 호출했는지 |
| `gallery_dl_errors[]` | gallery-dl이 dump 안에 실어 보낸 오류 항목 |
| `kept_tweet_ids[]` | 결과에 남은 트윗 id 전부 |
| `fetched_at` | 수집 시각 |

**`meta.media_files[]`** — 받은 파일마다 한 항목.

| 필드 | 뜻 |
|---|---|
| `path` · `tweet_id` | 파일 경로 · 그 파일이 속한 트윗 id |
| `width` · `height` | 받은 파일의 **실제** 가로세로. 시스템에 `ffprobe`가 있으면 파일을 재서 채우고, 없거나 읽지 못하면 X가 선언한 크기로 채운다 |
| `declared_width` · `declared_height` | X가 선언한 가로세로(`--write-metadata` 사이드카 값) |
| `size_source` | `width`·`height`를 어떻게 얻었는지 — `probe`(ffprobe로 측정) 또는 `sidecar`(선언 크기) |

X는 선언한 크기보다 작은 변형을 내줄 수 있어서 실제 크기와 선언 크기를 따로 둔다. 원본 해상도가 중요하면 `size_source`가 `probe`인 항목의 `width`·`height`를 믿는다.

## 7. 위험 등급 / 보안

- **라이선스:** gallery-dl은 GPL-2.0(`requirements.txt` 참조). sipher는 이를 vendor하지 않고 별도 프로세스로 subprocess 호출만 한다 — 동일 프로세스 결합이 아니므로 GPL 전파 조건에 해당하지 않는다는 것이 통설이다(tiktok 어댑터와 같다). pip 사용자가 자기 환경에 직접 설치하므로 재배포 의무도 없다.
- **불변식:**
  1. host·경로 화이트리스트(`x.com`/`twitter.com`의 `status` 경로만) — SSRF 방어.
  2. subprocess는 list 인자 + `shell=False` + URL 앞 `--`(옵션 오인/인자 인젝션 방어).
  3. 외부 LLM 호출 없음 — 추출은 gallery-dl JSON 필드 매핑만.
  4. 쿠키 값은 어디에도 쓰지 않는다 — 결과·로그에는 경로 사용 여부(`cookies_used`)만 남는다.
- **데이터 약관(정직 고지):** 로그인 쿠키로 X 페이지 데이터를 가져온다 — X ToS 회색지대다. 개인용 적합 전제 위에서 쓰고, 대량·상업적 수집, rate-limit 우회, 비공개 계정 무단 접근은 이 어댑터의 의도된 사용 범위 밖이다.

## 8. 의존성

- **필수:** `gallery-dl`(GPL-2.0, `adapters/x/requirements.txt`, tiktok 어댑터와 같은 패키지라 LITE에 이미 설치됨).
- **선택:** 시스템 `ffprobe`(ffmpeg 패키지에 포함) — 받은 미디어의 실제 크기를 재는 데만 쓰며, 없어도 동작한다(`size_source = "sidecar"`).

## 9. 알려진 한계

- **쿠키가 필요하다.** 쿠키 없이는 어떤 포스트도 받을 수 없다. 쿠키가 만료되면 다시 내보내야 한다.
- **인용 아티클의 본문 전문은 받지 못한다.** 제목·표지·이미지까지만 오고 본문은 링크(`x.com/i/article/...`)로만 온다.
- **타인 답글은 상한까지만 남는다.** 받은 타인 답글이 `max_replies`를 넘으면 X가 준 순서대로 앞에서부터 상한만큼만 `comments[]`에 남는다. 전체 몇 건이었는지는 `meta.other_reply_total`, 잘렸는지는 `meta.replies_truncated`로 확인한다.
- **원글을 못 찾으면 오류다.** 비공개·삭제·차단된 포스트는 빈 성공이 아니라 「원글을 찾을 수 없음」 오류로 끝난다.
- **gallery-dl 실행 한도.** 메타 수집은 180초, 다운로드는 600초를 넘기면 타임아웃 오류다.
