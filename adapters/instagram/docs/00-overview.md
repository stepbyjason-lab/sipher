# sipher-instagram — Instagram 어댑터 · 설계 Overview

- **상태:** 구현(round-09) · **로그인 세션 필수로 정정(round-10 §④)** · **작성:** 2026-07-02
  · **위치:** `adapters/instagram`
- **이식 원본:** 없음(instaloader pip 라이브러리 직접 호출 — 벤더링 아님)
- **이 문서가 충족하는 마디 floor:** PRD(§1·2) · 아키텍처(§4) · 데이터 모델(§6) · 위험/보안(§7).

---

## 1. 한 줄 정의

**공개 Instagram 포스트/릴스 URL을 던지면 → 캡션(본문) + 미디어(옵트인 다운로드) +
메타를 정규화 JSON으로 돌려주는 도구. round-09/round-10 두 라운드 실측 결과 IG는
익명 접근을 거의 항상 403으로 차단하므로 실질적으로 로그인 세션이 필요하며, 이
어댑터는 그 사실을 명확한 에러 메시지로 안내한다(우회하지 않는다).**

## 2. 범위

| 한다 ✅ | 안 한다 ❌ |
|---|---|
| 공개 포스트/릴스(`/p/`, `/reel/`, `/tv/`) 캡션·메타(**로그인 세션 있을 때**) | 프로필 피드 크롤, 스토리(24h 휘발) |
| 댓글 수집 opt-in(`comments=True`) — 막히면 정직 라벨 degrade | 실계정 로그인 세션 라이브 검증(인터페이스만 제공, 스코프 밖) |
| 미디어 다운로드 opt-in(대표 이미지/영상 1건) | 캐러셀 심화 처리(다중 항목 개별 다운로드) |
| 로그인 세션 opt-in 경로(`session_file`) + 명확한 미로그인 에러 안내 | OCR·전사(→ sipher 정규화 단계) |

## 3. De-risk Spike 핵심 사실 (round-09/round-10 실측 — 로그인 세션 필수로 정정)

instaloader 4.15.1 기준, **IG 서버가 익명 `graphql/query` 요청을 거의 항상 403
Forbidden으로 차단한다**(round-09 2026-07-02 최초 실측, round-10 2026-07-02 재확인 —
`Post.from_shortcode`/`Profile.from_username` 둘 다 재현, 두 라운드 모두 동일 증상).
이는 instaloader 자체의 버그가 아니라 **IG 서버 측 anti-scraping 정책의 현재
상태**로 보이며, GitHub #2682/#2678(2026-03~04, 활성)에서 동일 증상이 다수 보고됨.

**round-09는 이 어댑터를 "익명 우선"으로 설계했으나, 실측을 반복할수록 익명 경로가
예외가 아니라 상수적으로 실패한다는 것이 명확해져 round-10에서 "로그인 세션
필수"로 재포지셔닝했다.** 코드는 표준 instaloader API를 올바르게 호출하고,
차단되면:
- 포스트 자체를 못 가져오면 → `InstagramAccessError`(RuntimeError 서브클래스,
  `access_label` 속성 보유)로 "로그인 세션 필요 — session_file 지정 또는 브라우저
  프로필 쿠키 필요"를 명확히 안내(빈 결과를 성공처럼 반환하지 않음, round-10에서
  round-09 P2 팔로우업으로 실제 값이 채워지도록 수정 — §7 참조)
- 댓글만 막히면 → `meta.comments_label = "login_required"`(정직 degrade)
- `meta.ig_access_label`은 **성공 시(포스트를 실제로 가져왔을 때)에만** `meta`에
  등장하며 항상 `"ok"`다. 실패는 `meta`가 아니라 `InstagramAccessError.access_label`
  속성으로 판별한다(§7 불변식 5번, round-09 P2 수정 내역).

## 4. 아키텍처

