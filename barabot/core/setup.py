"""Resumable interactive and incremental headless setup orchestration."""

from __future__ import annotations

import getpass
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from dotenv import set_key

from barabot.core.setup_hints import (
    BOT_COMMAND,
    PROCESSOR_COMMAND,
    READY_COMMAND,
    SETUP_COMMAND,
    has_deploy_environment,
    missing_setup_commands,
    next_setup_command,
)
from barabot.core.telegram import TelegramBackend

InputFunction = Callable[[str], str]
ScraperCredentials = Callable[..., Path]
ScraperAuthorize = Callable[..., Awaitable[None]]
ProcessorConfigure = Callable[..., Path]
BotAuthorize = Callable[[str, Path], Any]
ConfigLoader = Callable[[], Any]
HEADLESS_COMMAND = ".venv/bin/python -m barabot setup --headless"

PROCESSOR_NAMES = ("TYPESAFE_API_KEY",)


def _default_scraper_credentials(*, path: Path, input_func: InputFunction, secret_input_func: InputFunction) -> Path:
    from barabot.domains.scraper.login import configure_credentials

    return configure_credentials(path=path, input_func=input_func, secret_input_func=secret_input_func)


async def _default_scraper_authorize(*, root: Path) -> None:
    from barabot.domains.scraper.login import authorize

    await authorize(root=root)


def _default_processor_configure(*, path: Path) -> Path:
    from barabot.domains.processor import configure as configure_module

    previous_path = configure_module.PROCESSOR_ENV_PATH
    configure_module.PROCESSOR_ENV_PATH = path
    try:
        return configure_module.configure()
    finally:
        configure_module.PROCESSOR_ENV_PATH = previous_path


def _default_bot_authorize(token: str, path: Path) -> Any:
    from barabot.domains.bot.auth import authorize

    return authorize(token, path)


def _default_config_loader() -> Any:
    from barabot.core.config import load

    return load()


