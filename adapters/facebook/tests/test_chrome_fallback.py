from adapters.facebook import auth


class _FakeChromium:
    def __init__(self):
        self.calls = []

    def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("channel") == "chrome":
            raise RuntimeError("Chrome unavailable")
        return object()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()


def test_persistent_context_falls_back_to_bundled_chromium(tmp_path):
    p = _FakePlaywright()

    context = auth.open_persistent_context(p, tmp_path)

    assert context is not None
    assert p.chromium.calls[0]["channel"] == "chrome"
    assert "channel" not in p.chromium.calls[1]
