"""Pydantic 请求/响应模型。"""

from typing import Literal

from pydantic import BaseModel, Field


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    source: str = ""


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    mode: str = "hybrid"  # dense | bm25 | hybrid
    top_k: int = Field(default=5, ge=1, le=20)


class SessionIn(BaseModel):
    title: str = "新会话"


class ChatIn(BaseModel):
    request_id: str | None = Field(default=None, min_length=8, max_length=80)
    session_id: str
    message: str = Field(min_length=1)
    workflow: Literal["auto", "consult", "diagnose", "review", "history"] = "auto"


class DecisionIn(BaseModel):
    approved: bool
    comment: str = ""


class FaultIn(BaseModel):
    service: str
    title: str
    error_code: str = ""
    symptom: str = ""
    root_cause: str = ""
    resolution: str = ""
    severity: str = "P2"
