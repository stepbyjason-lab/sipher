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

OcrLabel = Literal["none", "not_downloaded", "done", "partial", "skipped_no_provider", "failed"]
TranscribeLabel = Literal["none", "not_downloaded", "done", "partial", "failed", "skipped_no_tool"]

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_AV_EXTS = {
    ".mp4", ".mov", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".ogg",
}


def _is_image(path: str) -> bool:
    return Path(path).suffix.lower() in _IMAGE_EXTS


def _is_av(path: str) -> bool:
    return Path(path).suffix.lower() in _AV_EXTS


def _ocr_label(
    *, provider_available: bool, expected_images: int, local_images: int, done: int,
    partial: int = 0, failed: int = 0,
) -> OcrLabel:
    """round-34 §B: `expected_images`(사전신호 meta.image_count)와 `local_images`
    (실제 다운로드된 이미지 수)를 먼저 대조한다 — 완전성이 provider 가용성보다
    우선한다. "안 받아서 없음"(not_downloaded)·"일부만 받음"(partial)은 provider가
    없어도 판정 가능한 사실이라, provider 부재(skipped_no_provider)로 가려지면
    안 된다(R29 게이트 P0 #3: image_count=8인데 0장 받고도 "원래 없음"으로 보이던 결함).

    round-36 F4(계약 §normalize 전파 P0-2): `partial`(OCR 절단으로 `done_count`에
    안 세인 이미지 수)이 있으면 `done == 0`이어도 "failed"가 아니라 "partial"을
    반환한다 — 텍스트 자체는 보존됐으므로(잘렸을 뿐 완전 실패가 아님) "실패"로
    오분류하면 안 된다. `partial` 값이 이미 "다운로드 일부만 됨" 의미로 점유돼
    있어(`local_images < expected_images`) 새 라벨값을 만들지 않고 재사용한다.
    """
    if expected_images == 0:
        return "none"
    if local_images == 0:
        return "not_downloaded"
    if local_images < expected_images:
        return "partial"
    if not provider_available:
        return "skipped_no_provider"
    # round-38A: 선언 image_count가 실제 로컬 수보다 작아도 OCR 실패가 있으면 done 금지.
    if done == expected_images and failed == 0:
        return "done"
    if done == 0 and partial == 0:
        return "failed"
    return "partial"


