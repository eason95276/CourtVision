"""Extract hashes for the runtime Skills actually embedded in a prompt."""
from __future__ import annotations

import hashlib
import re


_SKILLS = ("pregame_research", "forecasting", "editorial")


def prompt_skill_hashes(system_message: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for skill in _SKILLS:
        match = re.search(
            rf'<{skill}_skill\b[^>]*>\s*(.*?)\s*</{skill}_skill>',
            system_message,
            flags=re.DOTALL,
        )
        if match:
            hashes[skill] = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()
    return hashes
