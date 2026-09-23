"""
Example host tool — template for wiring your own tools into the Hermes host.

Copy this file, rename the function, write a real docstring (the LLM reads it
to decide when to call the tool), and add it to
`HermesHostService._host_langchain_tools()`.
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

logger = logging.getLogger("example_tool")


@tool
def echo(text: str) -> str:
    """Return the given text unchanged.

    A no-op tool that proves the host's tool-calling loop works. Use it only
    when the user explicitly asks to echo or repeat something verbatim.
    """
    logger.info("echo tool called text_len=%d", len(text or ""))
    return text or ""


def as_langchain_tools() -> list:
    """Tools this module contributes to the host agent."""
    return [echo]
