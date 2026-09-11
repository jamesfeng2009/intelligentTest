"""认证与 RBAC 权限矩阵（T35：4 角色权限生效）。

角色：admin / qa / developer / viewer
- 演示模式：内置账号 admin/123456 等，token 为内存签发
- 生产模式：预留对接 SSO/JWT 的位置
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Callable

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_session

# 内置账号（演示）：username → (password, role)
DEMO_USERS = {
    "admin": ("123456", "admin"),
    "qa": ("123456", "qa"),
    "developer": ("123456", "developer"),
    "viewer": ("123456", "viewer"),
}

# 角色 → 可访问的模块（模块: 读/写）
ROLE_MATRIX: dict[str, dict[str, str]] = {
    "admin": {"*": "rw"},
    "qa": {"project": "rw", "repo": "rw", "task": "rw", "report": "rw", "knowledge": "rw",
           "eval": "rw", "metric": "r", "ci": "r", "agent": "r", "system": "r"},
    "developer": {"project": "r", "repo": "r", "task": "rw", "report": "r", "knowledge": "r",
                  "eval": "r", "metric": "r", "ci": "r", "agent": "r", "system": "r"},
    "viewer": {"project": "r", "repo": "r", "task": "r", "report": "r", "knowledge": "r",
               "eval": "r", "metric": "r", "ci": "r", "agent": "r", "system": "r"},
}

_TOKENS: dict[str, dict] = {}  # token → {username, role, exp}


def issue_token(username: str) -> str:
    token = secrets.token_urlsafe(24)
    _TOKENS[token] = {"username": username, "role": DEMO_USERS[username][1], "exp": time.time() + 24 * 3600}
    return token


def authenticate(username: str, password: str) -> dict | None:
    user = DEMO_USERS.get(username)
    if user and hmac.compare_digest(user[0], password):
        return {"username": username, "role": user[1], "token": issue_token(username)}
    return None


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    token = authorization.removeprefix("Bearer ").strip()
    info = _TOKENS.get(token)
    if info is None or info["exp"] < time.time():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期")
    return {"username": info["username"], "role": info["role"]}


def require_role(module: str, access: str = "r"):
    def _dep(user: dict = Depends(get_current_user)) -> dict:
        role = user["role"]
        allowed = ROLE_MATRIX.get(role, {})
        perm = allowed.get(module) or allowed.get("*")
        if perm is None or access not in perm:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail=f"{role} 无 {module}:{access} 权限")
        return user
    return _dep


def role_matrix() -> dict:
    return {role: {m: p for m, p in perms.items() if m != "*"} for role, perms in ROLE_MATRIX.items()}