def enrich_ocr(result: dict) -> dict:
    """정규화 dict(어댑터 fetch 결과)의 `media_paths[]` 이미지를 OCR로 인리치한다.

    새 dict를 반환한다(원본 `result`는 mutate하지 않는다). 반환 dict는 입력과
    동일한 키 구조를 유지하되 `ocr_text`(list)와 `meta.ocr_label`/`meta.ocr_provider`만
    갱신된다.

    `ocr_text` 스키마: `[{"media_path": str, "text": str, "model": str}, ...]`
    (provenance 보존 — 어느 이미지에서 어느 모델로 나온 텍스트인지 추적 가능, §12.5).
    round-36 F4: OCR 응답이 provider에서 절단(truncated)됐던 항목은 `"partial":
    True`가 추가된다 — 텍스트는 버리지 않고 그대로 보존하되(부분 텍스트도 유용),
    그런 항목은 `done_count`에 세지 않아 `meta.ocr_label`이 정직하게 `"partial"`로
    나온다(조용한 `"done"` 금지).

    provider(Gemini 키)가 없으면 API를 호출하지 않고 `meta.ocr_label =
    "skipped_no_provider"`로 정직하게 표기한다(막지 않고 degrade).
    """
    media_paths = list(result.get("media_paths") or [])
    image_paths = [p for p in media_paths if _is_image(p)]
    # round-34 재게이트 P1: "받은 이미지 수"는 확장자만 맞는 경로 문자열이 아니라 실제
    # 로컬에 존재하는 파일 수여야 한다 — 이전엔 존재하지 않는 경로도 "받음"으로 세어
    # not_downloaded여야 할 상황이 failed로 오분류됐다(게이트 실측). 완전성 판정과 OCR
    # 루프가 같은 목록을 쓴다.
    existing_image_paths = [p for p in image_paths if Path(p).exists()]

    meta = dict(result.get("meta") or {})
    # round-34 §B: 어댑터가 다운로드 전 사전신호로 실은 image_count가 있으면 그것을
    # "기대치"로 쓴다 — 없으면 실제 로컬 이미지 수를 기대치로 간주(사전신호 미지원
    # 어댑터의 기존 동작 보존).
    declared = meta.get("image_count")
    expected_images = (
        declared if isinstance(declared, int) and declared >= 0 else len(existing_image_paths)
    )

    if expected_images == 0 or not existing_image_paths:
        meta["ocr_label"] = _ocr_label(
            provider_available=True, expected_images=expected_images,
            local_images=len(existing_image_paths), done=0,
        )
        meta["ocr_provider"] = None
        return {**result, "ocr_text": [], "meta": meta}

    provider_available = _ocr_ensemble.is_available()  # 무료 provider 1개라도(round-24)
    if not provider_available and len(existing_image_paths) >= expected_images:
        _log.info("OCR provider 없음 — skipped_no_provider (%d개 이미지)", len(existing_image_paths))
        meta["ocr_label"] = "skipped_no_provider"
        meta["ocr_provider"] = None
        return {**result, "ocr_text": [], "meta": meta}

    ocr_text: list[dict] = []
    provider_name: str | None = None
    done_count = 0
    partial_count = 0  # round-36 F4: 절단(truncated)돼 done_count에 안 세인 이미지 수
    failed_count = 0   # round-38A: 기존 OcrError·미지 예외 모두 거짓 done 방지에 반영

    for media_path in existing_image_paths:
        path_obj = Path(media_path)
        try:
            # round-24: 기본=무료 앙상블 사다리(ensemble→solo→유료 옵트인). 반환 shape 동일.
            ocr_result = _ocr_ensemble.ocr_image_ensemble(path_obj)
        except llm_free.OcrError as e:
            _log.warning("OCR 실패: %s (%s)", media_path, e)
            failed_count += 1
            continue
        except Exception:
            # round-38A: 미래의 비정규화 예외도 이미지 단위로 fail-open하되 traceback을 보존.
            _log.exception("OCR 예기치 않은 예외(이미지 skip): %s", media_path)
            failed_count += 1
            continue
        is_partial = bool(ocr_result.get("partial"))
        item = {
            "media_path": media_path,
            "text": ocr_result["text"],
            "model": ocr_result["model"],
        }
        if is_partial:
            item["partial"] = True  # 항목 provenance: OCR 절단 원인(다운로드 부분과 구분)
        ocr_text.append(item)
        provider_name = ocr_result["model"]
        if is_partial:
            partial_count += 1
        else:
            done_count += 1

    meta["ocr_label"] = _ocr_label(
        provider_available=provider_available,
        expected_images=expected_images,
        local_images=len(existing_image_paths),
        done=done_count,
        partial=partial_count,
        failed=failed_count,
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
        # round-34 §B: 어댑터가 이미 정직한 실패 라벨(fetch_failed/unavailable 등)을
        # 남겼으면 smart 폴백이 그것을 "none"으로 덮지 않는다(R29 게이트 P0 #4 —
        # 다운로드까지 실패했는데 최종 라벨이 "원래 없음"으로 보이던 결함).
        existing_label = meta.get("transcript_label")
        if existing_label and existing_label != "none":
            return {**result, "transcript": None, "meta": meta}
        # 사전신호(has_video/video_count/video_label)가 영상이 있었다고 말하는데
        # 로컬에 AV 파일이 없으면 "안 받아서 없음"이지 "원래 없음"이 아니다.
        expected_video = (
            meta.get("has_video") is True
            or (isinstance(meta.get("video_count"), int) and meta.get("video_count") > 0)
            or meta.get("video_label") in {"download_failed", "partially_downloaded"}
        )
        meta["transcript_label"] = "not_downloaded" if expected_video else "none"
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
            texts.append(transcribe_result["text"])
            sources.append(media_path)
            model_name = transcribe_result["model"]
            # round-27: local/groq 사다리 도입 — 어느 backend가 실제로 인리치했는지
            # provenance로 남긴다(`ocr_provider`와 동형, 라벨 값집합 자체는 무변경).
            backend_name = transcribe_result.get("backend")
            done_count += 1
        except _transcribe.TranscribeError as e:
            _log.warning("전사 실패: %s (%s)", media_path, e)
            continue
        except Exception:
            _log.exception("전사 중 알 수 없는 오류 — 해당 미디어 skip: %s", media_path)
            continue

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
