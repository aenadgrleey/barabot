"""Processor configuration: the TypeSafe API key, stored privately and atomically.

Processing decides matches with TypeSafe Jev, so its only setting is the
API key. config/processor.env may still hold embedding settings from the
embedding-based design; they are ignored.
"""

from __future__ import annotations

import getpass
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from barabot.core.config import PROJECT_ROOT

PROCESSOR_ENV_PATH = PROJECT_ROOT / "config" / "processor.env"
SETTINGS = ("TYPESAFE_API_KEY",)


class ConfigurationError(ValueError):
    """A processor setting failed validation."""


def _validate(values: dict[str, str]) -> None:
    for name, value in values.items():
        if not value:
            raise ConfigurationError(f"{name} must not be empty")
        if "\n" in value or "\r" in value:
            raise ConfigurationError(f"{name} must be a single line")


def configure(
    *,
    input_fn: Callable[[str], str] = input,
    secret_input: Callable[[str], str] = getpass.getpass,
) -> Path:
    """Ask for the API key and persist it."""
    del input_fn  # no plain-text settings left; kept for a stable signature
    values = {"TYPESAFE_API_KEY": secret_input("TYPESAFE_API_KEY: ").strip()}
    return configure_from_mapping(values, path=PROCESSOR_ENV_PATH)


def configure_from_mapping(values: dict[str, str], *, path: Path) -> Path:
    """Validate mapping-provided settings and persist them without prompting."""
    resolved = {name: values.get(name, "").strip() for name in SETTINGS}
    _validate(resolved)
    _atomic_write(resolved, path)
    return path


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
