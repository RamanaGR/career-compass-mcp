"""Local Ollama assistant with Two-Mode routing, strict Closed-World Grounding, and Natural Writing Style."""

from compass_mcp.llm.assistant import (
    ABSTENTION_REASONING,
    BANNED_CLICHES,
    OllamaAssistant,
    sanitize_natural_engineer_prose,
)

__all__ = [
    "ABSTENTION_REASONING",
    "BANNED_CLICHES",
    "OllamaAssistant",
    "sanitize_natural_engineer_prose",
]
