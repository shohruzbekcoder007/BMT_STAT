"""
Guards that keep a prompt injection from turning into code execution.

Whatever the model reads -- a user message, a Data Commons result, a memory
it wrote last week -- can steer which tools it calls. So the boundary is not
the prompt but the tool set: the host agent gets no tool that runs commands
or touches the filesystem unless an operator says so out loud.

Two guards:

* `check_toolsets` -- an allowlist, not a denylist. Hermes has bundles
  (`coding`, `debugging`, `safe`, …) that pull in `terminal` indirectly, and
  plugins add toolsets under any name; a denylist would miss those.
* `harden_hermes_config` -- pins `skills.inline_shell: false`. With it on,
  `skill_view` executes `` !`cmd` `` snippets inside a skill, and the
  `skills` toolset lets the agent write that skill itself: shell access in
  two tool calls. Hermes defaults it to off; this makes the default a fact
  in every profile's config.yaml, including profiles created before this
  guard existed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

import yaml

logger = logging.getLogger("security")

# Toolsets that only read or write the agent's own state, or call a fixed
# read-only API. Nothing here runs a command, reads a file outside the
# profile, or reaches an arbitrary URL.
SAFE_TOOLSETS = frozenset({
    "memory",
    "session_search",
    "skills",
    "todo",
    "clarify",
    "datacommons",
})

OPT_IN_VAR = "ALLOW_UNSAFE_TOOLSETS"


class UnsafeConfiguration(RuntimeError):
    """The service refuses to start with this configuration."""


def _opted_in() -> bool:
    return os.getenv(OPT_IN_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def unsafe_toolsets(toolsets: Iterable[str]) -> list[str]:
    return sorted({t for t in toolsets if t not in SAFE_TOOLSETS})


def check_toolsets(toolsets: Iterable[str]) -> None:
    """Refuse toolsets outside the allowlist unless the operator opted in."""
    unsafe = unsafe_toolsets(toolsets)
    if not unsafe:
        return
    if _opted_in():
        logger.warning(
            "%s=true: host agent runs with %s. A prompt injection can now use "
            "them -- keep GATEWAY_TOKEN set and the port off public networks.",
            OPT_IN_VAR,
            ", ".join(unsafe),
        )
        return
    raise UnsafeConfiguration(
        f"HERMES_ENABLED_TOOLSETS contains {', '.join(unsafe)}, which is not in "
        f"the safe list ({', '.join(sorted(SAFE_TOOLSETS))}). Such toolsets let a "
        f"prompt injection run commands or read files. Remove them, or set "
        f"{OPT_IN_VAR}=true if this is deliberate."
    )


def harden_hermes_config(path: Path) -> bool:
    """
    Force `skills.inline_shell: false` in a Hermes config.yaml.

    Returns True when the file was changed. A missing file is left alone
    (Hermes' own default is off); an unparsable one is left alone and logged,
    since rewriting it would destroy whatever the operator put there.
    """
    if not path.is_file():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("cannot read %s to pin skills.inline_shell: %s", path, exc)
        return False
    if not isinstance(data, dict):
        logger.error("%s is not a mapping; skills.inline_shell not pinned", path)
        return False

    skills = data.get("skills")
    if not isinstance(skills, dict):
        skills = {}
        data["skills"] = skills
    if skills.get("inline_shell") is False:
        return False

    was = skills.get("inline_shell")
    skills["inline_shell"] = False
    try:
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    except OSError as exc:
        logger.error("cannot write %s to pin skills.inline_shell: %s", path, exc)
        return False
    logger.warning("pinned skills.inline_shell=false in %s (was %r)", path, was)
    return True
