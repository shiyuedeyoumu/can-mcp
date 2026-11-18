from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 13399


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig


def _read_section(
    data: dict[str, Any],
    section_name: str,
    allowed_keys: set[str],
) -> dict[str, Any]:
    section = data.get(section_name, {})
    if not isinstance(section, dict):
        raise ValueError(f"Configuration section [{section_name}] must be a table.")
    unknown_keys = set(section) - allowed_keys
    if unknown_keys:
        names = ", ".join(sorted(unknown_keys))
        raise ValueError(f"Unknown key(s) in [{section_name}]: {names}.")
    return section


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path is not None else Path.cwd() / "config.toml"
    with config_path.open("rb") as config_file:
        raw_config = tomllib.load(config_file)

    unknown_sections = set(raw_config) - {"server"}
    if unknown_sections:
        names = ", ".join(sorted(unknown_sections))
        raise ValueError(f"Unknown configuration section(s): {names}.")

    server_values = _read_section(raw_config, "server", {"host", "port"})

    if "host" in server_values and not isinstance(server_values["host"], str):
        raise ValueError("[server].host must be a string.")
    if "port" in server_values and (
        not isinstance(server_values["port"], int) or isinstance(server_values["port"], bool)
    ):
        raise ValueError("[server].port must be an integer.")

    server_config = ServerConfig(**server_values)

    if not server_config.host.strip():
        raise ValueError("[server].host must not be empty.")
    if not 1 <= server_config.port <= 65535:
        raise ValueError("[server].port must be between 1 and 65535.")

    return AppConfig(server=server_config)
