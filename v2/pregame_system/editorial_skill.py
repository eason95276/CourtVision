"""Load editable runtime editorial guidance for the NBA Agent."""
from __future__ import annotations

import os
from pathlib import Path


EDITORIAL_SKILL_DIR = Path(__file__).resolve().parents[2] / "agent" / "skills" / "editorial"


def load_editorial_skill() -> str:
    """Read the current skill and any explicitly enabled example files."""
    skill_path = EDITORIAL_SKILL_DIR / "SKILL.md"
    skill = skill_path.read_text(encoding="utf-8").strip()
    if not skill:
        raise ValueError(f"NBA editorial skill is empty: {skill_path}")

    sections = [f"<editorial_skill source=\"{skill_path.as_posix()}\">\n{skill}\n</editorial_skill>"]
    requested = {
        item.strip().lower()
        for item in os.environ.get("NBA_EDITORIAL_EXAMPLES", "good").split(",")
        if item.strip()
    }
    for name, filename in (
        ("good", "good_examples.md"),
        ("bad", "bad_examples.md"),
    ):
        if name not in requested:
            continue
        example_path = EDITORIAL_SKILL_DIR / filename
        examples = example_path.read_text(encoding="utf-8").strip()
        if examples:
            sections.append(
                f"<editorial_{name}_examples source=\"{example_path.as_posix()}\">\n"
                f"{examples}\n</editorial_{name}_examples>"
            )
    unknown = requested - {"good", "bad"}
    if unknown:
        raise ValueError(
            "Unsupported NBA_EDITORIAL_EXAMPLES value(s): "
            + ", ".join(sorted(unknown))
        )
    return "\n\n".join(sections)
