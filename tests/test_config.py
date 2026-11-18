from pathlib import Path

import pytest

from can_mcp.config import AppConfig, ServerConfig, load_config


def test_load_config_uses_toml_values_and_ignores_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_HOST", "environment-host")
    monkeypatch.setenv("MCP_PORT", "1")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[server]\nhost = "0.0.0.0"\nport = 18080\n',
        encoding="utf-8",
    )

    assert load_config(config_file) == AppConfig(
        server=ServerConfig(host="0.0.0.0", port=18080),
    )


def test_load_config_uses_defaults_for_missing_sections(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("", encoding="utf-8")

    assert load_config(config_file) == AppConfig(
        server=ServerConfig(),
    )


def test_load_config_rejects_can_configuration(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[can]\ninterface = "vector"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unknown configuration section"):
        load_config(config_file)


@pytest.mark.parametrize(
    ("toml", "error"),
    [
        ('[server]\nport = 0\n', r"\[server\]\.port"),
        ('[server]\nunknown = true\n', "Unknown key"),
        ('[server]\nport = "13399"\n', r"\[server\]\.port must be an integer"),
    ],
)
def test_load_config_rejects_invalid_values(
    tmp_path: Path,
    toml: str,
    error: str,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(toml, encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        load_config(config_file)
