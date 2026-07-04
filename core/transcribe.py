r"""
sipher 전사 backend 사다리 — round-27(로컬 whisper → 무료 Groq Whisper).

round-06까지는 외부 `whisper-transcribe` 도구(faster-whisper, GPU/CUDA, 자체
`.venv`)를 subprocess로 호출하는 단일 backend였다. round-27은 OCR round-26의
무료 provider 사다리 패턴을 전사 영역에 맞춰 적용한다:

1. local whisper — `WHISPER_TRANSCRIBE_DIR`가 구성되어 있으면 항상 최우선.
2. Groq Whisper 무료 tier — local이 unavailable이거나 per-item 실패했을 때만.
   단일 `GROQ_API_KEY` 안에서 Groq가 제공한 모델별(whisper-large-v3-turbo →
   whisper-large-v3) 무료 버킷만 사용한다 — 멀티계정/멀티키 우회가 아니다.
3. 전부 불가 — 기존 `skipped_no_tool` degrade 유지(호출자 `core.normalize.
   enrich_transcribe` 계약 무변경).

설계 원칙(round-06 계승 + round-27 확장):
- **도구 재구현 금지**: faster-whisper를 직접 import하지 않는다(local backend).
  Groq는 `requests` 직접 호출만 — SDK 신규 의존 추가 금지(계약 §전문 금지).
- **graceful degradation**: 어느 backend도 없으면 예외를 던지지 않고
  `is_available() -> False`로 신호한다(§10). per-item 실패는 상위로 죽이지
  않고 사다리를 한 단 더 내려간다(local 실패 → Groq 폴백 → 정직 skip).
- **subprocess 안전**: local은 리스트 인자(`shell=False`), 파일 존재 확인 후 호출.
- **키 값 절대 로그·출력 금지**: `GROQ_API_KEY` 원문은 로그·예외 메시지 어디에도
  남기지 않는다(`_redact` 방어심층, `core/llm_free.py`와 동형).
- **`WHISPER_TRANSCRIBE_DIR` 신뢰 경계**: 로컬 신뢰 입력(임의 실행 경로 가능) —
  단일 사용자 로컬 환경 전제. 멀티테넌트/서버 배포 시 재검토 필요(round-06 계승).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests

__all__ = ["transcribe_media", "is_available", "TranscribeError"]

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# ── local whisper 설정(round-06 계승) ───────────────────────────────────────
# 개인 머신 경로를 하드코딩하지 않는다 — 도구 위치는 WHISPER_TRANSCRIBE_DIR로 지정.
# 미설정이면 도구 없음으로 간주하고 skipped_no_tool로 정직 degrade한다(공개 안전).
_DEFAULT_TOOL_DIR = ""
_DEFAULT_TIMEOUT_SECONDS = 1200  # 20분 — 전사는 길 수 있음(관대한 기본값)

# ── Groq Whisper 설정(round-27) ─────────────────────────────────────────────
_GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_MODEL_TURBO = "whisper-large-v3-turbo"
_GROQ_MODEL_FALLBACK = "whisper-large-v3"
_GROQ_MODELS_ORDER = (_GROQ_MODEL_TURBO, _GROQ_MODEL_FALLBACK)
_GROQ_TIMEOUT_SECONDS = 120
_GROQ_MAX_RETRY_AFTER_SECONDS = 60  # 이 이하면 같은 모델 1회 대기 재시도(SHOULD)

# Groq audio/transcriptions가 받는 컨테이너/코덱 화이트리스트(문서화된 지원 포맷).
# 이 밖의 확장자는 400을 맞으러 가지 않고 사전에 skip한다(계약 §업로드 사전검사).
_GROQ_SUPPORTED_EXTS = {
    ".flac", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".ogg", ".wav", ".webm",
}
# 오디오만 추출해도 되는(=ffmpeg로 오디오 트랙만 뽑아 업로드 가능한) 영상 컨테이너.
_VIDEO_CONTAINER_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".webm", ".mov", ".mpeg"}

_GROQ_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25MB — Groq 무료 tier 업로드 상한(문서화값)


class TranscribeError(RuntimeError):
    """전사 호출 실패(도구/키 없음·타임아웃·비정상 종료·출력 누락 등)."""


def _load_env_file(path: Path) -> dict[str, str]:
    """`.env.local` 형식(`KEY=VALUE`, `#` 주석)을 dict로 읽는다. 파일 없으면 빈 dict.

    core/llm_free.py::_load_env_file과 동일한 패턴(모듈 간 결합을 피하기 위해
    자체 구현 — round-03 llm_free.py 자체도 독립 `_config()`를 가짐).
    """
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            env[key] = value
    return env


def _local_config() -> tuple[Path | None, int]:
    """(tool_dir 또는 None, timeout_seconds). `.env.local` → os.environ 순.

    WHISPER_TRANSCRIBE_DIR가 설정되지 않으면 tool_dir=None(도구 미구성) — 호출측이
    skipped_no_tool로 degrade한다(개인 경로 하드코딩 없음, round-23 공개 대비).
    """
    env = _load_env_file(_ROOT / ".env.local")
    tool_dir = (
        env.get("WHISPER_TRANSCRIBE_DIR")
        or os.environ.get("WHISPER_TRANSCRIBE_DIR")
        or _DEFAULT_TOOL_DIR
    ).strip()
    timeout_raw = env.get("WHISPER_TIMEOUT_SECONDS") or os.environ.get("WHISPER_TIMEOUT_SECONDS")
    try:
        timeout = int(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        timeout = _DEFAULT_TIMEOUT_SECONDS
    return (Path(tool_dir) if tool_dir else None), timeout


def _venv_python(tool_dir: Path) -> Path:
    return tool_dir / ".venv" / "Scripts" / "python.exe"


def _transcribe_script(tool_dir: Path) -> Path:
    return tool_dir / "transcribe.py"


def _local_is_available() -> bool:
    """local whisper 사용 가능 여부(도구 디렉토리·venv python·스크립트 존재만 확인).

    네트워크·모델 로드 없음 — 파일시스템 확인만(빠르게 skip 판단 가능).
    """
    tool_dir, _ = _local_config()
    return bool(tool_dir) and (
        tool_dir.is_dir()
        and _venv_python(tool_dir).is_file()
        and _transcribe_script(tool_dir).is_file()
    )


def _groq_api_key() -> str | None:
    """`GROQ_API_KEY`. `.env.local` → os.environ 순(core/llm_free.py `_config`와 동일 원칙).

    ※ 멀티계정/멀티키 무료한도 우회는 provider ToS 위반이라 지원하지 않는다 — 키는
    1개만. turbo/v3 모델별 버킷은 그 단일 키 안에서 Groq가 제공한 것이다.
    """
    env = _load_env_file(_ROOT / ".env.local")
    return env.get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY") or None


def _groq_is_available() -> bool:
    """Groq Whisper 사용 가능 여부(키 존재만 확인, 네트워크 호출 없음)."""
    return bool(_groq_api_key())


def is_available() -> bool:
    """전사 사용 가능 여부 — local whisper 또는 Groq(`GROQ_API_KEY`) 중 하나라도 가능하면 True.

    `enrich_transcribe`의 `tool_available` 분기(`skipped_no_tool` vs 시도)가 이
    값을 그대로 쓴다 — Groq만 가용한 환경에서도 시도 경로로 정상 분기된다(round-27).
    """
    return _local_is_available() or _groq_is_available()


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def _redact(text: str, secret: str | None) -> str:
    """`text`에서 `secret`(API 키 등) 부분 문자열을 마스킹한다(`core/llm_free.py`와 동형)."""
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


# ── local whisper backend(round-06 계승, 함수명만 `_transcribe_local`로 이동) ──


def _transcribe_local(
    media_path: Path,
    *,
    model: str | None,
    device: str | None,
    compute: str | None,
    lang: str,
) -> dict:
    """local whisper-transcribe 도구 subprocess 호출 → `{"text","model","backend":"local"}`.

    whisper-transcribe CLI(`D:\\Code\\_tools\\whisper-transcribe\\transcribe.py`)를
    subprocess로 호출한다. `model`/`device`/`compute`를 지정하지 않으면 해당
    인자를 아예 넘기지 않아 도구 자체 기본값(large-v3/cuda/float16)에 위임한다.

    도구가 없거나 실패하면 `TranscribeError`를 던진다 — dispatcher(`transcribe_media`)가
    Groq 폴백 여부를 판단한다(round-27).
    """
    tool_dir, timeout = _local_config()
    if tool_dir is None:
        raise TranscribeError(
            "WHISPER_TRANSCRIBE_DIR가 설정되지 않았습니다 "
            "(.env.local에 whisper-transcribe 도구 경로를 지정하세요)"
        )
    venv_python = _venv_python(tool_dir)
    script = _transcribe_script(tool_dir)
    if not (tool_dir.is_dir() and venv_python.is_file() and script.is_file()):
        raise TranscribeError(
            f"whisper-transcribe 도구를 찾을 수 없습니다: {tool_dir} "
            "(WHISPER_TRANSCRIBE_DIR 확인 또는 도구 설치 필요)"
        )

    with tempfile.TemporaryDirectory(prefix="sipher_whisper_") as tmpdir_str:
        outdir = Path(tmpdir_str)
        # 리스트 인자로 subprocess 호출 — shell=False, 셸 메타문자 해석 없음.
        args: list[str] = [
            str(venv_python),
            str(script),
            str(media_path),
            "--lang", lang,
            "--outdir", str(outdir),
        ]
        if model:
            args += ["--model", model]
        if device:
            args += ["--device", device]
        if compute:
            args += ["--compute", compute]

        try:
            proc = subprocess.run(
                args,
                cwd=str(tool_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as e:
            _log.warning("whisper 전사 타임아웃(%ds 초과): %s", timeout, media_path.name)
            raise TranscribeError(
                f"whisper 전사 타임아웃({timeout}s 초과): {media_path.name}"
            ) from e
        except OSError as e:
            _log.warning("whisper subprocess 실행 실패: %s", e)
            raise TranscribeError(f"whisper subprocess 실행 실패: {e}") from e

        if proc.returncode != 0:
            _log.warning(
                "whisper 전사 실패(returncode=%d): %s", proc.returncode, media_path.name
            )
            raise TranscribeError(
                f"whisper 전사 실패(returncode={proc.returncode}) {media_path.name}: "
                f"{_truncate(proc.stderr or proc.stdout or '(출력 없음)')}"
            )

        txt_path = outdir / f"{media_path.stem}.txt"
        if not txt_path.is_file():
            raise TranscribeError(
                f"whisper 출력 파일이 없습니다: {txt_path} "
                f"(stdout: {_truncate(proc.stdout)})"
            )
        text = txt_path.read_text(encoding="utf-8").strip()

    used_model = model or "large-v3"  # 도구 자체 기본값(미지정 시)과 동일 표기
    _log.info("whisper(local) 전사 완료: %s (model=%s)", media_path.name, used_model)
    return {"text": text, "model": used_model, "backend": "local"}


# ── Groq Whisper backend(round-27) ──────────────────────────────────────────


class _GroqRateLimited(TranscribeError):
    """429 — 시간창 rate limit(일시). dispatcher가 다음 모델로 사다리를 내린다.

    `retry_after`(초)는 응답 `Retry-After` 헤더 파싱값 또는 None(헤더 없음/파싱 실패).
    """

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _extract_audio(media_path: Path, *, outdir: Path) -> Path:
    """ffmpeg로 오디오 트랙만 추출(16kHz mono mp3 ~48kbps) → temp mp3 경로.

    분할(chunking)은 비목표 — 1회 오디오 추출만. 실패 시 `TranscribeError`.
    """
    out_path = outdir / f"{media_path.stem}_audio.mp3"
    args = [
        "ffmpeg", "-y", "-i", str(media_path),
        "-vn",  # 비디오 스트림 제거
        "-ar", "16000", "-ac", "1", "-b:a", "48k",
        str(out_path),
    ]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GROQ_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        raise TranscribeError(f"ffmpeg 오디오 추출 타임아웃: {media_path.name}") from e
    except OSError as e:
        raise TranscribeError(f"ffmpeg 실행 실패: {e}") from e
    if proc.returncode != 0 or not out_path.is_file():
        raise TranscribeError(
            f"ffmpeg 오디오 추출 실패(returncode={proc.returncode}) {media_path.name}: "
            f"{_truncate(proc.stderr or '(출력 없음)')}"
        )
    return out_path


def _prepare_upload_path(media_path: Path, *, tmpdir: Path) -> Path | None:
    """Groq 업로드용 최종 파일 경로를 결정한다. 업로드 불가면 None(정직 skip 신호).

    사전검사(계약 §업로드 사전검사):
    1. 미지원 확장자는 ffmpeg로 오디오 추출을 시도(영상 컨테이너면), 그 외는 skip.
    2. 영상 컨테이너이거나 25MB 초과면 ffmpeg가 있을 때만 오디오만 추출해 그것을
       업로드 후보로 삼는다.
    3. ffmpeg가 없거나 추출 후에도 25MB를 초과하면 HTTP 요청 없이 None(skip).
    """
    ext = media_path.suffix.lower()
    size = media_path.stat().st_size
    needs_extraction = ext in _VIDEO_CONTAINER_EXTS or size > _GROQ_MAX_UPLOAD_BYTES
    unsupported = ext not in _GROQ_SUPPORTED_EXTS

    if not needs_extraction and not unsupported:
        return media_path if size <= _GROQ_MAX_UPLOAD_BYTES else None

    # 오디오 추출이 필요하거나(영상/과대용량) 미지원 확장자 — ffmpeg로 우회 시도.
    if _ffmpeg_path() is None:
        _log.info(
            "Groq 업로드 사전검사: ffmpeg 없음 — 정직 skip(%s, %d bytes)",
            media_path.name, size,
        )
        return None
    try:
        extracted = _extract_audio(media_path, outdir=tmpdir)
    except TranscribeError as e:
        _log.warning("Groq 업로드용 오디오 추출 실패 — skip: %s (%s)", media_path.name, e)
        return None
    if extracted.stat().st_size > _GROQ_MAX_UPLOAD_BYTES:
        _log.info(
            "Groq 업로드 사전검사: 오디오 추출 후에도 25MB 초과 — 정직 skip(%s)",
            media_path.name,
        )
        return None
    return extracted


def _parse_retry_after(resp: requests.Response) -> int | None:
    """`Retry-After` 헤더(초 단위 정수 문자열) 파싱. 헤더 없음/파싱 실패면 None.

    Groq는 초 단위 정수를 반환한다(HTTP-date 형식은 다루지 않음 — 관측 범위 밖).
    """
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _call_groq(
    upload_path: Path,
    *,
    api_key: str,
    model: str,
    lang: str,
) -> dict:
    """Groq `/audio/transcriptions` 1회 호출(단일 모델). 성공 시 `{"text","model"}`.

    429는 `_GroqRateLimited`로 던져 dispatcher가 사다리(Retry-After 1회 대기 또는
    다음 모델 폴백)를 판단하게 한다. 401/403은 `TranscribeError`(backend unavailable
    취급은 호출측). 5xx/네트워크예외/JSON 파싱 실패도 `TranscribeError`(per-item degrade).
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {
        "model": model,
        "response_format": "verbose_json",
    }
    if lang:
        data["language"] = lang

    try:
        with upload_path.open("rb") as f:
            files = {"file": (upload_path.name, f)}
            resp = requests.post(
                _GROQ_API_URL,
                headers=headers,
                data=data,
                files=files,
                timeout=_GROQ_TIMEOUT_SECONDS,
            )
    except requests.exceptions.RequestException as e:
        # timeout·connection error 등 — 세션을 죽이지 않고 해당 아이템만 degrade.
        raise TranscribeError(
            f"Groq 전사 네트워크 오류: {_redact(str(e), api_key)}"
        ) from e

    if resp.status_code == 429:
        raise _GroqRateLimited(
            f"Groq rate-limit(HTTP 429, model={model})",
            retry_after=_parse_retry_after(resp),
        )
    if resp.status_code in (401, 403):
        raise TranscribeError(f"Groq 인증/권한 오류(HTTP {resp.status_code})")
    if resp.status_code in (400, 413):
        # 사전검사를 통과했는데도 도달한 경우(포맷/용량) — 해당 아이템만 정직 skip.
        raise TranscribeError(
            f"Groq 요청 거부(HTTP {resp.status_code}): {_truncate(resp.text)}"
        )
    if resp.status_code >= 500:
        raise TranscribeError(f"Groq 서버 오류(HTTP {resp.status_code})")
    if resp.status_code != 200:
        raise TranscribeError(
            f"Groq 전사 실패(HTTP {resp.status_code}): {_truncate(resp.text)}"
        )

    try:
        payload = resp.json()
        text = payload["text"]
    except (ValueError, KeyError) as e:
        raise TranscribeError(f"Groq 응답 파싱 실패: {type(e).__name__}") from e

    return {"text": text.strip(), "model": model}


