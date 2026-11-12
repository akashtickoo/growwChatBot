"""The answer step's output schema (architecture §6.6).

Strict mode needs every property in `required` and `additionalProperties: false`. "No source"
is an empty string rather than null, which keeps the schema to plain types.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

Status = Literal["answered", "insufficient_context", "advisory", "out_of_scope"]

ANSWER_SCHEMA_NAME = "faq_answer"
ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["answered", "insufficient_context", "advisory", "out_of_scope"],
        },
        "answer": {"type": "string"},
        "source_id": {"type": "string"},
    },
    "required": ["status", "answer", "source_id"],
    "additionalProperties": False,
}


class LLMAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Status
    answer: str
    source_id: str  # "" when there is no source
