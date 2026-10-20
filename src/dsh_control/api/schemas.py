from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    key: str = ""


class CreateInstanceRequest(BaseModel):
    """§4：名字（slug）与端口一律服务端生成（§6.1.1），用户不能指定。

    confirm 不是"用户给的名字"，而是**回显**：确认卡先 GET /instances/next-slug
    问清"下一台叫什么"，创建时把这个名字打回来。对不上就一个对象都不建。
    前端那道打字确认被真双击穿透过一次（2026-10-21 现网凭空开出 dsh-03），
    所以这道确认必须落在服务端。"""

    host_id: int | None = None
    confirm: str | None = None


class UpdateInstanceRequest(BaseModel):
    """§6.1.1 铅笔就地编辑：只允许改备注名与备注。"""

    owner_label: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=4000)


class ReleaseRequest(BaseModel):
    confirm: str = ""
