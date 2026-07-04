# sipher-fb — 보안 Intake (마디 Gate 3 표면)

- **상태:** 설계(design) · **작성:** 2026-06-30
- **왜 이 문서:** FB 쿠키/세션은 **자격증명(credential)**이고 Playwright는 **외부 실행**이라 마디 등급 3 보안 렌즈가 걸린다(docs/12 Gate 3). 풀 threat model은 구현 라운드에서 보강하되, 설계 단계의 자산·경계·불변식을 여기 고정한다.

## 1. 자산 (Assets)

| 자산 | 민감도 | 위치 |
|---|---|---|
| FB 로그인 세션 쿠키(`c_user`, `xs` 등) | **높음** — 탈취 시 계정 장악 | persistent profile dir 또는 cookies.txt |
| 로그인된 브라우저 프로필(user_data_dir) | **높음** — 세션·자동완성 포함 | 로컬 전용 디렉토리 |
| 서명된 CDN URL(`oh=`/`oe=`) | 중 — 1~2h 만료, 재사용 시 위치 추적 | 메모리/manifest |
| 다운로드 미디어 | 중 — 개인 사진 포함 가능(PII) | 출력 media/ |

## 2. 신뢰 경계 (Trust Boundaries)

- 사용자 FB 계정 ↔ 도구: 도구는 **사용자 본인 세션**만 사용. 타인 계정·다중 계정 로테이션 ❌.
- **로컬 전용**: 쿠키·프로필·미디어는 사용자 머신을 떠나지 않는다. 외부 API 전송 없음(어댑터는 무-네트워크-유출).
- 도구 ↔ Facebook: 읽기 전용(공개적으로 브라우저가 로드 가능한 것만). 로그인월·비공개·페이월 우회 ❌.

## 3. 인증 전략 (3겹, DX 좋은 순)

| 순위 | 방식 | 사용자 행동 | 비고 |
|---|---|---|---|
| 기본 | **④ persistent context** (`launch_persistent_context(user_data_dir=...)`) | `sipher-fb login` → 창에서 FB 로그인 1회 | 쿠키 파일 안 만짐. 만료 시 재로그인 1회 |
| 보조 | **③ cookies-from-browser** (`browser_cookie3`, yt-dlp/gallery-dl 방식) | 평소 브라우저에 FB 로그인 상태 유지 | ⚠️ Chrome v127+ App-Bound Encryption → **Firefox 경로 우선** |
| fallback | **① cookies.txt** (Netscape, 현 자작 수집 도구 방식) | 확장으로 export | headless/서버 전용. ⚠️ `Get cookies.txt`(비-LOCALLY)는 멀웨어 이력 → 안내 금지 |

## 4. 보안 불변식 (Security Invariants) — 구현 시 강제

1. **쿠키·프로필·미디어는 절대 git에 안 들어간다.** `.gitignore`에 `**/cookies/`, `**/user_data/`, `**/media/`, `*.cookies.txt`. → 수락 시 grep guard.
   - **profile_dir 규약:** persistent `profile_dir`은 반드시 `.gitignore`가 덮는 경로 하위(`user_data/`·`profiles/`)에 둔다. 경로명이 규약 밖이어도 Chromium 내부 패턴(`**/Default/`·`**/*.ldb`·`Login Data`·`Cookies`)이 2중 방어한다. CLI 레이어는 profile_dir 기본값을 덮인 경로로 고정할 것.
2. **로그에 쿠키 값·서명 URL 전체를 남기지 않는다.** URL은 path 해시/redact만(자작 수집 도구 manifest가 이미 redact). → 코드 리뷰 lens.
3. **본인 계정만.** 계정 로테이션·다중 세션·anti-detect 기능 미구현(범위 밖).
4. **정중함(rate limit 회피):** scroll 간 2.2s, post 단위 직렬 다운로드 유지. 차단 시 백오프. → DoS·계정 차단 위험 완화.
5. **정직 라벨:** 풀사이즈 회수 실패를 `fullsize_label: thumbnail_only`로 표기. 허위 "원본" 주장 금지.

## 5. 위협 (구현 라운드에서 threat model로 확장)

| 위협 | 완화 |
|---|---|
| 쿠키 파일/프로필 유출 → 계정 장악 | git 제외 + 로컬 전용 + 로그 redact (불변식 1·2) |
| 과도 요청 → FB 계정 차단 | 정중함·백오프 (불변식 4) |
| 봇 탐지(랜덤 DOM·핑거프린팅) | text/attribute 기반 선택자, 실패 시 정직 보고(우회 시도 안 함) |
| 다운로드 미디어의 PII(타인·아동 사진) | 공개 게시 차단은 다운스트림 책임. 어댑터는 로컬 저장까지, 외부 업로드 ❌ |

## 6. 수락 기준 (Gate 5 보안 통과 증거)

- [ ] `.gitignore`에 쿠키/프로필/미디어 제외 + `git status`로 staged 누출 0 확인
- [ ] 로그 출력에 쿠키 값·전체 서명 URL 미포함(샘플 실행 로그 검사)
- [ ] persistent profile 경로가 출력 데이터 경로와 분리
- [ ] auth_mode가 meta에 정직 기록
- [ ] (등급 3) 구현 후 보안 리뷰 lens + threat model 보강
