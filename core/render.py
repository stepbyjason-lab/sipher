r"""
sipher 정규화 dict → 사람용 Markdown 렌더러.

`core.fetch()`가 반환하는 정규화 스키마(8키: `source, platform, body_text,
comments[], ocr_text[], transcript, media_paths[], meta`)를 사람이 바로 읽을 수
있는 Markdown 문자열로 변환한다(round-08 CLI UX contract §B).

설계 원칙:
- **플랫폼별 분기 최소화**: 8키 공통 처리만 한다. `meta` 안의 개별 필드
  (author/code/likes/completeness 등)는 플랫폼마다 있을 수도 없을 수도 있으므로
  전부 `.get()`으로 안전 접근하고, 없으면 해당 파트만 조용히 생략한다.
- **순수 함수**: 입력 `result`를 mutate하지 않는다(`core/normalize.py`의
  불변성 패턴과 동일).
- **빈 섹션은 생략**: 댓글 0개, OCR 없음, 전사 없음 등은 헤더 자체를 출력하지
  않는다 — 빈 섹션 헤더만 나열되는 소음을 피한다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

__all__ = ["render_markdown"]

PathMode = Literal["absolute", "relative"]


def _fmt_path(media_path: str, *, path_mode: PathMode, relative_to: Path | None) -> str:
    """미디어 경로를 path_mode 규칙대로 문자열로 변환한다.

    round-08 contract §미디어 경로 규칙: stdout(기본)은 절대경로, `--out` 저장
    시엔 md 파일 위치 기준 상대경로(크로스 드라이브 등으로 relpath 불가하면
    절대경로 fallback). `--json` 경로는 이 함수를 아예 거치지 않으므로 현행
    그대로 불변이다(render.py 미호출).
    """
    if path_mode == "absolute":
        return os.path.abspath(media_path)
    # path_mode == "relative"
    assert relative_to is not None, "relative 모드는 relative_to가 필요합니다"
    try:
        return os.path.relpath(media_path, start=relative_to)
    except ValueError:
        # 드라이브가 달라 relpath 불가(Windows) — 절대경로로 fallback.
        return os.path.abspath(media_path)


def _render_title(result: dict) -> str:
    meta = result.get("meta") or {}
    platform = result.get("platform") or "unknown"
    author = meta.get("author")
    code = meta.get("code") or result.get("source") or ""
    if author:
        return f"# @{author} — {platform} 포스트 ({code})"
    return f"# {platform} 포스트 ({code})"


def _fold_text(text: str) -> str:
    """리스트 항목 안에 들어갈 텍스트의 개행을 공백으로 접는다(리스트 깨짐 방지)."""
    return " ".join(text.split("\n"))


def _render_comments(result: dict) -> list[str]:
    comments = result.get("comments") or []
    if not comments:
        return []
    meta = result.get("meta") or {}
    completeness = meta.get("completeness") or {}
    total = completeness.get("expected")
    if total is None:
        total = meta.get("reply_count")

    n = len(comments)
    header = f"## 댓글 ({n}개 수집 / 전체 ~{total})" if total is not None else f"## 댓글 ({n}개 수집)"

    lines = [header, ""]
    for c in comments:
        author = c.get("author") or "(익명)"
        text = _fold_text(c.get("text") or "")
        likes = c.get("likes")
        like_part = f" ♥{likes}" if likes is not None else ""
        lines.append(f"- **{author}**{like_part}: {text}")
    return lines


def _render_media(result: dict, *, path_mode: PathMode, relative_to: Path | None) -> list[str]:
    media_paths = result.get("media_paths") or []
    if not media_paths:
        return []
    lines = ["## 미디어", ""]
    for p in media_paths:
        lines.append(f"- {_fmt_path(p, path_mode=path_mode, relative_to=relative_to)}")
    return lines


def _render_ocr(result: dict) -> list[str]:
    ocr_text = result.get("ocr_text") or []
    if not ocr_text:
        return []
    lines = ["## 이미지 속 텍스트 (OCR)", ""]
    for item in ocr_text:
        media_path = item.get("media_path") or ""
        filename = Path(media_path).name if media_path else "(알 수 없는 파일)"
        text = _fold_text(item.get("text") or "")
        lines.append(f"> {filename}: {text}")
    return lines


def _render_transcript(result: dict) -> list[str]:
    transcript = result.get("transcript")
    if not transcript:
        return []
    return ["## 전사", "", transcript]


def _completeness_label(meta: dict) -> str:
    completeness = meta.get("completeness") or {}
    if completeness.get("incomplete") is True:
        captured = completeness.get("captured")
        expected = completeness.get("expected")
        label = f"부분수집({captured}/{expected})"
    else:
        label = "완전수집"
    if meta.get("media_complete") is False:
        label += " · 미디어 일부 누락"
    return label


def _render_footer(result: dict) -> str:
    meta = result.get("meta") or {}
    platform = result.get("platform") or "unknown"
    parts = [f"플랫폼: {platform}"]
    likes = meta.get("likes")
    if likes is not None:
        parts.append(f"좋아요: {likes}")
    fetched_at = meta.get("fetched_at")
    if fetched_at:
        parts.append(f"수집: {fetched_at}")
    parts.append(f"완전성: {_completeness_label(meta)}")
    return "---\n" + " · ".join(parts)


def render_markdown(
    result: dict,
    *,
    path_mode: PathMode = "absolute",
    relative_to: Path | None = None,
) -> str:
    """정규화 dict(8키 스키마) → 사람용 Markdown 문자열.

    `path_mode="relative"`일 때는 `relative_to`(보통 저장할 md 파일의 부모
    디렉토리)를 반드시 넘겨야 한다. `result`는 mutate하지 않는다.
    """
    sections: list[str] = [_render_title(result)]

    body_text = result.get("body_text")
    if body_text:
        sections.append("")
        sections.append(body_text)

    comment_lines = _render_comments(result)
    if comment_lines:
        sections.append("")
        sections.extend(comment_lines)

    media_lines = _render_media(result, path_mode=path_mode, relative_to=relative_to)
    if media_lines:
        sections.append("")
        sections.extend(media_lines)

    ocr_lines = _render_ocr(result)
    if ocr_lines:
        sections.append("")
        sections.extend(ocr_lines)

    transcript_lines = _render_transcript(result)
    if transcript_lines:
        sections.append("")
        sections.extend(transcript_lines)

    sections.append("")
    sections.append(_render_footer(result))

    return "\n".join(sections) + "\n"
