r"""
sipher 코어 — 로컬 파일 직접 입력 흐름.

`core.fetch()`가 URL 정규식(`PLATFORM_HOSTS`)에 매칭되지 않고 로컬 경로가
존재하면 위임하는 분기(round-08 CLI UX contract §C). 어댑터와 동일하게
sipher 정규화 스키마 8키를 채워 반환하되, `platform="local"`로 표시한다.

신뢰 경계: 로컬 경로는 **로컬 사용자 신뢰 입력**으로 취급한다 — 어댑터
`media_dir` 인자(임의 로컬 경로에 다운로드), `core/transcribe.py`의
`WHISPER_TRANSCRIBE_DIR`(임의 로컬 실행 경로)와 동일 원칙. containment(경로
탈출 방지 sandbox)는 하지 않는다. 단일 사용자 로컬 CLI 도구 전제이며,
멀티테넌트/서버 배포 시에는 이 신뢰 경계를 재검토해야 한다.

지원 형식:
- 영상/음성(`core.normalize._AV_EXTS`) → `media_paths=[path]`, whisper 전사
  **자동 적용**(호출자인 `core.fetch()`가 로컬 흐름일 때 `transcribe`를
  강제 True로 승격 — 이 모듈 자체는 전사를 실행하지 않고 media_paths만 채운다,
  실제 전사는 `core.normalize.enrich_transcribe`가 기존 인리치 단계에서 수행).
- 이미지(`core.normalize._IMAGE_EXTS`) → `media_paths=[path]`. OCR은 opt-in
  유지(`--ocr`) — 이 모듈은 자동으로 OCR을 걸지 않는다.
- 텍스트(`.txt`/`.md`) → `body_text=파일 내용`(UTF-8, `errors="replace"`).
- 문서(`core.markitdown_local.DOCUMENT_EXTS` — `.pdf`/`.docx`/`.pptx`/`.xls`/
  `.xlsx`/`.csv`/`.ipynb`/`.msg`/`.zip`) → MarkItDown(round-10 §③)으로 변환해
  `body_text`에 채운다. `meta.conversion="markitdown"` +
  `meta.conversion_label`("done"/"failed"/"skipped_no_tool")로 정직 표기 —
  MarkItDown 미설치 시 막지 않고 `body_text=None`+`skipped_no_tool` degrade,
  변환 자체 실패 시 `body_text=None`+`failed`+`meta.conversion_error`.
- 그 외(위 어디에도 안 걸리는 확장자, `.epub` 포함 — MarkItDown 0.0.2엔
  epub 컨버터가 없음) → `ValueError`(정직 거부, MarkItDown도 처리 못하는
  포맷이라 이 단계 이전에 걸러짐).
- 디렉토리 → `ValueError`(이번 스코프 밖, 단일 파일만).
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Literal

from . import markitdown_local
from .normalize import _AV_EXTS, _IMAGE_EXTS

__all__ = ["fetch_local"]

FileType = Literal["video", "audio", "image", "text", "document", "unsupported"]

_AUDIO_ONLY_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg"}
_TEXT_EXTS = {".txt", ".md"}


def _classify(path: Path) -> FileType:
    ext = path.suffix.lower()
    if ext in _AV_EXTS:
        return "audio" if ext in _AUDIO_ONLY_EXTS else "video"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _TEXT_EXTS:
        return "text"
    if ext in markitdown_local.DOCUMENT_EXTS:
        return "document"
    return "unsupported"


def fetch_local(path: str, **_kwargs) -> dict:
    """로컬 파일 경로 → sipher 정규화 JSON dict(8키, `platform="local"`).

    `**_kwargs`는 어댑터 전용 옵션(media_dir/deep/auto 등)이 라우터를 통해
    실수로 흘러들어와도 TypeError 없이 무시한다 — 로컬 흐름에는 해당 개념이
    없다(§D 계약: 로컬 흐름에서 kwargs는 의미가 없으므로 명시적으로 무시).

    OCR/전사 인리치먼트는 이 함수의 책임이 아니다 — `core.fetch()`가 반환된
    dict를 `core.normalize.enrich_ocr`/`enrich_transcribe`에 통과시킨다(기존
    어댑터 흐름과 동일한 2단계 파이프라인: fetch → normalize enrich).
    """
    p = Path(path)
    if p.is_dir():
        raise ValueError("디렉토리 입력은 지원하지 않습니다(단일 파일만 — round-08 스코프 밖)")
    if not p.is_file():
        raise ValueError(f"로컬 파일을 찾을 수 없습니다: {path}")

    resolved = p.resolve()
    file_type = _classify(resolved)
    if file_type == "unsupported":
        raise ValueError(
            f"미지원 형식입니다(MarkItDown도 지원하지 않음): "
            f"{resolved.suffix or '(확장자 없음)'}"
        )

    body_text: str | None = None
    media_paths: list[str] = []
    meta: dict = {}
    if file_type == "text":
        body_text = resolved.read_text(encoding="utf-8", errors="replace")
    elif file_type == "document":
        conv = markitdown_local.convert_document(resolved)
        body_text = conv["text"]
        meta["conversion"] = "markitdown"
        meta["conversion_label"] = conv["label"]
        meta["conversion_error"] = conv["error"]
    else:
        media_paths = [str(resolved)]

    meta.update({
        "file_type": file_type,
        "size_bytes": resolved.stat().st_size,
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    })

    return {
        "source": str(resolved),
        "platform": "local",
        "body_text": body_text,
        "comments": [],
        "ocr_text": [],
        "transcript": None,
        "media_paths": media_paths,
        "meta": meta,
    }
