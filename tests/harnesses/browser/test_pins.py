from __future__ import annotations

from parallax.harnesses.browser import pins


def test_pins_are_complete_and_have_registry_versions() -> None:
    entries = pins.load()
    assert {
        "browser-use-sdk",
        "browser-use",
        "browser-harness",
        "agent-browser",
        "cua-driver",
        "@openai/codex",
    } <= entries.keys()
    assert all(entry["registry"] in {"pypi", "npm"} and entry["version"] for entry in entries.values())


def test_stale_reports_only_packages_that_changed(monkeypatch: object) -> None:
    class FakeHTTP:
        def get(self, url: str) -> object:
            class Response:
                def raise_for_status(self) -> None:
                    return None

                def json(self) -> dict[str, dict[str, str]]:
                    return {"info": {"version": "1.0.2"}}

            return Response()

    monkeypatch.setattr(pins, "load", lambda: {"browser-use": {"registry": "pypi", "version": "1.0.1"}})
    assert pins.stale(FakeHTTP()) == [
        {"name": "browser-use", "registry": "pypi", "version": "1.0.1", "latest": "1.0.2"}
    ]


def test_arm_pin_checks_the_distribution_it_installs() -> None:
    """An arm-specific pin resolves its `package`, not its own key, against the registry."""
    import httpx

    requested: list[str] = []

    def registry(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/pypi/browser-use/json":
            return httpx.Response(200, json={"info": {"version": "1.0.8"}})
        return httpx.Response(404, json={"message": "Not Found"})

    with httpx.Client(transport=httpx.MockTransport(registry)) as http:
        assert pins.stale(http, ["browser-use-1"]) == []
    assert requested == ["/pypi/browser-use/json"]
