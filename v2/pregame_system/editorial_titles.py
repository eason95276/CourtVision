"""Presentation-only title rules; no forecast or frozen-version migration."""
from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field


HAN = r"[\u3400-\u4dbf\u4e00-\u9fff〇]"
COUPLET = re.compile(rf"^({HAN}{{7}})\s*[/／，,；;。！？!?]?\s*({HAN}{{7}})$")


class TitleRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=80,
        description="Two seven-character Chinese lines, separated by one newline; no punctuation.")


def normalize_poem_title(value: str) -> str | None:
    """Normalize separators only. Never truncate, pad, rename, or invent words."""
    if not isinstance(value, str):
        return None
    match = COUPLET.fullmatch(unicodedata.normalize("NFC", value).strip())
    return "\n".join(match.groups()) if match else None