```
입력: IG 포스트/릴스 URL
   │
   ▼ parse_url — instagram.com 호스트 검증 + /p|reel|tv/<shortcode> 추출
   │     (shortcode 문자열만 안전한 문자 집합으로 정규식 검증 후 instaloader
   │      API에 그대로 전달 — instaloader.Post.from_shortcode는 URL이 아니라
   │      shortcode 인자 하나만 받는 시그니처이므로 canonical URL 재조립
   │      자체가 불필요, round-10 §7 불변식 2번 정정)
   │
   ▼ instaloader.Post.from_shortcode(context, shortcode)
   │     익명(기본, 사실상 항상 403 — §3) 또는 session_file 로그인 컨텍스트
   │     ConnectionException/LoginRequiredException/TypeError(leak) →
   │     InstagramAccessError(access_label="anonymous_blocked" 익명 실패 시 /
   │     "session_failed" 로그인 세션 사용 중 실패 시 — round-10 Post-Review Fix
   │     P2, §7 불변식 6번)로 명확히 안내
   │
   ▼ (opt-in) comments=True → post.get_comments()
   │     실패 시 comments_label="login_required"/"fetch_failed"로 degrade
   │
   ▼ (opt-in) download=True → 대표 미디어 1건 다운로드
   │
출력: 정규화 JSON(§6) + (옵트인) 미디어 파일
```

## 5. 유저 플로우

1. `sipher-instagram fetch <포스트URL>` 또는 sipher 라우터 호출 → §4 파이프라인.
2. 기본은 익명이지만 **거의 항상 403으로 실패한다**(§3, round-10 정정). 실패 시
   에러 메시지에 "Instagram은 로그인 세션이 필요합니다 — session_file 지정 또는
   브라우저 프로필 쿠키 필요"가 명시되고, `InstagramAccessError.access_label`로
   프로그램적으로도 판별 가능하다.
3. 로그인 세션이 있으면 `--session-file`로 opt-in(instaloader
   `save_session_to_file` 결과물, 또는 §9 브라우저 프로필 경로). 실계정 라이브
   검증은 이 라운드도 스코프 밖(§9 참조).

## 6. 데이터 모델 (정규화 출력)

```jsonc
{
  "source": "https://www.instagram.com/p/<code>/",
  "platform": "instagram",
  "body_text": "<캡션>",
  "comments": [],                 // comments=True일 때만 채움
  "ocr_text": [],
  "transcript": null,
  "media_paths": ["downloads/ig_<code>.jpg"],
  "meta": {
    "shortcode": "...", "author": "...", "post_id": "...",
    "likes": 0, "comment_count": 0, "comment_count_captured": 0,
    "comments_label": "not_requested | collected | login_required | fetch_failed",
    "is_video": false,
    "media_label": "none | downloaded | download_failed",
    "ig_access_label": "ok",   // 성공 시에만 meta에 등장, 항상 "ok"(round-10 정정)
    "date_utc": "<ISO>", "fetched_at": "<ISO>"
  }
}
```

**실패 시(포스트 조회 자체가 안 됐을 때)** 이 dict는 반환되지 않는다 — 대신
`InstagramAccessError`(RuntimeError 서브클래스)가 발생하고, `e.access_label`이
`"anonymous_blocked"`(익명 접근이 차단된 경우) 또는 `"session_failed"`
(`session_file`로 로그인 세션을 이미 사용 중인데도 실패한 경우 — 세션 만료 등,
round-10 Post-Review Fix P2)를 담는다. round-09에서는 이 구분이
`meta.ig_access_label` 값으로만 이뤄질 것으로 설계됐으나 실패 경로가 예외로
즉시 튀는 구조라 그 라벨이 항상 죽어있었다(round-09 리뷰 P2) — round-10에서
예외 속성으로 옮겨 실제로 작동하게 정정했고, round-10 독립 리뷰가 지적한
"로그인 세션 실패도 anonymous_blocked로 고정되는" 문제(P2)는 이 라운드의
Post-Review Fix에서 `session_failed` 값을 신설해 해소했다.

## 7. 위험 등급 / 보안 (인라인)

- **work scale: 등급 2.** 공개 콘텐츠 기본이나 로그인 세션 opt-in 경로가 있어
  naver_blog(등급 1)보다 상위. round-10에서 "로그인 세션 사실상 필수"로
  재포지셔닝되며 상향 유지.
- **자산:** session_file(있는 경우 로그인 쿠키/토큰 등가물 — 로컬 신뢰 입력, 경로
  containment 없음. 절대 리포지토리에 커밋하지 않음, `.gitignore` 대상).
