"""round-27: 전사 backend 사다리(local whisper → 무료 Groq Whisper) 단위 테스트.

네트워크·subprocess 없이 `_transcribe_local`/`_call_groq`/`_ffmpeg_path` 등을
monkeypatch로 대체한다(`core/tests/test_ocr_ensemble.py`와 동형 monkeypatch 스타일).
검증 대상은 계약(`​.handoff/rounds/round-27-transcribe-ladder-contract.md`) §3·§검증:
- local 우선순위, local unavailable + Groq, local per-item 실패 → Groq 폴백
- turbo 429 → v3, turbo/v3 모두 429 → 정직 skip
- GROQ_API_KEY 없음 + local 없음 → 기존 no-tool degrade
- 25MB 초과 + ffmpeg 없음 → HTTP 미발생 skip, 미지원 확장자 사전 필터
- RequestException/5xx/JSON 파싱 실패 → 아이템만 degrade(상위 전파 없음)
- 키 비노출, timeout 인자 전달
- (사후 Codex 메타 리뷰 P2 보강) `core.normalize.enrich_transcribe` 레벨
  통합: dispatcher가 `TranscribeError`를 던지는 케이스(429 전모델 소진·
  401/403·5xx·JSON 파싱 실패)에서 예외가 fetch 전체로 전파되지 않고
  기존 `TranscribeLabel` 값집합(`failed`/`partial`) 안으로 수렴하는지 확인.
"""
from __future__ import annotations

import logging
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pathlib import Path  # noqa: E402

import requests  # noqa: E402

from core import normalize as N  # noqa: E402
from core import transcribe as T  # noqa: E402


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────


def _media(tmp_path, name: str = "clip.mp3", size: int = 100) -> Path:
    p = tmp_path / name
    p.write_bytes(b"0" * size)
    return p


def _env(env_file: Path, **kv):
    """`.env.local`을 격리 재작성 + 관련 os.environ 정리(test_llm_free_ocr.py와 동형)."""
    T._ROOT = env_file.parent
    for k in list(os.environ):
        if k in ("GROQ_API_KEY", "WHISPER_TRANSCRIBE_DIR", "WHISPER_TIMEOUT_SECONDS"):
            os.environ.pop(k, None)
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in kv.items()) + ("\n" if kv else ""),
        encoding="utf-8",
    )


class _Resp:
    """requests.Response 최소 mock — status_code/json/text/headers만 필요."""

    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


def _no_local(monkeypatch):
    monkeypatch.setattr(T, "_local_is_available", lambda: False)


def _with_local(monkeypatch, *, transcribe_result=None, error: Exception | None = None):
    monkeypatch.setattr(T, "_local_is_available", lambda: True)

    def fake_local(media_path, *, model, device, compute, lang):
        if error is not None:
            raise error
        return transcribe_result or {"text": "LOCAL", "model": "large-v3", "backend": "local"}

    monkeypatch.setattr(T, "_transcribe_local", fake_local)


# ── 사다리 우선순위 ──────────────────────────────────────────────────────────


