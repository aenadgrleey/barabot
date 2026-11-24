from __future__ import annotations

import importlib
import stat
from pathlib import Path

import pytest

SECRET = "ts-test-super-secret"


def _read_env(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)


def test_configure_stores_the_key_privately_and_reads_it_as_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("barabot.domains.processor.configure")
    monkeypatch.setattr(module, "PROCESSOR_ENV_PATH", tmp_path / "config" / "processor.env")
    prompts: list[str] = []

    def secret_input(prompt: str) -> str:
        prompts.append(prompt)
        return SECRET

    path = module.configure(input_fn=lambda _p: pytest.fail("no plain prompts"), secret_input=secret_input)

    assert path == tmp_path / "config" / "processor.env"
    assert prompts == ["TYPESAFE_API_KEY: "]
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _read_env(path) == {"TYPESAFE_API_KEY": SECRET}


def test_configure_from_mapping_ignores_legacy_embedding_settings(tmp_path: Path) -> None:
    module = importlib.import_module("barabot.domains.processor.configure")
    path = tmp_path / "config" / "processor.env"

    module.configure_from_mapping({"TYPESAFE_API_KEY": SECRET, "OPENAI_API_KEY": "old"}, path=path)

    assert _read_env(path) == {"TYPESAFE_API_KEY": SECRET}


@pytest.mark.parametrize("value", ["", "   ", "two\nlines"])
def test_invalid_key_preserves_existing_file(tmp_path: Path, value: str) -> None:
    module = importlib.import_module("barabot.domains.processor.configure")
    path = tmp_path / "config" / "processor.env"
    path.parent.mkdir()
    original = b"existing processor configuration\n"
    path.write_bytes(original)

    with pytest.raises(module.ConfigurationError, match="TYPESAFE_API_KEY"):
        module.configure_from_mapping({"TYPESAFE_API_KEY": value}, path=path)

    assert path.read_bytes() == original


def test_atomic_replace_failure_preserves_processor_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("barabot.domains.processor.configure")
    path = tmp_path / "config/processor.env"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing processor configuration\n")
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated replace failure")),
    )

    with pytest.raises(OSError):
        module.configure_from_mapping({"TYPESAFE_API_KEY": SECRET}, path=path)

    assert path.read_bytes() == b"existing processor configuration\n"
    assert [item.name for item in path.parent.iterdir()] == ["processor.env"]