def _transcribe_groq(media_path: Path, *, lang: str) -> dict:
    """Groq 무료 tier 전사 → `{"text","model","backend":"groq"}`.

    모델 순서: whisper-large-v3-turbo → whisper-large-v3(429 시에만 폴백).
    Retry-After ≤60s면 같은 모델 1회 대기 후 재시도(SHOULD) — RPM 순간폭주로
    두 버킷을 연달아 태우는 것을 완화한다(계약 §429 처리).
    """
    api_key = _groq_api_key()
    if not api_key:
        raise TranscribeError("GROQ_API_KEY가 설정되지 않았습니다")

    with tempfile.TemporaryDirectory(prefix="sipher_groq_upload_") as tmpdir_str:
        upload_path = _prepare_upload_path(media_path, tmpdir=Path(tmpdir_str))
        if upload_path is None:
            raise TranscribeError(
                f"Groq 업로드 불가(용량/포맷) — 정직 skip: {media_path.name}"
            )

        last_err: TranscribeError | None = None
        for model in _GROQ_MODELS_ORDER:
            try:
                result = _call_groq(upload_path, api_key=api_key, model=model, lang=lang)
            except _GroqRateLimited as e:
                last_err = e
                retry_after = e.retry_after
                if retry_after is not None and retry_after <= _GROQ_MAX_RETRY_AFTER_SECONDS:
                    _log.info(
                        "Groq 429(model=%s) — Retry-After %ds 대기 후 동일 모델 1회 재시도",
                        model, retry_after,
                    )
                    time.sleep(retry_after)
                    try:
                        result = _call_groq(upload_path, api_key=api_key, model=model, lang=lang)
                    except _GroqRateLimited as e2:
                        last_err = e2
                        _log.info("Groq 429(model=%s) 재시도도 429 — 다음 모델 폴백", model)
                        continue
                else:
                    _log.info("Groq 429(model=%s) — 다음 모델 폴백", model)
                    continue
            _log.info("Groq 전사 완료: %s (model=%s)", media_path.name, result["model"])
            return {**result, "backend": "groq"}

        # 전 모델 429 — 해당 아이템만 정직 skip(세션 dead 마킹 없음).
        raise TranscribeError(
            f"Groq 전사 rate-limit 전 모델 소진 — 정직 skip: {media_path.name}"
        ) from last_err


