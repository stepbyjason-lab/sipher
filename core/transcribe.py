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
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests

__all__ = ["transcribe_media", "is_available", "probe_audio", "TranscribeError"]

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

# R27-H1: `verbose_json` 구간 신호로 말 없는 구간을 거른다 — Whisper 표준 무음 판정값
# (faster-whisper `no_speech_threshold`·`log_prob_threshold`·`compression_ratio_threshold`).
# R1 거름은 「말 없음 확률이 높고 *그리고* 글의 확신이 낮다」일 때만이다. R1은 압축률로 거르지 않는다 — 높은
# 압축률은 반복되는 실제 말(구호·노래)일 수 있어 `quality.segments`에 신호로만 남긴다(R27-H2 규칙은 아래).
_NO_SPEECH_PROB_MIN = 0.6
_AVG_LOGPROB_MAX = -1.0
_COMPRESSION_RATIO_MAX = 2.4  # 판정에 쓰지 않는다 — 표준값 기록(기획서 §4 R1)
_SEGMENT_SIGNALS = ("no_speech_prob", "avg_logprob", "compression_ratio")

# R27-H2: turbo는 `no_speech_prob`를 늘 0으로 줘 R1이 걸지 못한다 — 흔한 환각 문구가 「짧은 창(낮은 압축률)」
# 안에 홀로 있고 모델이 확신할 때만 거른다. 문구는 공백·문장부호를 지운 글로 정확히 맞춰 본다(부분 일치 없음).
# 신호는 30초 창 단위라 말이 가득한 창 안의 같은 문구는 압축률이 높아 남는다. 기획서 §4 a3·「문구 목록」.
_HALLUCINATION_PHRASES = frozenset({"감사합니다", "다음영상에서만나요"})
_HALLUCINATION_COMPRESSION_RATIO_MAX = 0.8
_HALLUCINATION_AVG_LOGPROB_MIN = -0.75


