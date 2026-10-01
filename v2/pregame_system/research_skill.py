"""Load the editable runtime research guidance for the web pregame researcher."""
from pathlib import Path


PREGAME_RESEARCH_SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "agent"
    / "skills"
    / "pregame_research"
    / "SKILL.md"
)


def load_pregame_research_skill() -> str:
    """Read SKILL.md on every call so edits take effect without code changes."""
    skill = PREGAME_RESEARCH_SKILL_PATH.read_text(encoding="utf-8").strip()
    if not skill:
        raise ValueError(f"NBA pregame research skill is empty: {PREGAME_RESEARCH_SKILL_PATH}")
    return (
        f'<pregame_research_skill source="{PREGAME_RESEARCH_SKILL_PATH.as_posix()}">\n'
        f"{skill}\n"
        "</pregame_research_skill>"
    )
