"""Presentation-only title rules; no forecast or frozen-version migration."""
from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field,model_validator


HAN = r"[\u3400-\u4dbf\u4e00-\u9fff〇]"
COUPLET = re.compile(rf"^({HAN}{{7}})\s*[/／，,；;。！？!?]?\s*({HAN}{{7}})$")


class TitleRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str|None = Field(default=None,min_length=1, max_length=80,
        description="Two seven-character Chinese lines, separated by one newline; no punctuation.")
    upper:list[str]|None=Field(default=None,min_length=7,max_length=7,description='上句，恰好七项，每项一个汉字。')
    lower:list[str]|None=Field(default=None,min_length=7,max_length=7,description='下句，恰好七项，每项一个汉字。')

    @model_validator(mode='after')
    def exact_character_arrays(self):
        if self.title is not None:
            if self.upper is not None or self.lower is not None:raise ValueError('title_revision_mixed_formats')
        elif self.upper is None or self.lower is None or not all(re.fullmatch(HAN,c)for c in self.upper+self.lower):
            raise ValueError('title_revision_requires_two_seven_han_arrays')
        return self

    def rendered_title(self):
        return self.title if self.title is not None else ''.join(self.upper)+'\n'+''.join(self.lower)


def normalize_poem_title(value: str) -> str | None:
    """Normalize separators only. Never truncate, pad, rename, or invent words."""
    if not isinstance(value, str):
        return None
    match = COUPLET.fullmatch(unicodedata.normalize("NFC", value).strip())
    return "\n".join(match.groups()) if match else None
