"""Prompt-injection guards: toolset allowlist and the inline-shell pin."""

from __future__ import annotations

import pytest
import yaml

from agents.security import (
    SAFE_TOOLSETS,
    UnsafeConfiguration,
    check_toolsets,
    harden_hermes_config,
)


@pytest.fixture(autouse=True)
def _no_opt_in(monkeypatch):
    monkeypatch.delenv("ALLOW_UNSAFE_TOOLSETS", raising=False)


# --- toolset allowlist -----------------------------------------------------

def test_default_toolsets_pass():
    check_toolsets(["memory", "session_search", "skills", "todo", "datacommons"])


@pytest.mark.parametrize(
    "toolset",
    # Direct ones, bundles that include them, and a name nobody listed.
    ["terminal", "file", "code_execution", "browser", "web", "delegation",
     "coding", "debugging", "safe", "some_plugin"],
)
def test_anything_outside_the_allowlist_is_refused(toolset):
    with pytest.raises(UnsafeConfiguration, match=toolset):
        check_toolsets(["memory", toolset])


def test_explicit_opt_in_allows_it(monkeypatch):
    monkeypatch.setenv("ALLOW_UNSAFE_TOOLSETS", "true")
    check_toolsets(["terminal"])


def test_allowlist_has_no_command_or_file_tools():
    assert not SAFE_TOOLSETS & {"terminal", "file", "code_execution", "browser", "web"}


# --- inline shell pin ------------------------------------------------------

def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pins_when_missing(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: x\n", encoding="utf-8")
    assert harden_hermes_config(cfg) is True
    data = _load(cfg)
    assert data["skills"]["inline_shell"] is False
    assert data["model"]["default"] == "x"


def test_turns_it_off_when_enabled(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("skills:\n  inline_shell: true\n  inline_shell_timeout: 5\n", encoding="utf-8")
    assert harden_hermes_config(cfg) is True
    assert _load(cfg)["skills"] == {"inline_shell": False, "inline_shell_timeout": 5}


def test_already_pinned_file_is_not_rewritten(tmp_path):
    cfg = tmp_path / "config.yaml"
    text = "# operator comment\nskills:\n  inline_shell: false\n"
    cfg.write_text(text, encoding="utf-8")
    assert harden_hermes_config(cfg) is False
    assert cfg.read_text(encoding="utf-8") == text


def test_unparsable_file_is_left_alone(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("skills: [unclosed\n", encoding="utf-8")
    assert harden_hermes_config(cfg) is False
    assert cfg.read_text(encoding="utf-8") == "skills: [unclosed\n"


def test_shipped_template_is_already_pinned():
    from pathlib import Path

    template = Path(__file__).resolve().parent.parent / "config" / "hermes_config.yaml"
    assert _load(template)["skills"]["inline_shell"] is False


def test_existing_profile_gets_pinned_on_open(tmp_path, monkeypatch):
    from agents.user_profiles import reset_seed_cache, resolve_profile

    monkeypatch.setenv("HERMES_USERS_HOME", str(tmp_path / "users"))
    reset_seed_cache()
    # A profile created before the guard existed, with inline shell on.
    home = tmp_path / "users" / "aziza"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("skills:\n  inline_shell: true\n", encoding="utf-8")

    profile = resolve_profile("aziza")
    assert _load(profile.home / "config.yaml")["skills"]["inline_shell"] is False
    reset_seed_cache()


# --- startup ---------------------------------------------------------------

def test_app_refuses_to_start_with_terminal(monkeypatch):
    from app.api import create_app

    monkeypatch.setenv("HERMES_ENABLED_TOOLSETS", "memory,terminal")
    with pytest.raises(UnsafeConfiguration):
        create_app()


def test_host_is_not_ready_with_terminal(monkeypatch):
    from agents.hermes_host import HermesHostService

    monkeypatch.setenv("HERMES_ENABLED_TOOLSETS", "terminal")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "m")
    host = HermesHostService()
    state = host.initialize()
    assert state["ready"] is False
    assert "terminal" in state["error"]
