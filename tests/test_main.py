from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import dotenv
import pytest


SETUP_COMMAND = ".venv/bin/python -m barabot setup"
SCRAPER_COMMAND = ".venv/bin/python -m barabot setup scraper"
PROCESSOR_COMMAND = ".venv/bin/python -m barabot processor configure"
SENTINEL_SECRET = "sentinel-secret"


def _cancelled_guidance(command: str) -> str:
    return f"Cancelled; rerun with:\n{command}\n"


def _import_main(
    monkeypatch: pytest.MonkeyPatch,
    login_module=None,
    auth_module=None,
    processor_configure_module=None,
    setup_module=None,
):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    sys.modules.pop("barabot.__main__", None)
    sys.modules.pop("barabot.core.config", None)
    if login_module is not None:
        monkeypatch.setitem(
            sys.modules, "barabot.domains.scraper.login", login_module
        )
    if auth_module is not None:
        monkeypatch.setitem(sys.modules, "barabot.domains.bot.auth", auth_module)
    if processor_configure_module is not None:
        monkeypatch.setitem(
            sys.modules,
            "barabot.domains.processor.configure",
            processor_configure_module,
        )
    if setup_module is not None:
        monkeypatch.setitem(sys.modules, "barabot.core.setup", setup_module)
    return importlib.import_module("barabot.__main__")


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        api_id=12345,
        watch_chats=[],
        similarity_threshold=0.85,
        log_level="INFO",
        db_path=Path("/fixed/data/barabot.sqlite"),
        scraper_session_path=Path("/fixed/config/scraper.session"),
    )


def _call_main_without_escaped_keyboard_interrupt(main_module, argv: list[str]) -> int:
    try:
        return main_module.main(argv)
    except KeyboardInterrupt:
        pytest.fail("main() must catch KeyboardInterrupt and return 130")


class TestRunCommand:
    def test_check_loads_config_without_runtime_or_network_work(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main_module = _import_main(monkeypatch)
        calls: list[str] = []

        def load_config():
            calls.append("load")
            return _config()

        def forbidden(*args, **kwargs):
            pytest.fail("run --check attempted runtime or network work")

        monkeypatch.setattr(main_module, "_load_config_or_exit", load_config)
        monkeypatch.setattr(main_module, "missing_setup_commands", lambda _root: [])
        monkeypatch.setattr(main_module, "_build_services", forbidden)
        monkeypatch.setattr(main_module, "_acquire_lock", forbidden)
        monkeypatch.setattr(main_module.asyncio, "run", forbidden)

        assert main_module.main(["run", "--check"]) == 0
        assert calls == ["load"]
        captured = capsys.readouterr()
        assert "config ok" in captured.out
        assert captured.err == ""

    def test_check_failure_preserves_error_and_appends_missing_setup_commands(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        main_module = _import_main(monkeypatch)
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / ".env").touch()
        (config_dir / "processor.env").touch()
        (config_dir / "bot.token").touch()
        monkeypatch.setattr(main_module, "missing_setup_commands", lambda _root: [])

        def fail_load():
            raise RuntimeError("API_ID is invalid")

        monkeypatch.setattr(sys.modules["barabot.core.config"], "load", fail_load)
        monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["run", "--check"])

        assert exc_info.value.code == 2
        assert capsys.readouterr().err.splitlines() == [
            "config check failed: API_ID is invalid",
            "missing setup:",
            "  .venv/bin/python -m barabot setup",
            "  .venv/bin/python -m barabot setup scraper",
        ]

    @pytest.mark.parametrize("session_state", ["missing", "unauthorized"])
    def test_check_rejects_missing_or_unauthorized_session_before_claiming_config_ok(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        session_state: str,
    ) -> None:
        main_module = _import_main(monkeypatch)
        config = tmp_path / "config"
        config.mkdir()
        for name in (".env", "scraper.env", "processor.env", "bot.token"):
            (config / name).touch()
        if session_state == "unauthorized":
            with (config / "scraper.session").open("wb") as stream:
                stream.write(b"not-an-authorized-session")
        monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(
            main_module,
            "_load_config_or_exit",
            lambda: pytest.fail("incomplete readiness must be rejected before config loader"),
        )

        try:
            result = main_module.main(["run", "--check"])
        except SystemExit as exc:
            assert isinstance(exc.code, int)
            result = exc.code

        assert result != 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "config ok" not in combined.lower()
        assert SCRAPER_COMMAND in combined


