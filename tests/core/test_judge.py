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
        pytest.param(
            {"OPENROUTER_API_KEY": "or"}, (judge.DEFAULT_MODEL,), judge.OPENROUTER_BASE_URL, id="openrouter key"
        ),
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


@pytest.mark.parametrize(
    ("base_url", "providers", "routing"),
    [
        pytest.param("https://ai-gateway.vercel.sh/v1", "", {}, id="unpinned: the gateway routes"),
        pytest.param(
            "https://ai-gateway.vercel.sh/v1",
            "inference-net, fireworks",
            {
                "providerOptions": {
                    "gateway": {"only": ["inference-net", "fireworks"], "order": ["inference-net", "fireworks"]}
                }
            },
            id="AI Gateway",
        ),
        pytest.param(
            "https://openrouter.ai/api/v1",
            "deepinfra",
            {
                "provider": {
                    "require_parameters": True,
                    "only": ["deepinfra"],
                    "order": ["deepinfra"],
                    "allow_fallbacks": False,
                }
            },
            id="OpenRouter",
        ),
        pytest.param(
            "https://openrouter.ai/api/v1",
            "",
            {"provider": {"require_parameters": True}},
            id="OpenRouter unpinned still requires the reply schema",
        ),
        pytest.param("https://judge.example/v1", "", {}, id="another endpoint unpinned"),
        pytest.param("https://judge.example/v1", "deepinfra", None, id="a pin no gateway can honour is refused"),
    ],
)
def test_the_fallback_is_held_to_the_named_providers(
    monkeypatch: pytest.MonkeyPatch, base_url: str, providers: str, routing: dict[str, object] | None
) -> None:
    """2026-11-16: four AI Gateway hosts of DeepSeek V4.1 Flash 1017 ignored the strict reply schema, so 16 of the
    night's lite and platform trials graded INVALID; the pin keeps the fallback on hosts that enforce it."""
    monkeypatch.setenv("PARALLAX_JUDGE_API_KEY", "k")
    monkeypatch.setenv("PARALLAX_JUDGE_BASE_URL", base_url)
    monkeypatch.setenv(judge.PROVIDERS_ENV, providers)
    if routing is None:
        with pytest.raises(judge.JudgeConfigError, match="not a gateway"):
            judge.config_from_env()
        return
    assert judge.config_from_env().routing() == routing