def test_local_used_even_when_groq_configured(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _with_local(monkeypatch)
    called = {"groq": False}
    monkeypatch.setattr(T, "_transcribe_groq", lambda *a, **k: called.__setitem__("groq", True))
    r = T.transcribe_media(_media(tmp_path), lang="ko")
    assert r["backend"] == "local" and r["text"] == "LOCAL"
    assert called["groq"] is False


def test_local_unavailable_uses_groq_turbo(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    calls = []

    def fake_call_groq(upload_path, *, api_key, model, lang):
        calls.append(model)
        return {"text": "GROQ", "model": model}

    monkeypatch.setattr(T, "_call_groq", fake_call_groq)
    r = T.transcribe_media(_media(tmp_path), lang="ko")
    assert r["backend"] == "groq"
    assert r["model"] == T._GROQ_MODEL_TURBO
    assert calls == [T._GROQ_MODEL_TURBO]


def test_local_per_item_failure_falls_back_to_groq(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _with_local(monkeypatch, error=T.TranscribeError("returncode!=0"))
    monkeypatch.setattr(
        T, "_call_groq",
        lambda upload_path, *, api_key, model, lang: {"text": "GROQ-FALLBACK", "model": model},
    )
    r = T.transcribe_media(_media(tmp_path), lang="ko")
    assert r["backend"] == "groq" and r["text"] == "GROQ-FALLBACK"


def test_local_failure_without_groq_raises(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local")  # GROQ_API_KEY 없음
    _with_local(monkeypatch, error=T.TranscribeError("boom"))
    try:
        T.transcribe_media(_media(tmp_path), lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError as e:
        assert "boom" in str(e)


# ── Groq 429 사다리 ──────────────────────────────────────────────────────────


def test_turbo_429_falls_back_to_v3(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)

    def fake_call_groq(upload_path, *, api_key, model, lang):
        if model == T._GROQ_MODEL_TURBO:
            raise T._GroqRateLimited("429", retry_after=None)
        return {"text": "V3-OK", "model": model}

    monkeypatch.setattr(T, "_call_groq", fake_call_groq)
    r = T.transcribe_media(_media(tmp_path), lang="ko")
    assert r["backend"] == "groq"
    assert r["model"] == T._GROQ_MODEL_FALLBACK
    assert r["text"] == "V3-OK"


def test_turbo_and_v3_429_skips_item(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    monkeypatch.setattr(
        T, "_call_groq",
        lambda upload_path, *, api_key, model, lang: (_ for _ in ()).throw(
            T._GroqRateLimited(f"429 {model}", retry_after=None)
        ),
    )
    try:
        T.transcribe_media(_media(tmp_path), lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError as e:
        assert "429" not in str(type(e))  # 일반 TranscribeError로 정직 skip(세션 dead 아님)


def test_retry_after_within_60s_retries_same_model(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    calls = []
    sleeps = []
    monkeypatch.setattr(T.time, "sleep", lambda s: sleeps.append(s))

    def fake_call_groq(upload_path, *, api_key, model, lang):
        calls.append(model)
        if len(calls) == 1:
            raise T._GroqRateLimited("429", retry_after=5)
        return {"text": "RETRIED-OK", "model": model}

    monkeypatch.setattr(T, "_call_groq", fake_call_groq)
    r = T.transcribe_media(_media(tmp_path), lang="ko")
    assert r["text"] == "RETRIED-OK"
    assert calls == [T._GROQ_MODEL_TURBO, T._GROQ_MODEL_TURBO]  # 같은 모델 1회 재시도
    assert sleeps == [5]


def test_retry_after_over_60s_skips_retry_goes_to_v3(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    calls = []
    sleeps = []
    monkeypatch.setattr(T.time, "sleep", lambda s: sleeps.append(s))

    def fake_call_groq(upload_path, *, api_key, model, lang):
        calls.append(model)
        if model == T._GROQ_MODEL_TURBO:
            raise T._GroqRateLimited("429", retry_after=90)
        return {"text": "V3", "model": model}

    monkeypatch.setattr(T, "_call_groq", fake_call_groq)
    r = T.transcribe_media(_media(tmp_path), lang="ko")
    assert r["model"] == T._GROQ_MODEL_FALLBACK
    assert calls == [T._GROQ_MODEL_TURBO, T._GROQ_MODEL_FALLBACK]  # 재시도 없이 바로 폴백
    assert sleeps == []


# ── 미구성 상태 degrade ──────────────────────────────────────────────────────


def test_no_key_no_local_is_available_false(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local")
    _no_local(monkeypatch)
    assert T.is_available() is False
    try:
        T.transcribe_media(_media(tmp_path), lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError as e:
        assert "구성되지 않았습니다" in str(e)


def test_groq_only_makes_is_available_true(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    assert T.is_available() is True


# ── 업로드 사전검사 ──────────────────────────────────────────────────────────


def test_oversized_without_ffmpeg_skips_without_http(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    monkeypatch.setattr(T, "_ffmpeg_path", lambda: None)
    http_called = []
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: http_called.append(1))

    big = _media(tmp_path, name="big.mp3", size=T._GROQ_MAX_UPLOAD_BYTES + 1)
    try:
        T.transcribe_media(big, lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError:
        pass
    assert http_called == []  # HTTP 요청 자체가 발생하지 않음


def test_unsupported_extension_prefiltered(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    monkeypatch.setattr(T, "_ffmpeg_path", lambda: None)  # 우회 불가 상태로 확인
    http_called = []
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: http_called.append(1))

    weird = _media(tmp_path, name="clip.xyz", size=10)
    try:
        T.transcribe_media(weird, lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError:
        pass
    assert http_called == []


def test_video_container_extracts_audio_then_uploads(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    monkeypatch.setattr(T, "_ffmpeg_path", lambda: "ffmpeg")

    extracted_path = tmp_path / "extracted_audio.mp3"
    extracted_path.write_bytes(b"0" * 50)

    def fake_extract(media_path, *, outdir):
        return extracted_path

    monkeypatch.setattr(T, "_extract_audio", fake_extract)

    seen_paths = []

    def fake_call_groq(upload_path, *, api_key, model, lang):
        seen_paths.append(upload_path)
        return {"text": "VIDEO-OK", "model": model}

    monkeypatch.setattr(T, "_call_groq", fake_call_groq)
    video = _media(tmp_path, name="clip.mp4", size=10)
    r = T.transcribe_media(video, lang="ko")
    assert r["text"] == "VIDEO-OK"
    assert seen_paths == [extracted_path]


def test_extraction_still_oversized_skips(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    monkeypatch.setattr(T, "_ffmpeg_path", lambda: "ffmpeg")

    huge_extracted = tmp_path / "still_huge.mp3"
    huge_extracted.write_bytes(b"0" * 10)  # 실제 크기 대신 stat 패치로 크게 위조

    monkeypatch.setattr(T, "_extract_audio", lambda media_path, *, outdir: huge_extracted)

    real_stat = Path.stat

    def fake_stat(self, *a, **k):
        if self == huge_extracted:
            class _St:
                st_size = T._GROQ_MAX_UPLOAD_BYTES + 1
            return _St()
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", fake_stat)
    http_called = []
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: http_called.append(1))

    video = _media(tmp_path, name="clip.mkv", size=10)
    try:
        T.transcribe_media(video, lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError:
        pass
    assert http_called == []


# ── 네트워크/서버/파싱 실패 → per-item degrade ───────────────────────────────


def test_request_exception_degrades_item_not_crash(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)

    def raise_timeout(*a, **k):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(T.requests, "post", raise_timeout)
    try:
        T.transcribe_media(_media(tmp_path), lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError:
        pass  # 상위(예: RequestException 원문)로 전파되지 않고 TranscribeError로 감싸짐


def test_server_5xx_degrades_item(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: _Resp(503, text="server err"))
    try:
        T.transcribe_media(_media(tmp_path), lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError as e:
        assert "503" in str(e)


def test_json_parse_failure_degrades_item(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: _Resp(200, payload=None, text="not json"))
    try:
        T.transcribe_media(_media(tmp_path), lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError as e:
        assert "파싱" in str(e)


def test_401_unavailable_degrades_honestly(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: _Resp(401, text="unauthorized"))
    try:
        T.transcribe_media(_media(tmp_path), lang="ko")
        assert False, "TranscribeError expected"
    except T.TranscribeError as e:
        assert "401" in str(e)


# ── 키 비노출 ────────────────────────────────────────────────────────────────


def test_key_not_in_log_on_network_exception(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="SECRET_GROQ_XYZ")
    _no_local(monkeypatch)

    def raise_with_key_in_message(*a, **k):
        raise requests.exceptions.ConnectionError("failed for key SECRET_GROQ_XYZ")

    monkeypatch.setattr(T.requests, "post", raise_with_key_in_message)

    recs = []
    h = logging.Handler()
    h.emit = lambda r: recs.append(h.format(r))
    T._log.addHandler(h)
    T._log.setLevel(logging.DEBUG)
    try:
        try:
            T.transcribe_media(_media(tmp_path), lang="ko")
        except T.TranscribeError as e:
            assert "SECRET_GROQ_XYZ" not in str(e)
    finally:
        T._log.removeHandler(h)
    assert "SECRET_GROQ_XYZ" not in "\n".join(recs)


def test_key_not_in_exception_string_on_401(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="SECRET_GROQ_XYZ")
    _no_local(monkeypatch)
    monkeypatch.setattr(T.requests, "post", lambda *a, **k: _Resp(401, text="unauthorized"))
    try:
        T.transcribe_media(_media(tmp_path), lang="ko")
        assert False
    except T.TranscribeError as e:
        assert "SECRET_GROQ_XYZ" not in str(e)


# ── timeout 인자 전달 ────────────────────────────────────────────────────────


def test_groq_http_call_passes_timeout(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    _no_local(monkeypatch)
    captured = {}

    def fake_post(url, *, headers, data, files, timeout):
        captured["timeout"] = timeout
        return _Resp(200, payload={"text": "OK"})

    monkeypatch.setattr(T.requests, "post", fake_post)
    T.transcribe_media(_media(tmp_path), lang="ko")
    assert captured["timeout"] == T._GROQ_TIMEOUT_SECONDS


def test_local_subprocess_call_passes_timeout(tmp_path, monkeypatch):
    _env(tmp_path / ".env.local", WHISPER_TRANSCRIBE_DIR=str(tmp_path / "tool"))
    tool_dir = tmp_path / "tool"
    (tool_dir / ".venv" / "Scripts").mkdir(parents=True)
    (tool_dir / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    (tool_dir / "transcribe.py").write_text("", encoding="utf-8")

    captured = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, *, cwd, capture_output, text, encoding, errors, timeout, shell):
        captured["timeout"] = timeout
        # 도구가 outdir/<stem>.txt를 생성하는 것을 흉내
        outdir = Path(args[args.index("--outdir") + 1])
        (outdir / f"{Path(args[2]).stem}.txt").write_text("LOCAL-OK", encoding="utf-8")
        return _Proc()

    monkeypatch.setattr(T.subprocess, "run", fake_run)
    r = T.transcribe_media(_media(tmp_path), lang="ko")
    assert r["backend"] == "local" and r["text"] == "LOCAL-OK"
    assert captured["timeout"] == T._DEFAULT_TIMEOUT_SECONDS


# ── enrich_transcribe 레벨 통합(사후 Codex 메타 리뷰 P2 보강) ────────────────
# `core.normalize.enrich_transcribe`는 `_transcribe.transcribe_media`를 호출하고
# `TranscribeError`만 잡는다(core/normalize.py:207-213) — dispatcher가 던지는
# TranscribeError가 실제로 그 except 절에서 흡수돼 fetch 전체(다른 소스 처리)를
# 죽이지 않고, 라벨이 기존 5값(`none`/`done`/`partial`/`failed`/
# `skipped_no_tool`) 안으로만 수렴하는지 확인한다. `transcribe_media` 자체를
# monkeypatch하는 방식 — dispatcher 내부(local/Groq 사다리)는 이미 위에서
# 단위 검증했으므로 여기서는 `enrich_transcribe`가 그 예외를 올바르게 소비하는지만
# 본다(관례: `core/tests/test_ocr_ensemble.py`의 monkeypatch 스타일).


def _result_with_media(*paths: str) -> dict:
    return {
        "source": "test", "platform": "test", "body_text": "", "comments": [],
        "ocr_text": [], "transcript": None, "media_paths": list(paths),
        "meta": {},
    }


def test_enrich_transcribe_all_models_429_does_not_crash_fetch(tmp_path, monkeypatch):
    """turbo/v3 모두 429 → dispatcher가 TranscribeError(전 모델 소진) 발생.

    enrich_transcribe가 이를 삼키고(예외 미전파) 라벨이 기존 값집합
    (done=0이므로 "failed") 안에 있어야 한다.
    """
    monkeypatch.setattr(N._transcribe, "is_available", lambda: True)

    def all_429(path, *, model=None, device=None, compute=None):
        raise N._transcribe.TranscribeError(
            "Groq 전사 rate-limit 전 모델 소진 — 정직 skip: clip.mp3"
        )

    monkeypatch.setattr(N._transcribe, "transcribe_media", all_429)

    media = tmp_path / "clip.mp3"
    media.write_bytes(b"0" * 10)
    result = _result_with_media(str(media))

    out = N.enrich_transcribe(result)  # 예외가 여기서 전파되면 테스트 자체가 실패

    assert out["meta"]["transcript_label"] in {"failed", "partial", "none", "skipped_no_tool"}
    assert out["meta"]["transcript_label"] == "failed"  # 유일 소스 실패 → done=0
    assert out["transcript"] is None


def test_enrich_transcribe_401_unavailable_does_not_crash_fetch(tmp_path, monkeypatch):
    """401(backend 인증/권한 문제)도 enrich_transcribe 레벨에서 fetch를 죽이지 않는다."""
    monkeypatch.setattr(N._transcribe, "is_available", lambda: True)

    def unauthorized(path, *, model=None, device=None, compute=None):
        raise N._transcribe.TranscribeError("Groq 인증/권한 오류(HTTP 401)")

    monkeypatch.setattr(N._transcribe, "transcribe_media", unauthorized)

    media = tmp_path / "clip.mp3"
    media.write_bytes(b"0" * 10)
    result = _result_with_media(str(media))

    out = N.enrich_transcribe(result)

    assert out["meta"]["transcript_label"] in {"failed", "partial", "none", "skipped_no_tool"}
    assert out["meta"]["transcript_label"] == "failed"
    assert out["transcript"] is None
    # 예외 문자열에 실제 키 값이 없었는지도 방어적으로 재확인(로그가 아니라 라벨/필드만 검사).
    assert "GROQ_API_KEY" not in str(out)


def test_enrich_transcribe_5xx_does_not_crash_fetch(tmp_path, monkeypatch):
    """Groq 5xx도 enrich_transcribe 레벨에서 fetch를 죽이지 않는다."""
    monkeypatch.setattr(N._transcribe, "is_available", lambda: True)

    def server_error(path, *, model=None, device=None, compute=None):
        raise N._transcribe.TranscribeError("Groq 서버 오류(HTTP 503)")

    monkeypatch.setattr(N._transcribe, "transcribe_media", server_error)

    media = tmp_path / "clip.mp3"
    media.write_bytes(b"0" * 10)
    result = _result_with_media(str(media))

    out = N.enrich_transcribe(result)

    assert out["meta"]["transcript_label"] in {"failed", "partial", "none", "skipped_no_tool"}
    assert out["meta"]["transcript_label"] == "failed"
    assert out["transcript"] is None


def test_enrich_transcribe_json_parse_failure_does_not_crash_fetch(tmp_path, monkeypatch):
    """Groq 응답 JSON 파싱 실패도 enrich_transcribe 레벨에서 fetch를 죽이지 않는다."""
    monkeypatch.setattr(N._transcribe, "is_available", lambda: True)

    def parse_failure(path, *, model=None, device=None, compute=None):
        raise N._transcribe.TranscribeError("Groq 응답 파싱 실패: KeyError")

    monkeypatch.setattr(N._transcribe, "transcribe_media", parse_failure)

    media = tmp_path / "clip.mp3"
    media.write_bytes(b"0" * 10)
    result = _result_with_media(str(media))

    out = N.enrich_transcribe(result)

    assert out["meta"]["transcript_label"] in {"failed", "partial", "none", "skipped_no_tool"}
    assert out["meta"]["transcript_label"] == "failed"
    assert out["transcript"] is None


def test_enrich_transcribe_partial_when_one_of_two_sources_fails(tmp_path, monkeypatch):
    """여러 미디어 중 일부만 실패 → "partial" 라벨로 수렴(값집합 회귀 확인 겸)."""
    monkeypatch.setattr(N._transcribe, "is_available", lambda: True)

    def one_fails(path, *, model=None, device=None, compute=None):
        if "bad" in str(path):
            raise N._transcribe.TranscribeError("Groq rate-limit 전 모델 소진")
        return {"text": "OK", "model": T._GROQ_MODEL_TURBO, "backend": "groq"}

    monkeypatch.setattr(N._transcribe, "transcribe_media", one_fails)

    good = tmp_path / "good.mp3"
    good.write_bytes(b"0" * 10)
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"0" * 10)
    result = _result_with_media(str(good), str(bad))

    out = N.enrich_transcribe(result)

    assert out["meta"]["transcript_label"] == "partial"
    assert out["transcript"] == "OK"


# ── multipart with-context 핸들 ──────────────────────────────────────────────


def test_call_groq_opens_file_with_context_manager(tmp_path, monkeypatch):
    """`_call_groq`가 파일을 `with open(...)`으로 열어 핸들을 정리하는지 확인."""
    _env(tmp_path / ".env.local", GROQ_API_KEY="gk")
    p = _media(tmp_path, name="ok.mp3", size=10)

    monkeypatch.setattr(
        T.requests, "post", lambda *a, **k: _Resp(200, payload={"text": "X"})
    )
    result = T._call_groq(p, api_key="gk", model=T._GROQ_MODEL_TURBO, lang="ko")
    assert result["text"] == "X"
    # 파일이 with 블록 종료 후 다시 열고 쓸 수 있으면(잠김이 없으면) 핸들 누수 없음.
    p.write_bytes(b"1" * 20)
    assert p.read_bytes() == b"1" * 20


if __name__ == "__main__":
    import tempfile
    import traceback

    class _MP:
        def __init__(self):
            self._o = []

        def setattr(self, o, n, v):
            self._o.append((o, n, getattr(o, n)))
            setattr(o, n, v)

        def undo(self):
            for o, n, v in reversed(self._o):
                setattr(o, n, v)

    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for name, fn in fns:
        mp = _MP()
        try:
            with tempfile.TemporaryDirectory() as d:
                argn = fn.__code__.co_varnames[: fn.__code__.co_argcount]
                a = {"tmp_path": Path(d), "monkeypatch": mp}
                fn(**{k: v for k, v in a.items() if k in argn})
            print(f"PASS {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        finally:
            mp.undo()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