- **불변식:**
  1. host 화이트리스트(`instagram.com`만) — SSRF 방어 1차선.
  2. **(round-10 정정)** shortcode를 안전한 문자 집합(`[A-Za-z0-9_-]+`)으로
     정규식 검증 후 instaloader API(URL이 아니라 shortcode 인자)에 그대로
     전달한다 — canonical URL 재구성은 하지 않는다(`Post.from_shortcode`가
     URL을 받지 않는 시그니처이므로 애초에 불필요, round-09 리뷰 P3 정정).
  3. 정직 라벨: 실패를 빈 결과로 위장하지 않음 — `InstagramAccessError`로
     명확히 전파(§3, §6).
  4. 외부 LLM 호출 없음 — 추출은 instaloader 반환 필드 매핑만.
  5. **(round-10 신규)** `ig_access_label`은 성공 경로에서만 `meta`에 등장
     (`"ok"` 고정) — 실패 판별은 `InstagramAccessError.access_label`로 한다
     (round-09 P2 팔로우업, §6 참조).
  6. **(round-10 Post-Review Fix, P2)** `access_label`은 익명 실패
     (`"anonymous_blocked"`)와 로그인 세션 사용 중 실패(`"session_failed"`)를
     구분한다 — `is_anonymous` 분기별로 다른 값을 부여해, 로그인 세션으로 실패한
     호출자가 "익명이라 차단됐다"고 오판하지 않게 한다(독립 리뷰 P2 지적 해소,
     `.handoff/rounds/round-10-absorb-web-review.md` 참조).
- **게이트:** round-09 contract(de-risk spike 선행) → 구현 → round-10 정정
  (.handoff/rounds/round-10-absorb-web-contract.md §④).

## 8. 알려진 한계 (2026-07-02 기준, 정직 기록)

- 익명 접근이 IG 서버 상태에 따라 거의 항상 403으로 실패함(§3, round-09/round-10
  두 라운드 일관 재현) — 이는 이 어댑터의 버그가 아니라 현재 IG 서비스 상태다.
  로그인 세션(`session_file`)이 사실상 필수다. 재발(또는 로그인 세션도 막히는
  경우) 시 instaloader 프로젝트의 최신 릴리스로 업그레이드를 검토해야 할 수 있다.
- 캐러셀(여러 장 게시물)은 대표 미디어 1건만 다운로드한다.
- 실계정 로그인 세션으로의 라이브 검증은 이 어댑터의 어느 라운드에서도 수행되지
  않았다(§9 참조) — `session_file` 경로는 인터페이스 레벨에서만 구현·확인됐다.

## 9. 로그인 세션 확보 경로 (문서·코드 경로만 — 실계정 라이브 검증은 스코프 밖)

**실계정 로그인 실측은 이 프로젝트의 어느 라운드에서도 수행하지 않는다**(Pre-Action
Documentation Rule 대상 — 사용자 자격증명을 다루므로 착수 전 별도 문서화 + 승인이
필요한 작업으로 분류, round-10 §④ 명시).

가능한 세션 확보 경로(문서화만, 실행은 사용자 책임):

1. **instaloader CLI 직접 로그인** — `instaloader --login <username>`을 사용자가
   직접 실행해 대화형으로 로그인하면 `~/.config/instaloader/session-<username>`에
   세션 파일이 저장된다. 이 파일 경로를 `session_file=`(또는 CLI `--session-file`)
   로 넘기면 이 어댑터가 그대로 로드한다(`_build_context`, `L.load_session_from_file`).
2. **기존 브라우저 프로필 쿠키 재사용** — `adapters/facebook/`이 이미 사용 중인
   persistent Firefox 프로필(`.fbprofile`)에 Instagram에도 로그인해두면, 같은
   브라우저 프로필 안에 `facebook.com`/`instagram.com` 쿠키가 도메인별로 각각
   공존한다(도메인 간 쿠키 공유는 안 되지만, 같은 프로필 파일 안에 두 도메인
   쿠키가 나란히 저장되는 것은 브라우저의 표준 동작). 다만 instaloader는 브라우저
   쿠키 jar 형식을 직접 읽지 않고 자체 세션 파일 포맷(`import_session`)을 쓰므로,
   브라우저 로그인 → instaloader 세션 파일로 변환하는 별도 단계가 필요하다
   (`instaloader.Instaloader.load_session_from_file`은 이미 이 어댑터가 호출하고
   있음 — 세션 파일을 "어떻게 만드는지"만 사용자 몫으로 남는다).
3. 두 경로 모두 **이 어댑터의 코드 변경 없이** 이미 사용 가능하다(`session_file`
   파라미터가 round-09부터 이미 존재) — round-10은 이 경로를 문서화하고 실패
   메시지에서 안내했을 뿐, 새 로그인 메커니즘을 추가하지 않았다.
