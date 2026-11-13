import pytest
from parallax import judge
from parallax.judge import family


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic/claude-sonnet-4", "anthropic"),
        ("openrouter/anthropic/claude-sonnet-4", "anthropic"),
        ("openrouter:anthropic/claude-opus-5.5", "anthropic"),
        ("inference_net:deepseek-v4.1-flash-1017", "deepseek"),
        ("vercel:typesafe-ai/jev", "typesafe-ai"),
        ("anthropic:claude-opus-5.5", "anthropic"),
        ("claude-sonnet-4", "anthropic"),
        ("gpt-5", "openai"),
        ("some-new-model", "some-new-model"),
    ],
)
def test_family(model: str, expected: str) -> None:
    assert family(model) == expected


@pytest.mark.parametrize(
    ("env", "panel", "base_url"),
    [
        pytest.param({}, None, None, id="no key: no panel, nothing spent"),
        pytest.param({"OPENROUTER_API_KEY": "or"}, (judge.DEFAULT_MODEL,), judge.OPENROUTER_BASE_URL, id="jev key"),
        pytest.param(
            {
                "OPENROUTER_API_KEY": "or",
                "PARALLAX_JUDGE_MODELS": "a/x, b/y",
                "PARALLAX_JUDGE_BASE_URL": "https://h/v1",
            },
            ("a/x", "b/y"),
            "https://h/v1",
            id="named panel and endpoint win",
        ),
    ],
)
def test_the_fallback_panel_defaults_once_a_key_can_reach_it(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], panel: tuple[str, ...] | None, base_url: str | None
) -> None:
    for key in ("OPENROUTER_API_KEY", "PARALLAX_JUDGE_API_KEY", "PARALLAX_JUDGE_MODELS", "PARALLAX_JUDGE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    found = judge.panel_from_env()
    assert (found.models if found else None) == panel
    if base_url is not None:
        assert judge.config_from_env().base_url == base_url
