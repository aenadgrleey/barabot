"""Telegram scraper credentials and interactive session authorization."""

from __future__ import annotations

import getpass
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from barabot.core.setup_hints import has_local_scraper_authorization
from barabot.core.telegram import telegram_env_path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ClientFactory = Callable[[str, int, str], Any]
InputFunction = Callable[[str], str]


class CredentialInputError(ValueError):
    """A Telegram credential could not be collected safely."""


def _telethon_client(session: str, api_id: int, api_hash: str) -> Any:
    """Construct Telethon lazily so importing this module stays offline."""
    from telethon import TelegramClient

    return TelegramClient(session, api_id, api_hash)


def _credential_value(value: str, expected_key: str) -> str | None:
    """Accept a raw value or an assignment for exactly ``expected_key``."""
    value = value.strip()
    if "=" not in value:
        return value
    key, value = value.split("=", 1)
    if key.strip() != expected_key:
        return None
    value = value.strip()
    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            return None
        value = value[1:-1]
    elif value.endswith(("'", '"')):
        return None
    return value


def _valid_api_id(value: str) -> int | None:
    parsed = _credential_value(value, "API_ID")
    if parsed is None or not re.fullmatch(r"[0-9]+", parsed):
        return None
    api_id = int(parsed)
    return api_id if api_id > 0 else None


def _valid_api_hash(value: str) -> str | None:
    parsed = _credential_value(value, "API_HASH")
    if parsed is None:
        return None
    return parsed if re.fullmatch(r"[0-9a-fA-F]{32}", parsed) else None


def has_local_authorization(path: Path) -> bool:
    """Expose the shared offline readiness check for scraper callers."""
    return has_local_scraper_authorization(path)


def credentials_from_mapping(source: Mapping[str, str]) -> tuple[int, str]:
    """Validate scraper credentials without writing or exposing their values."""
    api_id = _valid_api_id(source.get("API_ID", ""))
    if api_id is None:
        raise CredentialInputError("API_ID")
    api_hash = _valid_api_hash(source.get("API_HASH", ""))
    if api_hash is None:
        raise CredentialInputError("API_HASH")
    return api_id, api_hash


def _read_credentials(path: Path) -> tuple[int, str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, UnicodeError, OSError):
        return None
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    try:
        return credentials_from_mapping(values)
    except CredentialInputError:
        return None


def _prompt_credentials(
    input_func: InputFunction,
    secret_input_func: InputFunction,
) -> tuple[int, str]:
    for _attempt in range(3):
        api_id = _valid_api_id(input_func("Telegram API_ID: "))
        if api_id is not None:
            break
        print("API_ID must be a positive integer.")
    else:
        raise CredentialInputError("API_ID")

    for _attempt in range(3):
        api_hash = _valid_api_hash(secret_input_func("Telegram API_HASH (input hidden; paste, then press Enter): "))
        if api_hash is not None:
            break
        print("API_HASH must be exactly 32 hexadecimal characters.")
    else:
        raise CredentialInputError("API_HASH")
    return api_id, api_hash


def _write_credentials(path: Path, api_id: int, api_hash: str) -> None:
    """Atomically replace the credentials file with private permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"API_ID={api_id}\nAPI_HASH={api_hash}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def configure_credentials_from_mapping(source: Mapping[str, str], *, path: Path) -> Path:
    """Validate and atomically persist credentials, never a session."""
    credentials = credentials_from_mapping(source)
    _write_credentials(path, *credentials)
    return path


def configure_credentials(
    *,
    path: Path,
    input_func: InputFunction = input,
    secret_input_func: InputFunction = getpass.getpass,
) -> Path:
    """Interactively persist credentials without authorizing Telegram."""
    credentials = _prompt_credentials(input_func, secret_input_func)
    _write_credentials(path, *credentials)
    return path


async def authorize(
    *,
    root: Path = PROJECT_ROOT,
    input_func: InputFunction = input,
    secret_input_func: InputFunction = getpass.getpass,
    client_factory: ClientFactory = _telethon_client,
) -> None:
    """Authorize the local Telethon session, reusing persisted credentials."""
    config_dir = root / "config"
    session_path = config_dir / "scraper.session"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_dir.chmod(0o700)
    credentials_path = telegram_env_path(config_dir)

    credentials = _read_credentials(credentials_path)
    if credentials is None:
        configure_credentials(
            path=credentials_path,
            input_func=input_func,
            secret_input_func=secret_input_func,
        )
        credentials = _read_credentials(credentials_path)
        if credentials is None:  # defensive: the atomic writer should make this impossible
            raise CredentialInputError("scraper credentials")
    else:
        credentials_path.chmod(0o600)

    client = client_factory(str(session_path), *credentials)
    try:
        await client.start()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram client was not authorized")
        if session_path.exists():
            session_path.chmod(0o600)
        print("Telegram authorization successful.")
    finally:
        await client.disconnect()
