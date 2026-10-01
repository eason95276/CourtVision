"""Load editable runtime forecasting guidance for the NBA Analyst."""
from pathlib import Path


FORECASTING_SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / "agent"
    / "skills"
    / "forecasting"
    / "SKILL.md"
)


def load_forecasting_skill() -> str:
    """Read SKILL.md on every writer call so edits take effect immediately."""
    skill = FORECASTING_SKILL_PATH.read_text(encoding="utf-8").strip()
    if not skill:
        raise ValueError(f"NBA forecasting skill is empty: {FORECASTING_SKILL_PATH}")
    return (
        f'<forecasting_skill applies_to="final_prediction" source="{FORECASTING_SKILL_PATH.as_posix()}">\n'
        f"{skill}\n"
        "</forecasting_skill>"
    )
