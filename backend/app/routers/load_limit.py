"""限载投影接口：只读查询，投影只能由桥梁定检签发回写产生。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import PageResult
from app.services.load_limit import LoadLimitService

router = APIRouter(prefix="/api/load_limit", tags=["限载管理"])

service = LoadLimitService()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按检测编号或桥梁名称检索"),
    status: str | None = Query(default=None, description="生效中、已失效；缺省只看生效中"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """限载投影列表：默认只显示每个检测编号当前生效的一版，不重复显示旧评分。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出限载投影清单：返回当前生效的全量投影。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "load_limit", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条限载投影明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"限载投影 {entry_id} 不存在")
    return entry