class TranscribeError(RuntimeError):
    """전사 호출 실패(도구/키 없음·타임아웃·비정상 종료·출력 누락 등).

    R38-B-H1: `reason`은 원인 값 하나(`meta.transcript_errors[].reason`), `attempts`는
    `transcribe_media`가 쌓는 백엔드별 시도 `[{"backend", "reason"}]`다.
    """

    def __init__(self, message: str = "", *, reason: str = "other",
                 attempts: list[dict] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.attempts = list(attempts or [])


# R38-B-H1: ffmpeg가 「입력에 오디오 스트림이 없다」고 말할 때의 문구(ffmpeg 9.0.2 실측).
_NO_AUDIO_MARKERS = ("matches no streams", "does not contain any stream")


def _classify_stderr(text: str, default: str) -> str:
    """subprocess stderr로 원인 값을 고른다 — 소리 없음·애플리케이션 제어 차단만 따로 본다."""
    if any(m in text for m in _NO_AUDIO_MARKERS):
        return "no_audio_stream"
    if "WinError 4551" in text:
        return "app_blocked"
    return default


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
    """끝 `limit`자를 남긴다(R38-B-H1) — ffmpeg 배너·트레이스백은 앞이 군더더기고 이유는 끝줄에 있다."""
    return text if len(text) <= limit else "(truncated)..." + text[-limit:]


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

    whisper-transcribe CLI(`WHISPER_TRANSCRIBE_DIR`의 `transcribe.py`)를
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
            # whisper 도구는 transcript를 파일(<stem>.txt)로 쓰고 아래에서 read_text(utf-8)로
            # 읽는다. 그 도구는 이미 `encoding="utf-8"`로 파일을 명시 기록하므로(외부 리뷰
            # Codex가 도구 소스 확인) PYTHONUTF8은 no-op이라 넣지 않는다. IOENCODING만 둔다
            # — 실패 시 캡처하는 stderr(한글 오류메시지)를 깨짐 없이 읽기 위함(무해·일관).
            proc = subprocess.run(
                args,
                cwd=str(tool_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except subprocess.TimeoutExpired as e:
            _log.warning("whisper 전사 타임아웃(%ds 초과): %s", timeout, media_path.name)
            raise TranscribeError(
                f"whisper 전사 타임아웃({timeout}s 초과): {media_path.name}", reason="timeout",
            ) from e
        except OSError as e:
            _log.warning("whisper subprocess 실행 실패: %s", e)
            raise TranscribeError(
                f"whisper subprocess 실행 실패: {e}", reason=_classify_stderr(str(e), "other"),
            ) from e

        if proc.returncode != 0:
            output = proc.stderr or proc.stdout or "(출력 없음)"
            _log.warning(
                "whisper 전사 실패(returncode=%d): %s — %s",
                proc.returncode, media_path.name, _truncate(output),
            )
            raise TranscribeError(
                f"whisper 전사 실패(returncode={proc.returncode}) {media_path.name}: "
                f"{_truncate(output)}",
                reason=_classify_stderr(output, "other"),
            )

        txt_path = outdir / f"{media_path.stem}.txt"
        if not txt_path.is_file():
            raise TranscribeError(
                f"whisper 출력 파일이 없습니다: {txt_path} "
                f"(stdout: {_truncate(proc.stdout)})"
            )
        try:
            text = txt_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            raise TranscribeError(
                f"whisper 출력 파일 읽기 실패: {txt_path.name} ({type(e).__name__})"
            ) from e

    used_model = model or "large-v3"  # 도구 자체 기본값(미지정 시)과 동일 표기
    _log.info("whisper(local) 전사 완료: %s (model=%s)", media_path.name, used_model)
    return {"text": text, "model": used_model, "backend": "local"}


# ── Groq Whisper backend(round-27) ──────────────────────────────────────────


class _GroqRateLimited(TranscribeError):
    """429 — 시간창 rate limit(일시). dispatcher가 다음 모델로 사다리를 내린다.

    `retry_after`(초)는 응답 `Retry-After` 헤더 파싱값 또는 None(헤더 없음/파싱 실패).
    """

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message, reason="rate_limited")
        self.retry_after = retry_after


def _ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def probe_audio(path: str | Path) -> bool | None:
    """R38-B-H1: 미디어 파일에 오디오 스트림이 있는가 — True/False, 판별 불가면 None.

    ffprobe가 있으면 오디오 스트림 목록을 본다(rc 0에 출력이 비면 없음). ffprobe가 없으면
    ffmpeg로 첫 오디오 스트림을 골라 0초만 null 출력에 써 보고, 「스트림 없음」 문구면 없음이다.
    둘 다 없거나 다른 오류(깨진 파일 등)면 None — 호출자는 종전처럼 전사를 시도한다.
    """
    media = str(path)
    if _ffprobe_path() is not None:
        args = ["ffprobe", "-v", "error", "-select_streams", "a",
                "-show_entries", "stream=index", "-of", "csv=p=0", media]
    elif _ffmpeg_path() is not None:
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", media,
                "-map", "0:a:0", "-t", "0", "-f", "null", "-"]
    else:
        return None
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GROQ_TIMEOUT_SECONDS, shell=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.warning("오디오 스트림 판별 실패(%s): %s", args[0], e)
        return None
    if args[0] == "ffprobe":
        return bool(proc.stdout.strip()) if proc.returncode == 0 else None
    if proc.returncode == 0:
        return True
    if _classify_stderr(proc.stderr or "", "other") == "no_audio_stream":
        return False
    return None


def _extract_audio(media_path: Path, *, outdir: Path) -> Path:
    """ffmpeg로 오디오 트랙만 추출(16kHz mono mp3 ~48kbps) → temp mp3 경로.

    분할(chunking)은 비목표 — 1회 오디오 추출만. 실패 시 `TranscribeError`.
    """
    out_path = outdir / f"{media_path.stem}_audio.mp3"
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_path),
        "-vn",  # 비디오 스트림 제거
        "-ar", "16000", "-ac", "1", "-b:a", "48k",
        str(out_path),
    ]
    try:
        # ffmpeg는 비-python이라 PYTHONIOENCODING/PYTHONUTF8이 no-op이고, stdout도 로그뿐
        # (콘텐츠 미파싱, 오디오는 파일로 출력). 부모 utf-8 디코딩만 유지하고 env는 강제하지
        # 않는다(no-op 제거 — R32 외부 리뷰 반영).
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
        raise TranscribeError(
            f"ffmpeg 오디오 추출 타임아웃: {media_path.name}", reason="timeout",
        ) from e
    except OSError as e:
        raise TranscribeError(
            f"ffmpeg 실행 실패: {e}", reason=_classify_stderr(str(e), "other"),
        ) from e
    if proc.returncode != 0 or not out_path.is_file():
        stderr = proc.stderr or "(출력 없음)"
        raise TranscribeError(
            f"ffmpeg 오디오 추출 실패(returncode={proc.returncode}) {media_path.name}: "
            f"{_truncate(stderr)}",
            reason=_classify_stderr(stderr, "decode_failed"),
        )
    return out_path


