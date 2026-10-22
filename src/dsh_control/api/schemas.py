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


class PurgeRequest(BaseModel):
    """§4 POST /instances/{id}/purge：confirm 由前端填（长按确认），逐字等于 slug。"""

    confirm: str = ""


class DeployRequest(BaseModel):
    """§4 POST /instances/{id}/deploy 的可选 body：留空 = 站点当前目标 tag。"""

    image: str | None = Field(default=None, max_length=255)


class ImageShipSettings(BaseModel):
    """PUT /api/settings 的 image_ship 段（§6.1.5/§6.1.6）：字段留 None = 不动该项。"""

    mode: str | None = None              # ssh | download（其余 422）
    base_url: str | None = Field(default=None, max_length=512)
    mirror_order: list[str] | None = None    # gitee/github/console 的排列/子集（§6.1.6）
    auto_mirror: bool | None = None          # 发布是否顺道推外部渠道，默认 true


class MirrorRepoPatch(BaseModel):
    """PUT /api/settings 的 mirrors.{channel} 段：现在只有 repo 一个可写键。"""

    repo: str | None = Field(default=None, max_length=255)   # 形如 owner/repo


class PutSettingsRequest(BaseModel):
    """局部更新：只认已知键，别的键忽略不报错（§4 PUT /api/settings）。"""

    image_ship: ImageShipSettings | None = None
    mirrors: dict[str, MirrorRepoPatch] | None = None   # 只认 github/gitee 两个键


class MirrorTokenRequest(BaseModel):
    """PUT /api/mirror-tokens/{channel}（§6.1.6）：只写不回显，响应永不含 token。"""

    token: str = Field(min_length=1, max_length=4096)


class PublishImageRequest(BaseModel):
    """§4 POST /api/images：从 from_host_id 那台机 save 该 tag 发布成制品。"""

    tag: str = Field(max_length=255)
    from_host_id: int
