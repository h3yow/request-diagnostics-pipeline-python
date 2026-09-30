"""诊断查询的权限判定。

* 支持 ``Authorization: Bearer <token>`` 与 ``X-Diagnostics-Token: <token>``
  两种携带方式，前者优先；
* 使用 :func:`hmac.compare_digest` 做常量时间比较；
* 服务端令牌为空（未配置）时一律拒绝，避免"空令牌即放行"。
"""
from __future__ import annotations

import hmac


def extract_token(authorization: str = "", x_diagnostics_token: str = "") -> str:
    """从 Authorization 头或 X-Diagnostics-Token 头提取令牌。"""
    auth = (authorization or "").strip()
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        # 也容忍直接把令牌放进 Authorization 头的情况
        if len(parts) == 1:
            return parts[0].strip()
    return (x_diagnostics_token or "").strip()


def is_authorized(token: str, admin_token: str) -> bool:
    """常量时间比较；配置令牌为空或调用方未带令牌都拒绝。"""
    if not admin_token or not token:
        return False
    return hmac.compare_digest(token, admin_token)