def transcribe_media(
    path: str | Path,
    *,
    model: str | None = None,
    device: str | None = None,
    compute: str | None = None,
    lang: str | None = None,
) -> dict:
    """오디오/영상 파일 → `{"text": str, "model": str, "backend": "local"|"groq"}`.

    사다리(round-27, 계약 정본): local whisper 우선 → local unavailable 또는
    per-item `TranscribeError` → Groq 무료 tier 폴백(가능 시) → 전부 불가면
    `TranscribeError`(호출자 `enrich_transcribe`가 `is_available()`로 먼저 확인해
    `skipped_no_tool`로 degrade하는 것을 전제로 한다. 이 함수의 예외 계약은
    round-06과 동일 — dispatcher 내부 폴백은 이 함수를 벗어나지 않는다).

    `lang` 미지정(None)이면 사용자 언어(SIPHER_LANG, OS locale 자동감지 —
    `core.lang.resolve_lang`, round-20)를 쓴다. 명시 인자가 항상 우선(하위호환).

    `model`/`device`/`compute`는 local backend 전용 인자다(round-06 계승) — Groq
    폴백 시에는 무시된다(Groq는 자체 모델 사다리 turbo→v3를 쓴다, 계약 §사다리).
    """
    media_path = Path(path)
    if not media_path.exists():
        raise TranscribeError(f"미디어 파일이 없습니다: {media_path}")
    if lang is None:
        from .lang import resolve_lang  # 지연 import — 도구 부재 환경에서도 모듈 로드 가볍게
        lang = resolve_lang()

    local_ok = _local_is_available()
    groq_ok = _groq_is_available()

    if local_ok:
        try:
            return _transcribe_local(
                media_path, model=model, device=device, compute=compute, lang=lang
            )
        except TranscribeError as e:
            if not groq_ok:
                raise
            _log.warning(
                "local whisper 전사 실패 — Groq 폴백 시도: %s (%s)",
                media_path.name, e,
            )
            return _transcribe_groq(media_path, lang=lang)

    if groq_ok:
        return _transcribe_groq(media_path, lang=lang)

    raise TranscribeError(
        "전사 backend가 구성되지 않았습니다 "
        "(WHISPER_TRANSCRIBE_DIR 또는 GROQ_API_KEY 중 하나를 .env.local에 지정하세요)"
    )
