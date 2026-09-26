"""Request/response shapes for the judge-facing API.

Kept deliberately loose on the payload side: the judge is free to push context
objects whose inner shape we haven't seen, and rejecting those with a 422 would
cost us more than tolerating an unknown field.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ContextPush(BaseModel):
    scope: str
    context_id: str
    version: int = 1
    payload: Dict[str, Any] = Field(default_factory=dict)
    delivered_at: Optional[str] = None


class TickRequest(BaseModel):
    now: Optional[str] = None
    available_triggers: List[str] = Field(default_factory=list)


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str = "merchant"
    message: str = ""
    received_at: Optional[str] = None
    turn_number: int = 1


class Action(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    send_as: str = "vera"
    trigger_id: str
    template_name: str
    template_params: List[str] = Field(default_factory=list)
    body: str
    cta: str
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: List[Action] = Field(default_factory=list)
