"""诊断数据查询路由：显式开关 + 令牌权限 + 容量/数量受限输出。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query

from .config import get_config
from .diagnostics import get_store
from .security import extract_token, is_authorized

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


def _require_access(
    enabled: bool,
    authorization: str,
    x_diagnostics_token: str,
) -> None:
    # 关闭时统一表现为 404，不暴露"这里有一套诊断接口"
    if not enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    token = extract_token(authorization, x_diagnostics_token)
    if not is_authorized(token, get_config().admin_token):
        raise HTTPException(status_code=403, detail="forbidden")


@router.get("/records")
async def list_records(
    authorization: str = Header(default=""),
    x_diagnostics_token: str = Header(default=""),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """列出最近的诊断记录；需要管理员令牌，且仅包含已采样请求。"""
    config = get_config()
    _require_access(config.enabled, authorization, x_diagnostics_token)
    records = get_store().list_recent(limit=limit)
    return {
        "enabled": config.enabled,
        "sample_rate": config.sample_rate,
        "count": len(records),
        "records": [r.to_dict() for r in records],
    }


@router.get("/records/{request_id}")
async def get_record(
    request_id: str,
    authorization: str = Header(default=""),
    x_diagnostics_token: str = Header(default=""),
) -> dict:
    config = get_config()
    _require_access(config.enabled, authorization, x_diagnostics_token)
    record = get_store().get(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="record not found")
    return record.to_dict()