def _prepare_upload_path(media_path: Path, *, tmpdir: Path) -> Path | None:
    """Groq 업로드용 최종 파일 경로를 결정한다. 업로드 불가면 None(정직 skip 신호).

    사전검사(계약 §업로드 사전검사):
    1. 미지원 확장자는 ffmpeg로 오디오 추출을 시도(영상 컨테이너면), 그 외는 skip.
    2. 영상 컨테이너이거나 25MB 초과면 ffmpeg가 있을 때만 오디오만 추출해 그것을
       업로드 후보로 삼는다.
    3. ffmpeg가 없거나 추출 후에도 25MB를 초과하면 HTTP 요청 없이 None(skip).
    4. R38-B-H1: 추출 자체가 실패하면 `None`으로 바꾸지 않고 그 `TranscribeError`를 올린다
       — 「소리 없음」(`no_audio_stream`)이 「업로드 불가(용량/포맷)」로 둔갑하던 결함.
    """
    ext = media_path.suffix.lower()
    try:
        size = media_path.stat().st_size
    except OSError as e:
        raise TranscribeError(
            f"Groq 업로드 파일 검사 실패: {media_path.name} ({type(e).__name__})"
        ) from e
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
        raise
    try:
        extracted_size = extracted.stat().st_size
    except OSError as e:
        raise TranscribeError(
            f"Groq 추출 파일 검사 실패: {extracted.name} ({type(e).__name__})"
        ) from e
    if extracted_size > _GROQ_MAX_UPLOAD_BYTES:
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


def _is_number(value) -> bool:
    """유한한 실수만 숫자다 — NaN·무한대·float로 못 바꾸는 큰 정수는 신호가 없는 것으로 본다."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_no_speech_segment(seg: dict) -> bool:
    """R27-H1: 이 구간이 말 없음 규칙(R1)에 걸리는가 — 신호가 숫자임은 호출자가 확인했다."""
    return seg["no_speech_prob"] > _NO_SPEECH_PROB_MIN and seg["avg_logprob"] < _AVG_LOGPROB_MAX


def _is_hallucination_segment(seg: dict) -> bool:
    """R27-H2: 이 구간이 환각 문구 규칙에 걸리는가 — 문구 정확 일치 ∧ 낮은 압축률 ∧ 높은 확신."""
    return (
        re.sub(r"[\W_]+", "", seg["text"]) in _HALLUCINATION_PHRASES
        and seg["compression_ratio"] < _HALLUCINATION_COMPRESSION_RATIO_MAX
        and seg["avg_logprob"] > _HALLUCINATION_AVG_LOGPROB_MIN
    )


def _filter_segments(payload: dict, text: str) -> tuple[str, dict]:
    """R27-H1: 응답 구간을 거른 글과 `quality`(`{"segments", "dropped"}`)를 돌려준다.

    R27-H2: 말 없음 규칙(R1)을 먼저 보고, 안 걸린 구간만 환각 문구 규칙을 본다. 환각 문구로 거른 구간
    항목에만 `drop_reason: "hallucination_phrase"`를 붙인다(R1로 거른 항목은 R27-H1 모양 그대로).

    `segments`가 목록이 아니거나 구간 하나라도 신호·시각이 유한한 숫자가 아니거나 글이 문자열이 아니면
    거를 근거가 없다 — 글은 그대로, `segments`는 None이다(실패로 보지 않는다). 거른 구간이
    없으면 글은 응답 `text` 그대로이고, 있으면 남은 구간 글을 각각 다듬어 빈칸 하나로 잇는다.
    """
    raw = payload.get("segments")
    if not isinstance(raw, list) or not all(
        isinstance(seg, dict)
        and all(_is_number(seg.get(k)) for k in ("start", "end", *_SEGMENT_SIGNALS))
        and isinstance(seg.get("text"), str)
        for seg in raw
    ):
        return text, {"segments": None, "dropped": 0}
    segments: list[dict] = []
    kept: list[str] = []
    for seg in raw:
        no_speech = _is_no_speech_segment(seg)
        dropped = no_speech or _is_hallucination_segment(seg)
        item = {k: float(seg[k]) for k in ("start", "end", *_SEGMENT_SIGNALS)}
        item["dropped"] = dropped
        if dropped:
            item["text"] = seg["text"]  # 오탐을 나중에 되살릴 수 있게 거른 구간 글만 남긴다
            if not no_speech:
                item["drop_reason"] = "hallucination_phrase"
        else:
            kept.append(seg["text"].strip())
        segments.append(item)
    dropped_count = len(segments) - len(kept)
    if dropped_count:
        text = " ".join(t for t in kept if t)
    return text, {"segments": segments, "dropped": dropped_count}


def _call_groq(
    upload_path: Path,
    *,
    api_key: str,
    model: str,
    lang: str,
) -> dict:
    """Groq `/audio/transcriptions` 1회 호출(단일 모델). 성공 시 `{"text","model","quality"}`.

    R27-H1: `quality`는 `_filter_segments`가 만든 `{"segments": [...] | None, "dropped": int}`이고
    `text`는 말 없음 구간을 거른 글이다(거른 구간이 없으면 응답 `text` 그대로).
    R27-H2: 환각 문구 구간(`_is_hallucination_segment`)도 거른다 — turbo·폴백 두 모델에 똑같이.

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
            f"Groq 전사 네트워크 오류: {_redact(str(e), api_key)}",
            reason="timeout" if isinstance(e, requests.exceptions.Timeout) else "network",
        ) from e
    except OSError as e:
        raise TranscribeError(
            f"Groq 업로드 파일 열기 실패: {upload_path.name} ({type(e).__name__})"
        ) from e

    if resp.status_code == 429:
        raise _GroqRateLimited(
            f"Groq rate-limit(HTTP 429, model={model})",
            retry_after=_parse_retry_after(resp),
        )
    if resp.status_code in (401, 403):
        raise TranscribeError(f"Groq 인증/권한 오류(HTTP {resp.status_code})", reason="auth")
    if resp.status_code in (400, 413):
        # 사전검사를 통과했는데도 도달한 경우(포맷/용량) — 해당 아이템만 정직 skip.
        raise TranscribeError(
            f"Groq 요청 거부(HTTP {resp.status_code}): {_truncate(resp.text)}",
            reason="upload_unavailable",
        )
    if resp.status_code >= 500:
        raise TranscribeError(f"Groq 서버 오류(HTTP {resp.status_code})", reason="http_5xx")
    if resp.status_code != 200:
        raise TranscribeError(
            f"Groq 전사 실패(HTTP {resp.status_code}): {_truncate(resp.text)}"
        )

    try:
        payload = resp.json()
        text = payload["text"]
        text = text.strip()
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise TranscribeError(
            f"Groq 응답 파싱 실패: {type(e).__name__}", reason="bad_response",
        ) from e

    text, quality = _filter_segments(payload, text)
    return {"text": text, "model": model, "quality": quality}


