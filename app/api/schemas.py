"""Pydantic 请求/响应模型。"""

from typing import Literal

from pydantic import BaseModel, Field


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=500_000)
    source: str = Field(default="", max_length=256)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    mode: str = "hybrid"  # dense | bm25 | hybrid
    top_k: int = Field(default=5, ge=1, le=20)


class SessionIn(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=200)


class ChatIn(BaseModel):
    request_id: str | None = Field(default=None, min_length=8, max_length=80)
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=8000)
    workflow: Literal["auto", "consult", "diagnose", "review", "history", "chat"] = "auto"
    background: bool = False


class DecisionIn(BaseModel):
    approved: bool
    comment: str = Field(default="", max_length=600)


class FaultIn(BaseModel):
    service: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    error_code: str = Field(default="", max_length=64)
    symptom: str = Field(default="", max_length=2000)
    root_cause: str = Field(default="", max_length=2000)
    resolution: str = Field(default="", max_length=4000)
    severity: str = Field(default="P2", max_length=8)
