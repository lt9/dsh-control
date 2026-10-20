from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    key: str = ""


class CreateInstanceRequest(BaseModel):
    """§4：请求体可为空，或只带 host_id（新建确认页一次点击换主机）。
    任何情况下不接受用户传入的名字——slug 与端口一律服务端生成（§6.1.1）。"""

    host_id: int | None = None


class UpdateInstanceRequest(BaseModel):
    """§6.1.1 铅笔就地编辑：只允许改备注名与备注。"""

    owner_label: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=4000)


class ReleaseRequest(BaseModel):
    confirm: str = ""