class TestSetupCommand:
    @pytest.mark.parametrize(
        ("argv", "expected_force", "expected_headless"),
        [
            (["setup"], False, False),
            (["setup", "-f"], True, False),
            (["setup", "--force"], True, False),
            (["setup", "--forced"], True, False),
            (["setup", "--headless"], False, True),
            (["setup", "--headless", "--force"], True, True),
        ],
    )
    def test_setup_routes_modes_to_async_orchestrator_without_starting_services(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        argv: list[str],
        expected_force: bool,
        expected_headless: bool,
    ) -> None:
        calls: list[object] = []
        setup_module = types.ModuleType("barabot.core.setup")

        async def setup(*, root: Path, force: bool, headless: bool) -> int:
            calls.append((root, force, headless))
            return 0

        setattr(setup_module, "setup", setup)
        main_module = _import_main(monkeypatch, setup_module=setup_module)
        monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(
            main_module,
            "_load_config_or_exit",
            lambda: pytest.fail("setup must not use the run config path"),
        )
        monkeypatch.setattr(
            main_module,
            "_build_services",
            lambda config: pytest.fail("setup must not build runtime services"),
        )

        assert main_module.main(argv) == 0
        assert calls == [(tmp_path, expected_force, expected_headless)]

    def test_setup_scraper_routes_authorization_without_loading_complete_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        calls: list[Path] = []
        setup_module = types.ModuleType("barabot.core.setup")

        async def setup(*, root: Path, force: bool, headless: bool) -> int:
            pytest.fail("setup scraper must use its dedicated wrapper")

        async def setup_scraper(*, root: Path) -> int:
            calls.append(root)
            return 0

        setattr(setup_module, "setup", setup)
        setattr(setup_module, "setup_scraper", setup_scraper)
        main_module = _import_main(monkeypatch, setup_module=setup_module)
        monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(
            main_module,
            "_load_config_or_exit",
            lambda: pytest.fail("setup scraper must not load complete runtime Config"),
        )

        assert main_module.main(["setup", "scraper"]) == 0
        assert calls == [tmp_path]

    @pytest.mark.parametrize(
        "cancel", [KeyboardInterrupt(), EOFError(SENTINEL_SECRET)]
    )
    def test_setup_cancellation_returns_130_with_safe_rerun_guidance(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        cancel: BaseException,
    ) -> None:
        setup_module = types.ModuleType("barabot.core.setup")

        async def setup(*, root: Path, force: bool, headless: bool) -> int:
            assert headless is False
            raise cancel

        setattr(setup_module, "setup", setup)
        main_module = _import_main(monkeypatch, setup_module=setup_module)
        monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)

        assert _call_main_without_escaped_keyboard_interrupt(
            main_module, ["setup"]
        ) == 130
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == _cancelled_guidance(SETUP_COMMAND)

    @pytest.mark.parametrize(
        "cancel", [KeyboardInterrupt(), EOFError(SENTINEL_SECRET)]
    )
    def test_setup_scraper_cancellation_returns_130_with_exact_safe_guidance(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        cancel: BaseException,
    ) -> None:
        setup_module = types.ModuleType("barabot.core.setup")

        async def setup_scraper(*, root: Path) -> int:
            raise cancel

        setattr(setup_module, "setup_scraper", setup_scraper)
        main_module = _import_main(monkeypatch, setup_module=setup_module)

        assert _call_main_without_escaped_keyboard_interrupt(
            main_module, ["setup", "scraper"]
        ) == 130
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == _cancelled_guidance(SCRAPER_COMMAND)


