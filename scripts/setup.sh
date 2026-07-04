#!/usr/bin/env bash
# sipher setup — LITE/FULL 프로필 venv + 의존성 설치.
# 사용법: scripts/setup.sh [lite|full] [--browsers]
#   lite(기본): 공개 콘텐츠 + 무료 API OCR. 개인 로그인 세션 불필요.
#   full      : lite + threads/facebook/instagram(로그인 세션 필요) + whisper 권장.
#   --browsers: playwright chromium 브라우저 바이너리까지 설치(threads/facebook/web-tier2).
#
# 설계 원칙(docs/01-overview.md §10 degrade): 시스템 도구(ffmpeg/whisper/브라우저)가
# 없어도 setup은 막지 않는다 — 안내만 하고 종료 시 체크리스트를 출력한다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

PROFILE="lite"
INSTALL_BROWSERS=0

for arg in "$@"; do
  case "$arg" in
    lite|full)
      PROFILE="$arg"
      ;;
    --browsers)
      INSTALL_BROWSERS=1
      ;;
    -h|--help)
      echo "사용법: $0 [lite|full] [--browsers]"
      exit 0
      ;;
    *)
      echo "[에러] 알 수 없는 인자: $arg (lite|full|--browsers만 허용)" >&2
      exit 1
      ;;
  esac
done

if [[ "$PROFILE" != "lite" && "$PROFILE" != "full" ]]; then
  echo "[에러] profile은 lite 또는 full이어야 합니다. 받은 값: $PROFILE" >&2
  exit 1
fi

REQ_FILE="requirements-${PROFILE}.txt"
if [[ ! -f "$REQ_FILE" ]]; then
  echo "[에러] $REQ_FILE 을 찾을 수 없습니다(루트에서 실행했는지 확인하세요)." >&2
  exit 1
fi

# python 존재 확인(fail-fast)
PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "[에러] python(3) 실행 파일을 찾을 수 없습니다. Python 3.10+ 를 설치하세요." >&2
  exit 1
fi

echo "[sipher setup] profile=$PROFILE python=$($PYTHON_BIN --version 2>&1)"

# venv 생성(이미 있으면 재사용 — 파괴 금지)
VENV_DIR=".venv"
if [[ -d "$VENV_DIR" ]]; then
  echo "[sipher setup] 기존 venv 재사용: $VENV_DIR"
else
  echo "[sipher setup] venv 생성: $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  # Windows Git Bash venv 레이아웃 대응(Scripts/)
  VENV_PY="$VENV_DIR/Scripts/python.exe"
fi
if [[ ! -x "$VENV_PY" ]]; then
  echo "[에러] venv 안에서 python 실행 파일을 찾지 못했습니다: $VENV_DIR" >&2
  exit 1
fi

echo "[sipher setup] pip 업그레이드"
"$VENV_PY" -m pip install --upgrade pip

echo "[sipher setup] $REQ_FILE 설치"
"$VENV_PY" -m pip install -r "$REQ_FILE"

if [[ "$PROFILE" == "full" || "$INSTALL_BROWSERS" -eq 1 ]]; then
  echo "[sipher setup] playwright chromium 설치 시도(threads/facebook/web-tier2용)"
  if "$VENV_PY" -m playwright install chromium; then
    echo "[sipher setup] playwright chromium 설치 완료"
  else
    echo "[sipher setup][경고] playwright chromium 설치 실패 — 해당 기능은 나중에 필요할 때" \
         "'$VENV_PY -m playwright install chromium' 를 직접 실행하세요." >&2
  fi
else
  echo "[sipher setup] playwright 브라우저 설치 생략(lite 기본, --browsers로 강제 가능)"
fi

echo ""
echo "======================================================================"
echo " sipher setup 완료 (profile=$PROFILE)"
echo "======================================================================"
echo "필요한 것 체크리스트 (docs/08-packaging.md 상세):"
echo "  [ ] GEMINI_API_KEY  — 무료비전 OCR(core/llm_free.py). .env.local에 설정."
echo "  [ ] ffmpeg          — youtube 포맷 병합/자막 변환 권장(시스템 패키지)."
if [[ "$PROFILE" == "full" ]]; then
  echo "  [ ] whisper 전사 도구 — core/transcribe.py, GPU large-v3 권장."
  echo "  [ ] 로그인 세션      — threads(deep 크롤)/facebook/instagram 필수."
fi
echo "  [ ] playwright chromium — threads/facebook/web-tier2 사용 시(--browsers로 설치)."
echo ""
echo "없는 도구는 기능이 막히지 않고 정직 라벨로 degrade됩니다(예:"
echo "conversion_label=\"skipped_no_tool\"). 자세한 내용은 docs/08-packaging.md 참조."
