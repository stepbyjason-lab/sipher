"""doc_id 사망 폴백(iphone 경로) 단위 테스트 — 네트워크 호출 없음.

2026-09-07 실측으로 들어온 경로다. instaloader가 박아둔 persisted query id가 죽어
`Post.from_shortcode`가 TypeError로 leak할 때, 어댑터가 `get_iphone_json`으로 구제한다.

여기서 고정하는 것은 셋이다.
  ① shortcode → media id 변환의 정확성
  ② 폴백이 **반드시 `get_iphone_json`을 거친다**는 것 — 생 requests로 우회하면
     instaloader의 `'iphone'` 레이트 버킷(1800초 창)을 잃어 계정이 잠길 수 있다
  ③ 폴백 실패가 조용히 성공으로 위장하지 않는다는 것
"""
import pytest

from adapters import instagram as ig


# ── ① shortcode → media id ─────────────────────────────────────────────────

def test_shortcode_to_media_id_matches_live_observed_pair():
    # 2026-09-07 라이브 실측: 이 shortcode의 api/v1 응답 items[0].pk가 이 값이었다.
    assert ig.shortcode_to_media_id("Dc1Dt4kFhhH") == 3978102193102657607


def test_shortcode_to_media_id_is_base64_positional():
    assert ig.shortcode_to_media_id("A") == 0
    assert ig.shortcode_to_media_id("B") == 1
    assert ig.shortcode_to_media_id("BA") == 64


def test_shortcode_to_media_id_rejects_unknown_character():
    # 조용히 틀린 id를 만들지 않는다 — 엉뚱한 미디어를 받아오는 것이 최악이다.
    with pytest.raises(ValueError):
        ig.shortcode_to_media_id("Dc1Dt4kFhh!")


# ── 가짜 instaloader 컨텍스트 ───────────────────────────────────────────────

class _FakeContext:
    """`get_iphone_json`만 흉내낸다. 호출 인자를 기록해 경로를 검증한다."""

    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []

    def get_iphone_json(self, path, params):
        self.calls.append((path, params))
        if self._raises is not None:
            raise self._raises
        return self._result


class _FakeLoader:
    def __init__(self, context):
        self.context = context


class _FakePost:
    def __init__(self, context, media):
        self.context = context
        self.media = media


class _FakeInstaloaderModule:
    """`Post.from_iphone_struct`만 있는 최소 모듈 스텁."""

    class Post:
        @staticmethod
        def from_iphone_struct(context, media):
            return _FakePost(context, media)


# ── ② 반드시 get_iphone_json 을 거친다 ──────────────────────────────────────

def test_fallback_goes_through_get_iphone_json_with_correct_path():
    ctx = _FakeContext(result={"items": [{"pk": 1, "code": "Dc1Dt4kFhhH"}]})
    post = ig._fetch_post_via_iphone_api(
        _FakeLoader(ctx), _FakeInstaloaderModule, "Dc1Dt4kFhhH"
    )
    assert post is not None
    # 레이트 컨트롤러·iphone 헤더가 붙는 유일한 통로다. 생 requests면 이 기록이 비었을 것이다.
    assert ctx.calls == [("api/v1/media/3978102193102657607/info/", {})]


def test_fallback_builds_post_via_from_iphone_struct():
    # shim을 손으로 만들지 않고 instaloader가 주는 진짜 Post를 쓴다 —
    # 캐러셀·영상·댓글이 기존 코드 경로 그대로 돌게 하려는 것이다.
    item = {"pk": 1, "code": "Dc1Dt4kFhhH", "media_type": 8}
    ctx = _FakeContext(result={"items": [item]})
    post = ig._fetch_post_via_iphone_api(
        _FakeLoader(ctx), _FakeInstaloaderModule, "Dc1Dt4kFhhH"
    )
    assert isinstance(post, _FakePost)
    assert post.media is item
    assert post.context is ctx


# ── ③ 폴백 실패는 조용히 성공이 되지 않는다 ─────────────────────────────────

@pytest.mark.parametrize("result", [None, {}, {"items": []}, {"items": None}])
def test_fallback_returns_none_on_empty_response(result):
    ctx = _FakeContext(result=result)
    assert ig._fetch_post_via_iphone_api(
        _FakeLoader(ctx), _FakeInstaloaderModule, "Dc1Dt4kFhhH"
    ) is None


@pytest.mark.parametrize("exc", [
    RuntimeError("network down"),
    KeyError("items"),
    ValueError("bad json"),
])
def test_fallback_swallows_its_own_failure_so_original_error_survives(exc):
    # 폴백이 새 예외를 던지면 진짜 원인(doc_id 사망)이 가려진다.
    # None을 돌려 호출부가 원래 InstagramAccessError를 올리게 한다.
    ctx = _FakeContext(raises=exc)
    assert ig._fetch_post_via_iphone_api(
        _FakeLoader(ctx), _FakeInstaloaderModule, "Dc1Dt4kFhhH"
    ) is None


def test_fallback_returns_none_on_bad_shortcode_without_network_call():
    ctx = _FakeContext(result={"items": [{"pk": 1}]})
    assert ig._fetch_post_via_iphone_api(
        _FakeLoader(ctx), _FakeInstaloaderModule, "bad!code"
    ) is None
    assert ctx.calls == []   # 변환이 먼저 실패하므로 요청 자체를 안 쓴다