class TestRemovedScraperCommand:
    def test_top_level_help_does_not_expose_scraper(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main_module = _import_main(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["--help"])
        assert exc_info.value.code == 0
        help_text = capsys.readouterr().out.lower()
        choices = help_text.split("positional arguments:", 1)[0]
        assert "{run,setup,processor,bot}" in choices
        assert "{run,setup,scraper,processor,bot}" not in help_text

    def test_obsolete_scraper_login_route_is_rejected_by_argparse(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        login_module = types.ModuleType("barabot.domains.scraper.login")

        async def forbidden_login() -> None:
            pytest.fail("obsolete parser route reached login instead of rejecting arguments")

        setattr(login_module, "login", forbidden_login)
        main_module = _import_main(monkeypatch, login_module=login_module)
        with pytest.raises(SystemExit) as exc_info:
            main_module.main(["scraper", "login"])
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "invalid choice" in captured.err

    def test_documentation_exposes_only_setup_scraper_authorization(self) -> None:
        root = Path(__file__).resolve().parents[1]
        documentation = (root / "README.md").read_text(encoding="utf-8")
        example = (root / ".env.example").read_text(encoding="utf-8")

        assert "setup scraper" in documentation
        assert "scraper login" not in documentation.lower()
        assert "BARABOT_SCRAPER_SESSION_B64" not in documentation + example


class TestProcessorCommand:
    def test_configure_routes_without_loading_config_and_prints_saved_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        saved_path = Path("/fixed/config/processor.env")
        configure_module = types.ModuleType("barabot.domains.processor.configure")

        class ConfigurationError(ValueError):
            pass

        def configure() -> Path:
            return saved_path

        setattr(configure_module, "ConfigurationError", ConfigurationError)
        setattr(configure_module, "configure", configure)
        main_module = _import_main(
            monkeypatch, processor_configure_module=configure_module
        )
        monkeypatch.setattr(
            main_module,
            "_load_config_or_exit",
            lambda: pytest.fail("processor configure must not load process config"),
        )

        assert main_module.main(["processor", "configure"]) == 0
        captured = capsys.readouterr()
        assert captured.out == f"Processor configuration saved: {saved_path}\n"
        assert captured.err == ""

    def test_configure_handles_configuration_error_without_exposing_details(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        configure_module = types.ModuleType("barabot.domains.processor.configure")

        class ConfigurationError(ValueError):
            pass

        def configure() -> Path:
            raise ConfigurationError("secret-value must not be printed")

        setattr(configure_module, "ConfigurationError", ConfigurationError)
        setattr(configure_module, "configure", configure)
        main_module = _import_main(
            monkeypatch, processor_configure_module=configure_module
        )
        monkeypatch.setattr(
            main_module,
            "_load_config_or_exit",
            lambda: pytest.fail("processor configure must not load process config"),
        )

        assert main_module.main(["processor", "configure"]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "processor configuration failed\n"

    @pytest.mark.parametrize(
        "cancel", [KeyboardInterrupt(), EOFError(SENTINEL_SECRET)]
    )
    def test_configure_cancellation_returns_130_without_success_output_or_secret(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        cancel: BaseException,
    ) -> None:
        configure_module = types.ModuleType("barabot.domains.processor.configure")

        class ConfigurationError(ValueError):
            pass

        def configure() -> Path:
            raise cancel

        setattr(configure_module, "ConfigurationError", ConfigurationError)
        setattr(configure_module, "configure", configure)
        main_module = _import_main(
            monkeypatch, processor_configure_module=configure_module
        )

        assert _call_main_without_escaped_keyboard_interrupt(
            main_module, ["processor", "configure"]
        ) == 130
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == _cancelled_guidance(PROCESSOR_COMMAND)


class TestBotCommand:
    def test_auth_routes_required_token_without_loading_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "123456:secret-token"
        calls: list[object] = []
        auth_module = types.ModuleType("barabot.domains.bot.auth")

        class AuthorizationError(Exception):
            pass

        def authorize(received_token: str, token_path: Path):
            calls.append((received_token, token_path))
            return SimpleNamespace(id=42, username="safe_bot")

        setattr(auth_module, "AuthorizationError", AuthorizationError)
        setattr(auth_module, "authorize", authorize)
        main_module = _import_main(monkeypatch, auth_module=auth_module)
        monkeypatch.setattr(
            main_module,
            "_load_config_or_exit",
            lambda: pytest.fail("bot auth must not load config/.env"),
        )

        assert main_module.main(["bot", "auth", token]) == 0
        assert calls == [(token, main_module.Config.bot_token_path)]