def _write_environment(path: Path, updates: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if path.exists():
                stream.write(path.read_text(encoding="utf-8"))
        for key, value in updates.items():
            set_key(temporary_path, key, value, quote_mode="never")
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _failure(component: str, command: str) -> int:
    print(f"{component} setup failed; resume with:", file=sys.stderr)
    print(command, file=sys.stderr)
    return 1


def _headless_failure(component: str, *names: str) -> int:
    print(
        f"{component} setup failed; check environment variable(s): {', '.join(names)}",
        file=sys.stderr,
    )
    print(f"rerun with:\n{HEADLESS_COMMAND}", file=sys.stderr)
    return 1


def _supplied(source: Mapping[str, str], names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for name in names if source.get(name, "").strip())


async def setup_scraper(
    *,
    root: Path,
    scraper_authorize: ScraperAuthorize = _default_scraper_authorize,
    config_loader: ConfigLoader = _default_config_loader,
) -> int:
    """Perform the sole interactive Telegram OTP/2FA authorization step."""
    del config_loader  # authorization must not require complete runtime configuration
    transport = TelegramBackend.configured(root).resolve()
    if transport.uses_local_session:
        await scraper_authorize(root=root)
    else:
        print("The tgmux gateway owns the scraper session.")
    print(f"Next: {next_setup_command(root, transport)}")
    return 0


async def setup(
    *,
    root: Path,
    force: bool = False,
    headless: bool = False,
    environ: Mapping[str, str] | None = None,
    input_func: InputFunction = input,
    secret_input_func: InputFunction = getpass.getpass,
    scraper_credentials: ScraperCredentials = _default_scraper_credentials,
    scraper_authorize: ScraperAuthorize = _default_scraper_authorize,
    processor_configure: ProcessorConfigure = _default_processor_configure,
    bot_authorize: BotAuthorize = _default_bot_authorize,
    config_loader: ConfigLoader = _default_config_loader,
) -> int:
    """Configure supplied/missing artifacts, while deferring Telegram authorization."""
    del scraper_authorize  # plain and headless setup never touch the session
    source = os.environ if environ is None else environ
    backend = TelegramBackend.configured(root, source)
    transport = backend.resolve()
    config_dir = root / "config"
    env_path = config_dir / ".env"
    scraper_path = config_dir / "scraper.env"
    processor_path = config_dir / "processor.env"
    bot_path = config_dir / "bot.token"

    if headless:
        # Each group is independent. An absent group is a no-op; a partial group
        # fails at its position before any later group is applied.
        updates = {}
        if source.get("BARABOT_ENV") and (force or not has_deploy_environment(env_path)):
            environment = source["BARABOT_ENV"].strip()
            if environment not in {"develop", "prod"}:
                return _headless_failure("environment", "BARABOT_ENV")
            updates["BARABOT_ENV"] = environment
        if "TELEGRAM_BACKEND" in source or updates:
            updates["TELEGRAM_BACKEND"] = backend.value
        if updates:
            try:
                _write_environment(env_path, updates)
            except OSError:
                return _headless_failure("environment", *updates)

        scraper_values = transport.uses_local_session and _supplied(source, ("API_ID", "API_HASH"))
        if scraper_values:
            missing = tuple(name for name in ("API_ID", "API_HASH") if not source.get(name, "").strip())
            if missing:
                return _headless_failure("scraper", *missing)
        if scraper_values and (force or not scraper_path.exists()):
            try:
                from barabot.domains.scraper.login import (
                    configure_credentials_from_mapping,
                )

                configure_credentials_from_mapping(source, path=scraper_path)
            except (KeyboardInterrupt, EOFError):
                raise
            except BaseException as exc:
                name = str(exc) if str(exc) in {"API_ID", "API_HASH"} else "API_ID"
                return _headless_failure("scraper", name)

        processor_values = _supplied(source, PROCESSOR_NAMES)
        if processor_values and not source.get("TYPESAFE_API_KEY", "").strip():
            return _headless_failure("processor", "TYPESAFE_API_KEY")
        if processor_values and (force or not processor_path.exists()):
            try:
                from barabot.domains.processor.configure import configure_from_mapping

                configure_from_mapping(dict(source), path=processor_path)
            except (KeyboardInterrupt, EOFError):
                raise
            except BaseException as exc:
                name = next((item for item in PROCESSOR_NAMES if item in str(exc)), "TYPESAFE_API_KEY")
                return _headless_failure("processor", name)

        bot_values = _supplied(source, ("BOT_TOKEN",))
        if bot_values and (force or not bot_path.exists()):
            try:
                bot_authorize(source["BOT_TOKEN"], bot_path)
            except (KeyboardInterrupt, EOFError):
                raise
            except BaseException:
                return _headless_failure("bot", "BOT_TOKEN")
    else:
        if has_deploy_environment(env_path) and not force:
            print("Skip environment: already configured.")
        else:
            try:
                while True:
                    environment = input_func("BARABOT_ENV [develop]: ").strip() or "develop"
                    if environment in {"develop", "prod"}:
                        break
                    print("BARABOT_ENV must be develop or prod.")
                _write_environment(env_path, {"BARABOT_ENV": environment, "TELEGRAM_BACKEND": backend.value})
            except (KeyboardInterrupt, EOFError):
                raise
            except BaseException:
                return _failure("environment", SETUP_COMMAND)

        if not transport.uses_local_session:
            print("Using the tgmux gateway for Telegram authorization.")
        elif scraper_path.exists() and not force:
            print("Skip scraper credentials: already configured.")
        else:
            try:
                scraper_credentials(
                    path=scraper_path,
                    input_func=input_func,
                    secret_input_func=secret_input_func,
                )
            except (KeyboardInterrupt, EOFError):
                raise
            except BaseException:
                return _failure("scraper credentials", SETUP_COMMAND)

        if processor_path.exists() and not force:
            print("Skip processor: already configured.")
        else:
            try:
                processor_configure(path=processor_path)
            except (KeyboardInterrupt, EOFError):
                raise
            except BaseException:
                return _failure("processor", PROCESSOR_COMMAND)

        if bot_path.exists() and not force:
            print("Skip bot: already configured.")
        else:
            try:
                token = secret_input_func("BotFather token: ")
                bot_authorize(token, bot_path)
            except (KeyboardInterrupt, EOFError):
                raise
            except BaseException:
                return _failure("bot", BOT_COMMAND)

    pending = missing_setup_commands(root, transport)
    if pending:
        print("Setup pending; run:")
        for command in pending:
            print(f"  {command}")
        return 0

    try:
        config_loader()
    except (KeyboardInterrupt, EOFError):
        raise
    except BaseException:
        return _failure("check", SETUP_COMMAND)
    print(f"Setup complete. Next: {READY_COMMAND}")
    return 0
