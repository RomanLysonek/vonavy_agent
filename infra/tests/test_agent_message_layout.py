from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
STYLES = ROOT / "web/styles.css"


def _declarations(selector: str) -> list[str]:
    css = STYLES.read_text()
    pattern = re.compile(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}")
    return [match.group("body") for match in pattern.finditer(css)]


def _has_declaration(blocks: list[str], name: str, value: str) -> bool:
    expected = re.compile(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*{re.escape(value)}\s*(?:;|$)")
    return any(expected.search(block) for block in blocks)


def test_agent_messages_keep_natural_height_and_assistant_width() -> None:
    messages = _declarations(".agent-messages")
    assistants = _declarations(".agent-message.assistant")

    assert _has_declaration(messages, "display", "grid")
    assert _has_declaration(messages, "grid-auto-rows", "max-content")
    assert _has_declaration(messages, "align-content", "start")
    assert _has_declaration(messages, "overflow-y", "auto")

    assert _has_declaration(assistants, "width", "100%")
    assert _has_declaration(assistants, "max-width", "100%")
    assert _has_declaration(assistants, "min-width", "0")
    assert _has_declaration(assistants, "justify-self", "stretch")
