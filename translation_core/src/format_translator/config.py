from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    """A required provider setting is missing or malformed."""


@dataclass(frozen=True, slots=True)
class LlmProviderSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)


def load_llm_settings(path: Path | str = ".env") -> LlmProviderSettings:
    """Load OpenAI-compatible provider settings without logging secrets.

    The lowercase names retain compatibility with the user's existing file.
    Conventional uppercase aliases are also accepted so deployment can migrate
    to injected environment variables without changing application code.
    """

    env_path = Path(path).expanduser().resolve()
    file_values = _read_env_file(env_path)

    base_url = _first_value(
        file_values,
        "model_baseurl",
        "MODEL_BASE_URL",
        "OPENAI_BASE_URL",
    )
    api_key = _first_value(
        file_values,
        "apil_key",
        "api_key",
        "API_KEY",
        "OPENAI_API_KEY",
    )
    model = _first_value(
        file_values,
        "model_name",
        "MODEL_NAME",
        "OPENAI_MODEL",
    )

    missing = [
        label
        for label, value in (
            ("model_baseurl", base_url),
            ("apil_key", api_key),
            ("model_name", model),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            f"Missing required LLM setting(s) in {env_path}: {', '.join(missing)}"
        )
    if not base_url.startswith(("https://", "http://")):
        raise ConfigurationError("model_baseurl must start with http:// or https://")

    return LlmProviderSettings(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        model=model,
    )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigurationError(f"Environment file does not exist: {path}")
    values: dict[str, str] = {}
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(
                f"Invalid environment entry at {path}:{line_number}"
            )
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            raise ConfigurationError(f"Empty environment key at {path}:{line_number}")
        values[name] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _first_value(file_values: dict[str, str], *names: str) -> str:
    for name in names:
        environment_value = os.environ.get(name)
        if environment_value:
            return environment_value.strip()
        file_value = file_values.get(name)
        if file_value:
            return file_value.strip()
    return ""
