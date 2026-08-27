"""round-29: TikTok gallery-dl 실행/다운로드 파싱 회귀 테스트(네트워크 없음).

2건 모두 2026-07-13 smart 스모크로 발굴된 실사용 silent-loss 버그의 회귀 가드:
- `_run`이 자식(gallery-dl)에 utf-8을 강제하지 않으면 한국 Windows(cp949)에서 한글·이모지
  파일 경로가 깨져 미디어가 통째 누락됐다.
- `_download`이 gallery-dl의 "이미 존재→skip" 출력(`# <경로>`)을 못 걸러 재fetch 시
  캐시된 미디어를 download_failed로 오판했다.
실행: `pytest adapters/tiktok/tests/test_download_parse.py` 또는 이 파일 직접.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from adapters import tiktok as tt  # noqa: E402


def test_run_forces_utf8_child_encoding(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(tt.subprocess, "run", fake_run)
    tt._run(["--version"], timeout=10)
    # R32: stdout 사이트는 IOENCODING만으로 경로 인코딩을 고친다(repro 9/9). PYTHONUTF8은
    # cp949 gallery-dl.conf 로딩을 깨뜨릴 수 있어(외부 리뷰 P2) 의도적으로 넣지 않는다.
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"


def test_download_includes_skipped_existing_files(tmp_path, monkeypatch):
    # gallery-dl이 이미 존재하는 파일을 "# <경로>"로 skip 출력해도 media_paths에 포함해야 한다.
    f1 = tmp_path / "a.jpg"; f1.write_bytes(b"x")
    f2 = tmp_path / "b.jpg"; f2.write_bytes(b"y")
    out = f"# {f1}\r\n{f2}\r\n\r\n"  # 하나는 skip(#), 하나는 신규 다운로드
    monkeypatch.setattr(tt, "_run",
                        lambda args, *, timeout: subprocess.CompletedProcess(args, 0, out, ""))
    paths = tt._download("https://www.tiktok.com/@x/photo/1", str(tmp_path))
    assert str(f1) in paths and str(f2) in paths
    assert len(paths) == 2


def test_download_skips_nonexistent_paths(tmp_path, monkeypatch):
    # 실존하지 않는 경로(오독·부분출력)는 제외한다.
    real = tmp_path / "real.jpg"; real.write_bytes(b"x")
    out = f"# {real}\r\n{tmp_path / 'ghost.jpg'}\r\n"
    monkeypatch.setattr(tt, "_run",
                        lambda args, *, timeout: subprocess.CompletedProcess(args, 0, out, ""))
    paths = tt._download("https://www.tiktok.com/@x/photo/1", str(tmp_path))
    assert paths == [str(real)]


def test_download_returns_empty_on_nonzero_rc(monkeypatch):
    monkeypatch.setattr(tt, "_run",
                        lambda args, *, timeout: subprocess.CompletedProcess(args, 1, "", "boom"))
    assert tt._download("https://www.tiktok.com/@x/photo/1", "downloads") == []


if __name__ == "__main__":
    import tempfile
    import traceback
    from pathlib import Path

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
