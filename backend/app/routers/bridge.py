"""桥梁定检接口：维护检测记录，并承载集中签发链（现场复核 → 签发 → 限载投影）。

链路的业务判断都在 services 层，路由只负责解析参数、把可读的失败原因返回给页面。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.bridge import BridgeService

router = APIRouter(prefix="/api/bridge", tags=["桥梁定检"])

service = BridgeService()

LIST_FIELDS = ["检测编号", "桥梁名称", "检测类型", "检测日期", "技术状况评分", "主要病害", "检测单位", "检测状态"]
STATUSES = ["待复核", "已复核", "已签发", "已限载"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按检测编号检索"),
    status: str | None = Query(default=None, description="待复核、已复核、已签发、已限载"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按检测编号与签发状态过滤桥梁定检列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/issuance/batches", response_model=ActionResult)
def issue_batch(payload: EntryPayload) -> ActionResult:
    """集中签发：整批一个事务，成功只保留一个版本，失败整体回滚不留半批限载。

    批次号已存在时按断点续做处理，直接从上次失败的断点继续。
    """
    batch_id = str(payload.values.get("batch_id") or "").strip() or None
    raw_codes = payload.values.get("检测编号") or []
    codes = [str(code).strip() for code in raw_codes if str(code).strip()] if isinstance(raw_codes, list) else []
    journal, message = service.issue_batch(batch_id, codes)
    if journal is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=journal)


@router.get("/issuance/batches/{batch_id}", response_model=dict)
def get_batch(batch_id: str) -> dict:
    """查看集中签发批次的断点日志：每项的状态与失败原因都在里面。"""
    journal = service.get_batch(batch_id)
    if journal is None:
        raise HTTPException(status_code=404, detail=f"签发批次 {batch_id} 不存在")
    return journal


@router.post("/issuance/batches/{batch_id}/resume", response_model=ActionResult)
def resume_batch(batch_id: str) -> ActionResult:
    """断点续做：从上次失败的断点继续签发，已生效的编号自动跳过、不重复建任务。"""
    journal, message = service.resume_batch(batch_id)
    if journal is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=journal)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出桥梁定检清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "bridge", "total": total, "items": items}


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
    """对单条检测记录执行开始检测、完成评定、归档报告；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/review", response_model=ActionResult)
def review_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """现场复核：每次复核生成一个新的评分版本，历史等级按检测时版本保留。"""
    entry, message = service.review_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/issue", response_model=ActionResult)
def issue_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """签发：现场复核后才能签发；结论同事务回写检测清单、工程待办和限载页面。

    可带上 expect_version 做并发校验：与服务端最后一次现场复核不一致时拒绝，
    保证并发签发按检测编号只生效一版。
    """
    raw_version = payload.values.get("expect_version")
    expect_version = int(raw_version) if raw_version not in (None, "") else None
    entry, message = service.issue_entry(entry_id, expect_version=expect_version)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/project-load", response_model=ActionResult)
def project_load(entry_id: int) -> ActionResult:
    """限载投影：未签发不得生成限载；已限载的编号幂等返回，不重复投影。"""
    entry, message = service.project_load(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
