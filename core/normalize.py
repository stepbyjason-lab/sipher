r"""
sipher 정규화 코어 — 어댑터 산출물 OCR·전사 인리치먼트.

어댑터는 `ocr_text: []`/`transcript: None`을 빈 채로 반환한다(각 어댑터
docstring: "sipher 정규화 단계(어댑터 밖)에서 채움"). 이 모듈이 그 채무를
갚는다:
- `media_paths[]` 중 이미지 파일만 골라 `core.llm_free.ocr_image`(Gemini)로
  텍스트를 추출해 `ocr_text[]`에 채운다(`enrich_ocr`).
- `media_paths[]` 중 오디오/영상 파일만 골라 `core.transcribe.transcribe_media`
  (로컬 whisper → 무료 Groq Whisper 사다리, round-27)로 텍스트를 추출해
  `transcript`에 채운다(`enrich_transcribe`).

(`docs/01-overview.md` §6 deterministic-first 정규화 로직, §8 무료 API 배치,
§12.5 정직 라벨)

경계:
- 타이핑된 본문/댓글(`body_text`, `comments[]`)은 재처리하지 않는다 — 어댑터가
  이미 코드로 채운 값이다. 이 모듈은 "이미지에 그려진 글"·"음성"만 다룬다.
- provider/도구 없으면 막지 않고 `skipped_no_provider`/`skipped_no_tool`로
  degrade한다(§10).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from . import llm_free
from . import ocr_ensemble as _ocr_ensemble
from . import transcribe as _transcribe

__all__ = ["enrich_ocr", "enrich_transcribe"]

_log = logging.getLogger(__name__)

OcrLabel = Literal["none", "done", "partial", "skipped_no_provider", "failed"]
TranscribeLabel = Literal["none", "done", "partial", "failed", "skipped_no_tool"]

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_AV_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".ogg",
}


def _is_image(path: str) -> bool:
    return Path(path).suffix.lower() in _IMAGE_EXTS


def _is_av(path: str) -> bool:
    return Path(path).suffix.lower() in _AV_EXTS


def _ocr_label(*, provider_available: bool, total_images: int, done: int) -> OcrLabel:
    if not provider_available:
        return "skipped_no_provider" if total_images > 0 else "none"
    if total_images == 0:
        return "none"
    if done == total_images:
        return "done"
    if done == 0:
        return "failed"
    return "partial"


def enrich_ocr(result: dict) -> dict:
    """정규화 dict(어댑터 fetch 결과)의 `media_paths[]` 이미지를 OCR로 인리치한다.

    새 dict를 반환한다(원본 `result`는 mutate하지 않는다). 반환 dict는 입력과
    동일한 키 구조를 유지하되 `ocr_text`(list)와 `meta.ocr_label`/`meta.ocr_provider`만
    갱신된다.

    `ocr_text` 스키마: `[{"media_path": str, "text": str, "model": str}, ...]`
    (provenance 보존 — 어느 이미지에서 어느 모델로 나온 텍스트인지 추적 가능, §12.5).

    provider(Gemini 키)가 없으면 API를 호출하지 않고 `meta.ocr_label =
    "skipped_no_provider"`로 정직하게 표기한다(막지 않고 degrade).
    """
    media_paths = list(result.get("media_paths") or [])
    image_paths = [p for p in media_paths if _is_image(p)]

    meta = dict(result.get("meta") or {})

    if not image_paths:
        meta["ocr_label"] = "none"
        meta["ocr_provider"] = None
        return {**result, "ocr_text": [], "meta": meta}

    provider_available = _ocr_ensemble.is_available()  # 무료 provider 1개라도(round-24)
    if not provider_available:
        _log.info("OCR provider 없음 — skipped_no_provider (%d개 이미지)", len(image_paths))
        meta["ocr_label"] = "skipped_no_provider"
        meta["ocr_provider"] = None
        return {**result, "ocr_text": [], "meta": meta}

    ocr_text: list[dict] = []
    provider_name: str | None = None
    done_count = 0

    for media_path in image_paths:
        path_obj = Path(media_path)
        if not path_obj.exists():
            _log.warning("OCR 대상 파일이 없음 — skip: %s", media_path)
            continue
        try:
            # round-24: 기본=무료 앙상블 사다리(ensemble→solo→유료 옵트인). 반환 shape 동일.
            ocr_result = _ocr_ensemble.ocr_image_ensemble(path_obj)
        except llm_free.OcrError as e:
            _log.warning("OCR 실패: %s (%s)", media_path, e)
            continue
        ocr_text.append({
            "media_path": media_path,
            "text": ocr_result["text"],
            "model": ocr_result["model"],
        })
        provider_name = ocr_result["model"]
        done_count += 1

    meta["ocr_label"] = _ocr_label(
        provider_available=provider_available,
        total_images=len(image_paths),
        done=done_count,
    )
    meta["ocr_provider"] = provider_name

    return {**result, "ocr_text": ocr_text, "meta": meta}


def _transcribe_label(*, tool_available: bool, total_sources: int, done: int) -> TranscribeLabel:
    if not tool_available:
        return "skipped_no_tool" if total_sources > 0 else "none"
    if total_sources == 0:
        return "none"
    if done == total_sources:
        return "done"
    if done == 0:
        return "failed"
    return "partial"


def enrich_transcribe(
    result: dict,
    *,
    model: str | None = None,
    device: str | None = None,
    compute: str | None = None,
) -> dict:
    """정규화 dict(어댑터 fetch 결과)의 `media_paths[]` 오디오/영상을 전사해
    `transcript`를 채운다. backend 사다리(local whisper → 무료 Groq Whisper,
    round-27)는 `core.transcribe.transcribe_media`가 내부에서 결정한다.

    `model`/`device`를 지정하지 않으면 local whisper 사용 시 whisper-transcribe
    도구 자체 기본값(large-v3/cuda)에 위임한다(Groq 폴백 시에는 이 인자들이
    무시된다 — Groq는 자체 모델 사다리 turbo→v3를 쓴다).

    새 dict를 반환한다(원본 `result`는 mutate하지 않는다, `enrich_ocr`와 동형
    불변성 패턴). 반환 dict는 입력과 동일한 키 구조를 유지하되 `transcript`
    (str)와 `meta.transcript_label`/`meta.transcript_model`/
    `meta.transcript_backend`/`meta.transcript_sources`만 갱신된다.

    **기존 transcript 보존**: `result["transcript"]`가 이미 채워져 있으면(예:
    youtube 어댑터가 `with_transcript=True`로 `youtube-transcript-api`를 통해
    이미 자체 전사를 채운 경우) 전사를 호출하지 않고 원본을 그대로 통과시킨다
    — 중복 API 호출 방지 + 어댑터 우선권 존중. 이 경우 `meta.transcript_label`도
    건드리지 않는다(youtube 자체 라벨 `fetched`/`unavailable`/`fetch_failed`를
    보존).

    `transcript`가 `None`일 때만 전사 인리치먼트를 시도한다. 대상 미디어가
    여러 개면 각각 전사한 텍스트를 `"\\n\\n"`로 join해 단일 문자열로 채운다
    (overview 스키마: `transcript`는 단수 필드).

    전사 backend(local/Groq 둘 다)가 없으면 API를 호출하지 않고
    `meta.transcript_label = "skipped_no_tool"`로 정직하게 표기한다(막지 않고
    degrade, §10).
    """
    if result.get("transcript"):
        # 어댑터가 이미 자체 전사를 채운 경우(예: youtube with_transcript=True) —
        # whisper로 덮어쓰지 않고 원본 그대로 통과.
        return dict(result)

    media_paths = list(result.get("media_paths") or [])
    av_paths = [p for p in media_paths if _is_av(p)]

    meta = dict(result.get("meta") or {})

    if not av_paths:
        meta["transcript_label"] = "none"
        meta["transcript_model"] = None
        meta["transcript_backend"] = None
        meta["transcript_sources"] = []
        return {**result, "transcript": None, "meta": meta}

    tool_available = _transcribe.is_available()
    if not tool_available:
        _log.info(
            "전사 backend 없음 — skipped_no_tool (%d개 미디어)", len(av_paths)
        )
        meta["transcript_label"] = "skipped_no_tool"
        meta["transcript_model"] = None
        meta["transcript_backend"] = None
        meta["transcript_sources"] = []
        return {**result, "transcript": None, "meta": meta}

    texts: list[str] = []
    sources: list[str] = []
    model_name: str | None = None
    backend_name: str | None = None
    done_count = 0

    for media_path in av_paths:
        path_obj = Path(media_path)
        if not path_obj.exists():
            _log.warning("전사 대상 파일이 없음 — skip: %s", media_path)
            continue
        try:
            transcribe_result = _transcribe.transcribe_media(
                path_obj, model=model, device=device, compute=compute
            )
        except _transcribe.TranscribeError as e:
            _log.warning("전사 실패: %s (%s)", media_path, e)
            continue
        texts.append(transcribe_result["text"])
        sources.append(media_path)
        model_name = transcribe_result["model"]
        # round-27: local/groq 사다리 도입 — 어느 backend가 실제로 인리치했는지
        # provenance로 남긴다(`ocr_provider`와 동형, 라벨 값집합 자체는 무변경).
        backend_name = transcribe_result.get("backend")
        done_count += 1

    meta["transcript_label"] = _transcribe_label(
        tool_available=tool_available,
        total_sources=len(av_paths),
        done=done_count,
    )
    meta["transcript_model"] = model_name
    meta["transcript_backend"] = backend_name
    meta["transcript_sources"] = sources

    transcript_text = "\n\n".join(texts) if texts else None

    return {**result, "transcript": transcript_text, "meta": meta}
