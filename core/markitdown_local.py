r"""
sipher 로컬 문서 변환 — MarkItDown 결선(round-10 §③).

`core/local.py`가 미분류(`unsupported`)로 걸러내던 로컬 파일(`.pdf`/`.docx`/
`.pptx`/`.xlsx`/`.csv`/`.ipynb` 등)을 MarkItDown(마이크로소프트,
`pip show markitdown` → 0.0.2, MIT)으로 Markdown 텍스트로 변환한다.

설계 원칙(round 공통):
- **도구 재구현 금지**: MarkItDown 내부 파서(pdfminer/mammoth/openpyxl 등)를
  직접 다루지 않는다 — `MarkItDown().convert(path)` 공개 API 1개만 호출.
- **지연 임포트**: `import markitdown`은 `convert_document()` 함수 내부에서만
  수행한다(core/local.py::_build_context의 instaloader 지연 임포트와 동일
  원칙) — MarkItDown 미설치 환경에서도 `core.local` import 자체는 깨지지
  않는다.
- **정직 라벨**: 성공/실패를 `meta.conversion`/`meta.conversion_label`로
  명시한다(round-03 OCR·round-06 whisper와 동일 "막지 않고 degrade" 원칙).
  MarkItDown 미설치 시 `skipped_no_tool`, 변환 자체가 실패하면 `failed`.
- **LLM 호출 0회**: MarkItDown은 결정적 파서 체인(포맷별 라이브러리)이다 —
  round 공통 SSOT 원칙에 부합. (MarkItDown 0.0.2는 선택적으로 Azure Document
  Intelligence를 붙일 수 있으나, 이 모듈은 그 경로를 사용하지 않는다 — 기본
  로컬 파서 체인만 호출한다.)

지원 형식(MarkItDown 0.0.2 실측 확인, `_markitdown.py`의 `_page_converters`
등록 목록 기준): `.pdf` `.docx` `.pptx` `.xls` `.xlsx` `.csv` `.ipynb` `.msg`
`.zip`(내부 파일 재귀 변환) — 그 외 이미지(`.jpg` 등)·오디오(`.mp3`/`.wav`)도
MarkItDown 자체는 변환 가능하지만, sipher는 이미지/AV를 이미 별도 파이프라인
(OCR opt-in/whisper 자동)으로 처리하므로 `core/local.py::_classify`가 이
모듈로 보내지 않는다(문서류만 라우팅됨). `.epub`은 이 버전에 컨버터가 없어
`UnsupportedFormatException`으로 실패한다(정직 실패 — round-10 §③이 "지원
안내"까지 요구하지 않으므로 별도 우회 없음).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

__all__ = ["convert_document", "is_available", "MarkItDownError", "DOCUMENT_EXTS"]

_log = logging.getLogger(__name__)

ConversionLabel = Literal["done", "failed", "skipped_no_tool"]

# MarkItDown 0.0.2 실측 지원 확장자(§docstring 근거) — core/local.py._classify가
# 이 집합을 "document" FileType으로 분류하는 데 사용한다.
DOCUMENT_EXTS = {
    ".pdf", ".docx", ".pptx", ".xls", ".xlsx", ".csv", ".ipynb", ".msg", ".zip",
}


class MarkItDownError(RuntimeError):
    """MarkItDown 변환 실패(미설치·미지원 포맷·파서 내부 오류 등)."""


def is_available() -> bool:
    """MarkItDown 패키지가 설치돼 있는지 확인(임포트 시도, 부작용 없음)."""
    try:
        import markitdown  # noqa: F401
    except ImportError:
        return False
    return True


def convert_document(path: str | Path) -> dict:
    """로컬 문서 파일 → `{"text": str, "label": ConversionLabel, "error": str|None}`.

    MarkItDown 미설치 시 예외를 던지지 않고 `label="skipped_no_tool"`로
    degrade한다(§10 정직 라벨 원칙 — 호출자가 `core/local.py::fetch_local`이며,
    이 함수 자체가 최종 사용자 대면 에러 메시지를 결정하지 않는다).

    변환 자체가 실패(MarkItDown이 예외를 던짐 — 손상된 파일, 미지원 세부 포맷
    등)하면 `label="failed"`와 함께 원인 문자열을 `error`에 담아 반환한다
    (예외를 올리지 않음 — 상위 `fetch_local`이 이 dict를 보고 `body_text`를
    비운 채 정직 라벨만 남긴다).

    파일이 존재하지 않으면 `MarkItDownError`를 던진다(존재하지 않는 로컬
    경로를 MarkItDown에 넘기지 않는다 — `core/transcribe.py::transcribe_media`
    와 동일 사전 검증 패턴).
    """
    p = Path(path)
    if not p.is_file():
        raise MarkItDownError(f"문서 파일이 없습니다: {p}")

    if not is_available():
        _log.info("MarkItDown 미설치 — skipped_no_tool: %s", p)
        return {"text": None, "label": "skipped_no_tool", "error": None}

    from markitdown import MarkItDown

    try:
        converter = MarkItDown()
        result = converter.convert(str(p))
    except Exception as e:  # MarkItDown이 다양한 예외를 던짐 — 정직 degrade
        _log.warning("MarkItDown 변환 실패(%s): %s", type(e).__name__, e)
        return {"text": None, "label": "failed", "error": f"{type(e).__name__}: {e}"}

    text = result.text_content or ""
    return {"text": text, "label": "done", "error": None}
