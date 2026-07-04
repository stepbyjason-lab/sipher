"""round-20: core.lang.resolve_lang 우선순위·파싱·persist 단위 테스트.

실행: `pytest core/tests/` 또는 이 파일 직접(`python core/tests/test_lang.py`).
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pathlib import Path  # noqa: E402

from core import lang as L  # noqa: E402


def _reset(tmp_env_file: Path | None = None):
    """모듈 캐시·env 격리. tmp_env_file 지정 시 .env.local 경로를 임시로 치환."""
    L._cached = None
    os.environ.pop("SIPHER_LANG", None)
    if tmp_env_file is not None:
        L._ENV_FILE = tmp_env_file


# ── _normalize: subtag 파싱 ──

def test_normalize_bcp47():
    assert L._normalize("ko-KR") == "ko"


def test_normalize_posix_with_encoding():
    assert L._normalize("en_US.UTF-8") == "en"


def test_normalize_windows_legacy_name():
    assert L._normalize("Korean_Korea") == "ko"


def test_normalize_bare():
    assert L._normalize("ja") == "ja"


def test_normalize_garbage_returns_none():
    assert L._normalize("C") is None          # POSIX "C" locale — 언어 아님
    assert L._normalize("1234") is None
    assert L._normalize("") is None
    assert L._normalize(None) is None


# ── resolve 우선순위 ──

def test_env_var_wins(tmp_path):
    _reset(tmp_path / ".env.local")
    (tmp_path / ".env.local").write_text("SIPHER_LANG=ja\n", encoding="utf-8")
    os.environ["SIPHER_LANG"] = "fr"
    try:
        assert L.resolve_lang() == "fr"  # env > 파일
    finally:
        _reset(tmp_path / ".env.local")


def test_file_wins_over_detect(tmp_path):
    _reset(tmp_path / ".env.local")
    (tmp_path / ".env.local").write_text("GEMINI_API_KEY=x\nSIPHER_LANG=ja\n", encoding="utf-8")
    assert L.resolve_lang() == "ja"  # 파일 > OS 감지
    _reset(tmp_path / ".env.local")


def test_detect_persists_and_preserves_existing(tmp_path):
    env_file = tmp_path / ".env.local"
    _reset(env_file)
    env_file.write_text("GEMINI_API_KEY=secret123\n", encoding="utf-8")
    got = L.resolve_lang()  # env/파일에 없음 → OS 감지 → persist
    assert L._SUBTAG_RE.fullmatch(got), got
    content = env_file.read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=secret123" in content            # 기존 내용 보존
    assert f"SIPHER_LANG={got}" in content                  # 감지값 기록
    # 2회째 호출은 파일에서 읽음(중복 기록 없음)
    L._cached = None
    assert L.resolve_lang() == got
    assert content.count("SIPHER_LANG") == env_file.read_text(encoding="utf-8").count("SIPHER_LANG") == 1
    _reset(env_file)


def test_cache_is_process_wide(tmp_path):
    _reset(tmp_path / ".env.local")
    os.environ["SIPHER_LANG"] = "de"
    try:
        assert L.resolve_lang() == "de"
        os.environ["SIPHER_LANG"] = "es"       # 캐시 후 env 바꿔도
        assert L.resolve_lang() == "de"        # 프로세스당 1회 결정 유지
    finally:
        _reset(tmp_path / ".env.local")


# ── llm_free 프롬프트 회귀 ──

def test_ko_prompt_is_unchanged_poc_string():
    from core import llm_free
    assert llm_free._build_prompt("ko") == (
        "이 이미지에 있는 모든 한국어 텍스트를 빠짐없이·정확히 추출해라. "
        "카드 내 순서/구조 유지. 설명·해석 없이 텍스트만 출력."
    )


def test_non_ko_prompt_is_generic_english():
    from core import llm_free
    p = llm_free._build_prompt("en")
    assert "Extract ALL text" in p
    assert llm_free._build_prompt("ja") == p  # ko 외 전부 동일 범용


if __name__ == "__main__":
    import tempfile
    import traceback
    fails = 0
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for name, fn in fns:
        try:
            if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
