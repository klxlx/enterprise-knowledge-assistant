"""登录与鉴权。

- 密码用 pbkdf2 散列存储，库里不保存明文
- 登录成功后发一个随机 token，存在 auth_tokens 表
- 接口用 `Authorization: Bearer <token>` 识别当前用户
- 没有登录时按最低权限（新人）处理，只能看全员可见的资料
"""

import hashlib
import hmac
import secrets

from fastapi import Depends, Header, HTTPException

import database
from database import ROLE_NEWCOMER, ROLE_STAFF

PBKDF2_ROUNDS = 120_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, expected = stored.split("$")
        rounds = int(rounds)
    except (ValueError, AttributeError):
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds
    )
    return hmac.compare_digest(digest.hex(), expected)


def login(username: str, password: str) -> tuple[str, dict] | None:
    """校验账号密码，成功则签发 token，返回（token, 用户信息）。"""
    user = database.get_user(username)
    if user is None or not verify_password(password, user["password_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    database.save_token(token, username)
    public = {key: value for key, value in user.items() if key != "password_hash"}
    return token, public


def optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    """解析 Bearer token；没有或无效就返回 None（调用方按最低权限处理）。"""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return database.get_token_user(token.strip())


def logout(token: str) -> None:
    """退出登录：把令牌从库里删掉。"""
    if token:
        database.delete_token(token)


def require_user(user: dict | None = Depends(optional_user)) -> dict:
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def require_staff(user: dict | None = Depends(optional_user)) -> dict:
    """员工专属操作：更新知识库、看盲区清单、查审计日志。"""
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if user["role"] != ROLE_STAFF:
        raise HTTPException(status_code=403, detail="只有员工可以执行这个操作")
    return user


def role_of(user: dict | None) -> str:
    """没有登录时按新人处理——权限默认最小。"""
    return user["role"] if user else ROLE_NEWCOMER
