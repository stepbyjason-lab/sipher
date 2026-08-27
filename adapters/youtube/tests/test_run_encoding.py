"""round-32: youtube yt-dlp 자식 utf-8 인코딩 강제 회귀 가드(네트워크 없음).

미강제 시 한국 Windows(cp949)에서 yt-dlp probe JSON의 한글 제목/설명이 부모의 utf-8
디코딩과 어긋나 손상된다(R32 sweep, TikTok `_run`과 동형). 경로 수집은 glob이라 미디어
손실엔 면역이지만 텍스트 손상은 이 강제로 막는다.
실행: `pytest adapters/youtube/tests/test_run_encoding.py` 또는 이 파일 직접.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters.youtube import scrape as S  # noqa: E402


def test_run_forces_utf8_child_encoding(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(S.subprocess, "run", fake_run)
    S._run(["--version"], timeout=10)
    # R32: stdout 사이트는 IOENCODING만(외부 리뷰 P2로 PYTHONUTF8 배제).
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    # 부모 디코딩도 utf-8 유지(자식과 일치).
    assert captured["encoding"] == "utf-8"


if __name__ == "__main__":
    import traceback

    class _MP:
        def __init__(self): self._o = []
        def setattr(self, o, n, v): self._o.append((o, n, getattr(o, n))); setattr(o, n, v)
        def undo(self):
            for o, n, v in reversed(self._o): setattr(o, n, v)

    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for name, fn in fns:
        mp = _MP()
        try:
            fn(mp)
            print(f"PASS {name}")
        except Exception:
            fails += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        finally:
            mp.undo()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
