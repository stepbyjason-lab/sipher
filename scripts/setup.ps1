# sipher setup — LITE/FULL 프로필 venv + 의존성 설치 (PowerShell)
# 사용법: scripts/setup.ps1 [-Profile lite|full] [-Browsers]
#   -Profile lite(기본): 공개 콘텐츠 + 무료 API OCR. 개인 로그인 세션 불필요.
#   -Profile full      : lite + threads/facebook/instagram(로그인 세션 필요) + whisper 권장.
#   -Browsers          : playwright chromium 브라우저 바이너리까지 설치.
#
# 설계 원칙(docs/01-overview.md §10 degrade): 시스템 도구(ffmpeg/whisper/브라우저)가
# 없어도 setup은 막지 않는다 — 안내만 하고 종료 시 체크리스트를 출력한다.

param(
    [ValidateSet("lite", "full")]
    [string]$Profile = "lite",
    [switch]$Browsers
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
Set-Location $RootDir

$ReqFile = "requirements-$Profile.txt"
if (-not (Test-Path $ReqFile)) {
    Write-Error "[에러] $ReqFile 을 찾을 수 없습니다(루트에서 실행했는지 확인하세요)."
    exit 1
}

# python 존재 확인(fail-fast)
$PythonBin = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonBin = $candidate
        break
    }
}
if (-not $PythonBin) {
    Write-Error "[에러] python(3) 실행 파일을 찾을 수 없습니다. Python 3.10+ 를 설치하세요."
    exit 1
}

$pyVersion = & $PythonBin --version 2>&1
Write-Host "[sipher setup] profile=$Profile python=$pyVersion"

# venv 생성(이미 있으면 재사용 — 파괴 금지)
$VenvDir = ".venv"
if (Test-Path $VenvDir) {
    Write-Host "[sipher setup] 기존 venv 재사용: $VenvDir"
} else {
    Write-Host "[sipher setup] venv 생성: $VenvDir"
    & $PythonBin -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[에러] venv 생성 실패"
        exit 1
    }
}

$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    # POSIX 레이아웃 대응(WSL 등에서 생성된 venv)
    $VenvPy = Join-Path $VenvDir "bin/python"
}
if (-not (Test-Path $VenvPy)) {
    Write-Error "[에러] venv 안에서 python 실행 파일을 찾지 못했습니다: $VenvDir"
    exit 1
}

Write-Host "[sipher setup] pip 업그레이드"
& $VenvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Error "[에러] pip 업그레이드 실패"
    exit 1
}

Write-Host "[sipher setup] $ReqFile 설치"
& $VenvPy -m pip install -r $ReqFile
if ($LASTEXITCODE -ne 0) {
    Write-Error "[에러] 의존성 설치 실패"
    exit 1
}

if ($Profile -eq "full" -or $Browsers) {
    Write-Host "[sipher setup] playwright chromium 설치 시도(threads/facebook/web-tier2용)"
    & $VenvPy -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[sipher setup] playwright chromium 설치 실패 — 해당 기능은 나중에 필요할 때 '$VenvPy -m playwright install chromium' 를 직접 실행하세요."
    } else {
        Write-Host "[sipher setup] playwright chromium 설치 완료"
    }
} else {
    Write-Host "[sipher setup] playwright 브라우저 설치 생략(lite 기본, -Browsers로 강제 가능)"
}

Write-Host ""
Write-Host "======================================================================"
Write-Host " sipher setup 완료 (profile=$Profile)"
Write-Host "======================================================================"
Write-Host "필요한 것 체크리스트 (docs/08-packaging.md 상세):"
Write-Host "  [ ] GEMINI_API_KEY  - 무료비전 OCR(core/llm_free.py). .env.local에 설정."
Write-Host "  [ ] ffmpeg          - youtube 포맷 병합/자막 변환 권장(시스템 패키지)."
if ($Profile -eq "full") {
    Write-Host "  [ ] whisper 전사 도구 - core/transcribe.py, GPU large-v3 권장."
    Write-Host "  [ ] 로그인 세션      - threads(deep 크롤)/facebook/instagram 필수."
}
Write-Host "  [ ] playwright chromium - threads/facebook/web-tier2 사용 시(-Browsers로 설치)."
Write-Host ""
Write-Host "없는 도구는 기능이 막히지 않고 정직 라벨로 degrade됩니다(예:"
Write-Host 'conversion_label="skipped_no_tool"). 자세한 내용은 docs/08-packaging.md 참조.'
