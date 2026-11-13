from __future__ import annotations

from parallax.harnesses.browser import pins


def test_pins_are_complete_and_have_registry_versions() -> None:
    entries = pins.load()
    assert {"browser-use-sdk", "browser-use", "browser-harness", "cua-driver", "@openai/codex"} <= entries.keys()
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
