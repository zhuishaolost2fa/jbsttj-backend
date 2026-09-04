"""鉴权辅助接口模型（调试与轻量前端用）。"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

Gender = Literal["male", "female", "other"]

_BIRTHDAY_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class WechatLoginRequest(BaseModel):
    """微信小程序登录：wx.login() 拿到的 code。

    nickname / avatar_url 可选 —— 只在首次创建资料时写入，已存在则不覆盖，
    避免用户改过昵称后每次登录都被重置回微信昵称。
    """

    code: str = Field(min_length=1, max_length=256)
    nickname: Optional[str] = Field(default=None, max_length=30)
    avatar_url: Optional[str] = Field(default=None, max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: Optional[int] = None
    user: Optional[Dict[str, Any]] = None


class ProfileResponse(BaseModel):
    """当前登录用户的完整资料，GET /auth/me 与 PATCH /auth/me 均返回它。"""

    id: str
    email: Optional[str] = None
    role: str
    is_service: bool = False
    email_verified: bool = False
    # 登录来源：None=邮箱注册；'wechat'=微信登录。
    # 前端据此隐藏「修改密码 / 修改邮箱」—— 微信用户没有真邮箱，改了也没意义。
    provider: Optional[str] = None
    nickname: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_color: int = 0
    bio: Optional[str] = None
    gender: Optional[Gender] = None
    birthday: Optional[str] = None
    region: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ProfileUpdate(BaseModel):
    """编辑个人资料：所有字段均可选，只传需要修改的字段（部分更新）。

    未传的字段保持原值；显式传 null 视为「清空该字段」（与 PATCH 语义一致）。
    """

    nickname: Optional[str] = Field(default=None, min_length=1, max_length=30)
    avatar_url: Optional[str] = Field(default=None, max_length=1024)
    avatar_color: Optional[int] = Field(default=None, ge=0, le=7)
    bio: Optional[str] = Field(default=None, max_length=120)
    gender: Optional[Gender] = None
    birthday: Optional[str] = None
    region: Optional[str] = Field(default=None, max_length=50)

    @field_validator("avatar_url")
    @classmethod
    def _check_avatar_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        # 允许 http(s) 远程图片；空串视为清空
        if v == "":
            return None
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("头像地址必须是 http(s) 开头的图片链接")
        return v

    @field_validator("birthday")
    @classmethod
    def _check_birthday(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        import datetime

        if not _BIRTHDAY_RE.match(v):
            raise ValueError("生日格式应为 YYYY-MM-DD")
        try:
            dt = datetime.date.fromisoformat(v)
        except ValueError:
            raise ValueError("生日不是有效日期")
        if dt > datetime.date.today():
            raise ValueError("生日不能晚于今天")
        if dt.year < 1900:
            raise ValueError("生日年份过早")
        return v


class ChangePasswordRequest(BaseModel):
    """修改登录密码：先用当前密码校验身份，再下发新密码。"""

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class ChangeEmailRequest(BaseModel):
    """修改登录邮箱：校验当前密码后，向新邮箱发送验证邮件。"""

    current_password: str = Field(min_length=1, max_length=128)
    new_email: EmailStr


class MessageResponse(BaseModel):
    """通用操作结果提示（改密 / 改邮箱等）。"""

    message: str