def _transcribe_groq(media_path: Path, *, lang: str) -> dict:
    """Groq 무료 tier 전사 → `{"text","model","quality","backend":"groq"}`(R27-H1 `quality`).

    모델 순서: whisper-large-v3-turbo → whisper-large-v3(429 시에만 폴백).
    Retry-After ≤60s면 같은 모델 1회 대기 후 재시도(SHOULD) — RPM 순간폭주로
    두 버킷을 연달아 태우는 것을 완화한다(계약 §429 처리).
    """
    api_key = _groq_api_key()
    if not api_key:
        raise TranscribeError("GROQ_API_KEY가 설정되지 않았습니다", reason="auth")

    with tempfile.TemporaryDirectory(prefix="sipher_groq_upload_") as tmpdir_str:
        upload_path = _prepare_upload_path(media_path, tmpdir=Path(tmpdir_str))
        if upload_path is None:
            raise TranscribeError(
                f"Groq 업로드 불가(용량/포맷) — 정직 skip: {media_path.name}",
                reason="upload_unavailable",
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
            f"Groq 전사 rate-limit 전 모델 소진 — 정직 skip: {media_path.name}",
            reason="rate_limited",
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

    R27-H1: Groq 결과에는 `quality`(`_filter_segments`)가 더해진다. local 결과에는 없다(구간 신호 없음).

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
    try:
        media_path = Path(path)
    except TypeError as e:
        raise TranscribeError(f"잘못된 미디어 경로 타입: {type(path).__name__}") from e
    if not media_path.exists():
        raise TranscribeError(f"미디어 파일이 없습니다: {media_path}", reason="missing_file")
    if lang is None:
        from .lang import resolve_lang  # 지연 import — 도구 부재 환경에서도 모듈 로드 가볍게
        lang = resolve_lang()

    local_ok = _local_is_available()
    groq_ok = _groq_is_available()
    # R38-B-H1: 백엔드별 시도 원인을 쌓아 마지막 예외에 싣는다(`meta.transcript_errors[].attempts`).
    attempts: list[dict] = []

    if local_ok:
        try:
            return _transcribe_local(
                media_path, model=model, device=device, compute=compute, lang=lang
            )
        except TranscribeError as e:
            attempts.append({"backend": "local", "reason": e.reason})
            if not groq_ok:
                e.attempts = attempts
                raise
            _log.warning(
                "local whisper 전사 실패 — Groq 폴백 시도: %s (%s)",
                media_path.name, e,
            )

    if groq_ok:
        try:
            return _transcribe_groq(media_path, lang=lang)
        except TranscribeError as e:
            e.attempts = attempts + [{"backend": "groq", "reason": e.reason}]
            raise

    raise TranscribeError(
        "전사 backend가 구성되지 않았습니다 "
        "(WHISPER_TRANSCRIBE_DIR 또는 GROQ_API_KEY 중 하나를 .env.local에 지정하세요)"
    )
