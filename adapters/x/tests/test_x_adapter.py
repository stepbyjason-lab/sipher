"""R45 X 어댑터 오프라인 테스트 — 검체 정답지(gallery-dl 실출력)를 고정 입력으로 쓴다.

gallery-dl subprocess(`_run`)만 가짜로 바꾼다. `--dump-json` 호출에는 정답지 JSON을,
다운로드 호출에는 `--filter`가 허용한 트윗의 파일과 `--write-metadata` 사이드카를 만들어
돌려준다. 기획서 3절 ⓐ~ⓖ를 잰다(ⓗ 실제 픽셀은 라이브 관측 몫).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core  # noqa: E402
from adapters import x  # noqa: E402

CORPUS = ROOT / ".handoff" / "rounds" / "r45" / "x-corpus"
PAPI = ("https://x.com/papisleepy_rf/status/2098287402024317051", "papisleepy_rf-2098287402024317051.json")
NICO = ("https://x.com/nicos_ai/status/2098764217947914722", "nicos_ai-2098764217947914722.json")
NICO_QUOTE = "2096925496323813778"
NICO_THREAD = "2098764222804963809"
_REAL_COOKIES_FILE = x._cookies_file


def _corpus(name: str) -> list:
    return json.loads((CORPUS / name).read_text(encoding="utf-8"))


def _fake_gallery_dl(monkeypatch, stdout: str, *, calls: list | None = None, rc: int = 0, stderr: str = ""):
    data = json.loads(stdout) if stdout.startswith("[") else []

    def fake_run(args, *, timeout):
        if calls is not None:
            calls.append(list(args))
        if "--dump-json" in args:
            return subprocess.CompletedProcess(args, rc, stdout, stderr)
        out = Path(args[args.index("-d") + 1])
        keep = {int(i) for i in re.findall(r"\d+", args[args.index("--filter") + 1])}
        lines = []
        for item in data:
            if item[0] != 3 or int(item[2]["tweet_id"]) not in keep:
                continue
            meta = item[2]
            f = out / "twitter" / f"{meta['tweet_id']}_{meta['num']}.{meta.get('extension') or 'jpg'}"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
            Path(str(f) + ".json").write_text(json.dumps(meta), encoding="utf-8")
            lines.append(str(f))
        return subprocess.CompletedProcess(args, 0, "\n".join(lines) + "\n", "")

    monkeypatch.setattr(x, "_run", fake_run)
    monkeypatch.setattr(x, "_cookies_file", lambda: None)
    monkeypatch.setattr(x, "_probe_size", lambda path: None)  # 가짜 파일 — 실측은 시험별로 바꿔 끼운다


def _fetch(monkeypatch, sample, **kwargs) -> dict:
    url, name = sample
    _fake_gallery_dl(monkeypatch, (CORPUS / name).read_text(encoding="utf-8"))
    return x.fetch(url, **kwargs)


def _ids(result: dict) -> set[str]:
    return {result["meta"]["tweet_id"], *(it["id"] for it in x._walk(result))}


def _strip_time(result: dict) -> dict:
    return {**result, "meta": {k: v for k, v in result["meta"].items() if k != "fetched_at"}}


# ⓐ 꼬리 파라미터
def test_a_tail_params_give_same_result(monkeypatch):
    bare = _fetch(monkeypatch, PAPI)
    tailed = _fetch(monkeypatch, (PAPI[0] + "?s=46", PAPI[1]))
    assert _strip_time(bare) == _strip_time(tailed)
    assert x.parse_url("twitter.com/nicos_ai/status/2098764217947914722?s=20") == (
        NICO[0], "2098764217947914722")


def test_parse_url_rejects_other_hosts_and_paths():
    for bad in ("https://box.com/a/status/1", "https://x.com/nicos_ai", "https://evil.com/x.com/a/status/1"):
        with pytest.raises(ValueError):
            x.parse_url(bad)


# ⓑ 검체 1
def test_b_sample1_ids_body_video(monkeypatch, tmp_path):
    data = _corpus(PAPI[1])
    out = _fetch(monkeypatch, PAPI, download=True, media_dir=str(tmp_path))
    assert _ids(out) == {str(i[-1]["tweet_id"]) for i in data}
    root = next(i[-1] for i in data if i[0] == 2 and str(i[-1]["tweet_id"]) == out["meta"]["tweet_id"])
    assert out["body_text"] == root["content"]
    assert out["meta"]["media"] == [{"url": data[1][1], "type": "video", "width": 720, "height": 744,
                                     "extension": "mp4"}]
    assert [(f["width"], f["height"]) for f in out["meta"]["media_files"]] == [(720, 744)]
    assert out["media_paths"][0].endswith(".mp4")
    assert out["meta"]["media_label"] == "downloaded"


# ⓗ 실측 크기 — 선언보다 작은 변형을 받으면 실측을 쓰고 선언은 따로 남긴다(iter 4)
def test_h_media_files_use_probed_size_and_keep_declared(monkeypatch, tmp_path):
    _fake_gallery_dl(monkeypatch, (CORPUS / PAPI[1]).read_text(encoding="utf-8"))
    monkeypatch.setattr(x, "_probe_size", lambda path: (360, 372))
    out = x.fetch(PAPI[0], download=True, media_dir=str(tmp_path))
    (f,) = out["meta"]["media_files"]
    assert (f["width"], f["height"], f["size_source"]) == (360, 372, "probe")
    assert (f["declared_width"], f["declared_height"]) == (720, 744)


def test_h_media_files_fall_back_to_sidecar_without_ffprobe(monkeypatch, tmp_path):
    out = _fetch(monkeypatch, PAPI, download=True, media_dir=str(tmp_path))
    (f,) = out["meta"]["media_files"]
    assert (f["width"], f["height"], f["size_source"]) == (720, 744, "sidecar")
    assert (f["declared_width"], f["declared_height"]) == (720, 744)


def test_h_probe_size_parses_ffprobe_and_tolerates_absence(monkeypatch):
    monkeypatch.setattr(x.shutil, "which", lambda name: None)
    assert x._probe_size("a.mp4") is None
    monkeypatch.setattr(x.shutil, "which", lambda name: "ffprobe")
    for stdout, want in (("720,720\n", (720, 720)), ("1082,2048\n", (1082, 2048)), ("", None), ("N/A,N/A\n", None)):
        monkeypatch.setattr(x.subprocess, "run",
                            lambda *a, _o=stdout, **k: subprocess.CompletedProcess(a, 0, _o, ""))
        assert x._probe_size("a.mp4") == want


# ⓒ 검체 2
def test_c_sample2_ids_video_article_counts(monkeypatch):
    data = _corpus(NICO[1])
    out = _fetch(monkeypatch, NICO)
    assert _ids(out) == {str(i[-1]["tweet_id"]) for i in data}
    assert out["body_text"].startswith("🚨UN DESARROLLADOR")
    assert [(m["type"], m["width"], m["height"]) for m in out["meta"]["media"]] == [("video", 2560, 1390)]
    (quote,) = out["quoted"]
    assert quote["article"]["title"].startswith("Guía de configuración de GPT-6 Astra")
    assert [(m["type"], m["width"], m["height"]) for m in quote["media"]] == [
        ("article:cover", 1600, 640), ("article:image", 1200, 991), ("article:image", 1200, 676)]
    assert all("name=orig" in m["url"] for m in quote["media"])
    assert len(out["author_thread"]) == 1
    assert out["meta"]["author_reply_count"] == 10
    # 기획서 ⓒ는 「53 이상」이라 적었지만 정답지의 타인 답글은 52건이다(고유 트윗 65 = 원글 1 +
    # 인용 1 + 이어쓰기 1 + 저자 대댓글 10 + 타인 52). 53은 인용 포스트를 답글로 셀 때만 나온다
    # — 그건 ⓓ와 어긋나므로 정답지 실수로 보고 「정답지의 타인 답글 전부」를 잰다(6절 질문).
    assert out["meta"]["other_reply_count"] == 52 == out["meta"]["other_reply_total"]


# ⓓ 인용은 답글이 아니라 인용 블록
def test_d_quoted_article_is_quote_block_not_reply(monkeypatch):
    out = _fetch(monkeypatch, NICO)
    assert [q["id"] for q in out["quoted"]] == [NICO_QUOTE]
    assert NICO_QUOTE not in {c["id"] for c in out["comments"]}


# ⓔ 저자 이어쓰기는 본문 쪽, 저자 대댓글은 댓글 쪽
def test_e_author_thread_and_author_replies(monkeypatch):
    out = _fetch(monkeypatch, NICO)
    assert [t["id"] for t in out["author_thread"]] == [NICO_THREAD]
    assert NICO_THREAD not in {c["id"] for c in out["comments"]}
    author_replies = [c for c in out["comments"] if c["kind"] == "author_reply"]
    assert len(author_replies) == 10
    assert all(c["author"] == "nicos_ai" and c["reply_to_id"] != out["meta"]["tweet_id"] for c in author_replies)


# ⓕ 쿠키 없음 → 오류, 빈 성공 아님
def test_f_auth_required_in_dump_raises(monkeypatch):
    # 2026-10-07 쿠키 없이 실측한 gallery-dl 출력(rc=0)
    stdout = json.dumps([[-1, {"error": "AuthRequired",
                               "message": "authenticated cookies needed to access this timeline"}]])
    _fake_gallery_dl(monkeypatch, stdout)
    with pytest.raises(x.XAuthRequired, match="AuthRequired"):
        x.fetch(PAPI[0])


def test_f_auth_required_on_stderr_and_empty_dump_raise(monkeypatch):
    _fake_gallery_dl(monkeypatch, "", rc=16, stderr="[twitter][error] AuthRequired: cookies needed")
    with pytest.raises(x.XAuthRequired):
        x.fetch(PAPI[0])
    _fake_gallery_dl(monkeypatch, "[]")
    with pytest.raises(x.GalleryDlError):
        x.fetch(PAPI[0])


def test_f_download_auth_required_raises_not_download_failed(monkeypatch, tmp_path):
    # iter 1 F1: dump는 성공하고 다운로드 호출만 만료 쿠키로 실패(rc=16, stderr AuthRequired).
    dump = (CORPUS / PAPI[1]).read_text(encoding="utf-8")

    def fake_run(args, *, timeout):
        if "--dump-json" in args:
            return subprocess.CompletedProcess(args, 0, dump, "")
        return subprocess.CompletedProcess(args, 16, "", "[twitter][error] AuthRequired: cookies needed")

    monkeypatch.setattr(x, "_run", fake_run)
    monkeypatch.setattr(x, "_cookies_file", lambda: None)
    with pytest.raises(x.XAuthRequired, match="AuthRequired"):
        x.fetch(PAPI[0], download=True, media_dir=str(tmp_path))


def test_f_core_cli_reports_error_not_success(monkeypatch, capsys):
    import core.__main__ as cli
    _fake_gallery_dl(monkeypatch, json.dumps([[-1, {"error": "AuthRequired", "message": "m"}]]))
    rc = cli.main(["fetch", PAPI[0] + "?s=46", "--json", "--no-smart"])
    assert rc == 1
    assert "AuthRequired" in capsys.readouterr().err


# ⓖ 답글 상한은 타인 답글에만
def test_g_reply_cap_only_cuts_others(monkeypatch):
    out = _fetch(monkeypatch, NICO, max_replies=5)
    assert out["meta"]["other_reply_count"] == 5
    assert sum(c["kind"] == "reply" for c in out["comments"]) == 5
    assert out["meta"]["author_reply_count"] == 10
    assert len(out["author_thread"]) == 1
    assert out["meta"]["replies_truncated"] is True


def test_g_download_filter_follows_cap(monkeypatch, tmp_path):
    calls: list = []
    _fake_gallery_dl(monkeypatch, (CORPUS / NICO[1]).read_text(encoding="utf-8"), calls=calls)
    capped = x.fetch(NICO[0], download=True, media_dir=str(tmp_path / "a"), max_replies=0)
    # AnimeeNoa3의 사진 답글은 상한 밖이라 받지 않는다. 원글 영상 1 + 인용 아티클 3.
    assert len(capped["media_paths"]) == 4
    assert capped["meta"]["media_label"] == "downloaded"
    full = x.fetch(NICO[0], download=True, media_dir=str(tmp_path / "b"))
    assert len(full["media_paths"]) == 5
    assert "--write-metadata" in calls[-1] and "--dump-json" not in calls[-1]


def test_cookie_file_is_passed_to_gallery_dl(monkeypatch, tmp_path):
    calls: list = []
    _fake_gallery_dl(monkeypatch, (CORPUS / PAPI[1]).read_text(encoding="utf-8"), calls=calls)
    jar = tmp_path / "x_cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(x, "_ROOT", tmp_path)
    (tmp_path / ".env.local").write_text("X_COOKIES_FILE=x_cookies.txt\n", encoding="utf-8")
    monkeypatch.setattr(x, "_cookies_file", _REAL_COOKIES_FILE)
    out = x.fetch(PAPI[0])
    assert calls[0][calls[0].index("--cookies") + 1] == str(jar)
    assert out["meta"]["cookies_used"] is True
    # 설정했는데 파일이 없으면 gallery-dl을 부르기 전에 그 사실을 오류로 낸다.
    jar.unlink()
    with pytest.raises(x.XAuthRequired, match="쿠키 파일이 없습니다"):
        x.fetch(PAPI[0])



# 라우터
def test_core_routes_x_and_twitter_with_smart_download(monkeypatch):
    for url in (NICO[0], "https://twitter.com/a/status/1", "https://mobile.twitter.com/a/status/1?s=46"):
        assert core.detect_platform(url) == "x"
    calls = {}

    def fake_adapter_fetch(platform):
        def _fetch(url, **kwargs):
            calls.update(kwargs, platform=platform)
            return {"source": url, "platform": platform, "body_text": "", "comments": [],
                    "ocr_text": [], "transcript": None, "media_paths": [], "meta": {}}
        return _fetch

    monkeypatch.setattr(core, "_adapter_fetch", fake_adapter_fetch)
    core.fetch(NICO[0], ocr=False, transcribe=False, max_replies=7)
    assert calls == {"platform": "x", "download": True, "media_dir": "downloads", "max_replies": 7}
    core.fetch(NICO[0], ocr=False, transcribe=False, download=False)
    assert calls["download"] is False


def test_max_replies_must_be_non_negative_int(monkeypatch):
    _fake_gallery_dl(monkeypatch, (CORPUS / PAPI[1]).read_text(encoding="utf-8"))
    for bad in (-1, "5", True):
        with pytest.raises(ValueError):
            x.fetch(PAPI[0], max_replies=bad)
