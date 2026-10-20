from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    key: str = ""


class CreateInstanceRequest(BaseModel):
    owner_label: str = Field(min_length=1, max_length=255)
    host_id: int
    note: str = ""


class ReleaseRequest(BaseModel):
    confirm: str = ""
