"""Interactive processor configuration with private atomic persistence."""
from __future__ import annotations

import getpass
import math
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from barabot.core.config import PROJECT_ROOT

PROCESSOR_ENV_PATH = PROJECT_ROOT / "config" / "processor.env"

_DEFAULTS = {
    "EMBEDDING_MODEL": "text-embedding-3-small",
    "EMBEDDING_URL": "https://api.openai.com/v1/embeddings",
    "SIMILARITY_THRESHOLD": "0.85",
    "EMBEDDING_CONCURRENCY": "4",
    "EMBEDDING_RETRIES": "3",
    "EMBEDDING_TIMEOUT": "30",
}


class ConfigurationError(ValueError):
    """A processor setting failed validation."""


def _prompt_with_default(
    input_fn: Callable[[str], str], name: str, default: str
) -> str:
    value = input_fn(f"{name} [{default}]: ").strip()
    return value or default


def _validate(values: dict[str, str]) -> None:
    for name in ("OPENAI_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_URL"):
        if not values[name].strip():
            raise ConfigurationError(f"{name} must not be empty")

    for name, value in values.items():
        if "\n" in value or "\r" in value:
            raise ConfigurationError(f"{name} must be a single line")

    try:
        threshold = float(values["SIMILARITY_THRESHOLD"])
    except ValueError:
        raise ConfigurationError(
            "SIMILARITY_THRESHOLD must be a number in [0, 1]"
        ) from None
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ConfigurationError("SIMILARITY_THRESHOLD must be in [0, 1]")

    limits = {
        "EMBEDDING_CONCURRENCY": 1,
        "EMBEDDING_RETRIES": 0,
        "EMBEDDING_TIMEOUT": 1,
    }
    for name, minimum in limits.items():
        try:
            value = int(values[name])
        except ValueError:
            raise ConfigurationError(f"{name} must be an integer") from None
        if value < minimum:
            raise ConfigurationError(f"{name} must be >= {minimum}")


def _atomic_write(values: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            for name, value in values.items():
                temporary_file.write(f"{name}={value}\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def configure(
    *,
    input_fn: Callable[[str], str] = input,
    secret_input: Callable[[str], str] = getpass.getpass,
) -> Path:
    """Collect, validate, and atomically persist processor settings."""
    values = {
        "OPENAI_API_KEY": secret_input("OPENAI_API_KEY: ").strip(),
        "EMBEDDING_MODEL": _prompt_with_default(
            input_fn, "EMBEDDING_MODEL", _DEFAULTS["EMBEDDING_MODEL"]
        ),
        "EMBEDDING_URL": _prompt_with_default(
            input_fn, "EMBEDDING_URL", _DEFAULTS["EMBEDDING_URL"]
        ),
        "SIMILARITY_THRESHOLD": _prompt_with_default(
            input_fn,
            "SIMILARITY_THRESHOLD",
            _DEFAULTS["SIMILARITY_THRESHOLD"],
        ),
        "EMBEDDING_CONCURRENCY": _prompt_with_default(
            input_fn,
            "EMBEDDING_CONCURRENCY",
            _DEFAULTS["EMBEDDING_CONCURRENCY"],
        ),
        "EMBEDDING_RETRIES": _prompt_with_default(
            input_fn, "EMBEDDING_RETRIES", _DEFAULTS["EMBEDDING_RETRIES"]
        ),
        "EMBEDDING_TIMEOUT": _prompt_with_default(
            input_fn, "EMBEDDING_TIMEOUT", _DEFAULTS["EMBEDDING_TIMEOUT"]
        ),
    }
    return configure_from_mapping(values, path=PROCESSOR_ENV_PATH)


def configure_from_mapping(values: dict[str, str], *, path: Path) -> Path:
    """Validate mapping-provided settings and persist them without prompting."""
    resolved = {
        "OPENAI_API_KEY": values.get("OPENAI_API_KEY", "").strip(),
        **{
            name: values.get(name, default).strip()
            for name, default in _DEFAULTS.items()
        },
    }
    _validate(resolved)
    _atomic_write(resolved, path)
    return path
