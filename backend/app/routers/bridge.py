"""桥梁定检接口：维护检测记录，覆盖开始检测、完成评定、现场复核、签发结论、归档报告。

签发结论沿「现场复核 → 签发 → 限载投影」单向推进：跳级、未复核签发、
重复签发都由服务端拒绝；分组提交整批一个事务，失败不留下半批限载。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.bridge import BridgeService

router = APIRouter(prefix="/api/bridge", tags=["桥梁定检"])

service = BridgeService()

LIST_FIELDS = ["检测编号", "桥梁名称", "检测类型", "检测日期", "技术状况评分", "技术状况等级", "签发状态", "评分版本", "检测状态"]
STATUSES = ["待检测", "检测中", "已评定", "已复核", "已签发", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按检测编号检索"),
    status: str | None = Query(default=None, description="待检测、检测中、已评定、已复核、已签发、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按检测编号与状态过滤桥梁定检列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出桥梁定检清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "bridge", "total": total, "items": items}


@router.post("/issue-batch", response_model=ActionResult)
def issue_batch(payload: EntryPayload) -> ActionResult:
    """分组提交集中签发：整批一个事务，断点续做跳过已签发项，失败整体回滚。"""
    batch_id = str(payload.values.get("batch_id") or "").strip()
    raw_ids = payload.values.get("ids") or []
    try:
        ids = [int(item) for item in raw_ids]
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="分组提交的检测记录 id 不是有效数字")
    batch, message = service.issue_batch(batch_id, ids)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/issue-batch/{batch_id}")
def batch_status(batch_id: str) -> dict[str, Any]:
    """查询批次进度：断点续做时据此判断哪些检测编号已签发。"""
    batch = service.batch_status(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"签发批次 {batch_id} 不存在或已回滚")
    return batch


@router.post("/migrate", response_model=ActionResult)
def migrate_legacy() -> ActionResult:
    """存量迁移：把未签发的检测编号按采集顺序回填到签发链正确节点。"""
    count = service.migrate_legacy()
    return ActionResult(ok=True, message=f"存量检测迁移完成，共回填 {count} 条到正确节点")


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条检测记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"检测记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条检测记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="检测记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条检测记录执行动作；跳级、未复核签发、重复签发都会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
