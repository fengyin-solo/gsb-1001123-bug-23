"""桥梁定检业务规则：签发链状态流转、结论回写与并发控制都收在这里。

定检结论沿「现场复核 → 签发 → 限载投影」单向推进，是一条单向修复链：
- 跳级由服务端拒绝，未现场复核不得签发，未签发不得生成限载；
- 签发把结论在同一事务里回写检测清单、工程待办与限载投影，任一失败整体回滚；
- 并发签发按检测编号串行，评分冲突以最后一次现场复核为准；
- 历史等级按检测时版本保留在「历史版本」里，限载投影只保留一个生效版本；
- 存量检测在启动迁移时按采集顺序回填到正确节点。
"""
from __future__ import annotations

import threading
from contextlib import ExitStack
from datetime import datetime
from typing import Any

from app.services.load_limit import LoadLimitService
from app.services.project import ProjectService
from app.store import store

MODULE = "bridge"
REQUIRED_FIELDS = ["检测编号", "桥梁名称", "检测类型"]
STATUS_ORDER = ["待检测", "检测中", "已评定", "已复核", "已签发", "已归档"]
ACTION_RULES = {
    "开始检测": "检测中",
    "完成评定": "已评定",
    "现场复核": "已复核",
    "签发结论": "已签发",
    "归档报告": "已归档",
}
NEGATIVE_ACTIONS: list[str] = []
CHAIN_LABEL = "→".join(STATUS_ORDER)

# 签发事务覆盖的四张表：检测清单、工程待办、限载投影、签发批次
TRANSACTION_MODULES = ("bridge", "project", "load_limit", "issue_batch")

project_service = ProjectService()
load_limit_service = LoadLimitService()


def now_text() -> str:
    # 毫秒精度：同一秒内的连续复核也能分出先后，冲突判定才靠得住
    return datetime.now().isoformat(timespec="milliseconds")


def grade_of(score: float) -> str:
    """技术状况等级：签发时按评分落到当期版本。"""
    if score >= 90:
        return "一类"
    if score >= 80:
        return "二类"
    if score >= 70:
        return "三类"
    if score >= 60:
        return "四类"
    return "五类"


def parse_score(raw: Any) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


class BridgeService:
    def __init__(self) -> None:
        # 并发签发按检测编号串行：每个编号一把锁
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, code: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(code, threading.Lock())

    def _present(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表/明细投影：把内部状态映射到清单字段，不动仓库里的原始行。"""
        item = dict(row)
        item["检测状态"] = row.get("status", "")
        return item

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("检测编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._present(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("检测日期", "技术状况评分", "主要病害", "检测单位"):
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 新记录落在签发链起点：未签发、零版本
        entry["签发状态"] = "未签发"
        entry["评分版本"] = 0
        entry["现场复核时间"] = None
        entry["复核评分"] = None
        entry["签发时间"] = None
        entry["签发复核时间"] = None
        entry["技术状况等级"] = ""
        entry["历史版本"] = []
        rows.append(entry)
        return self._present(entry), []

    def run_action(self, entry_id: int, action: str, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检测记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于桥梁定检可执行范围"
        if action == "现场复核":
            return self._review(entry, values)
        if action == "签发结论":
            return self.issue(entry_id, values)
        return self._advance(entry, action)

    def _advance(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any] | None, str]:
        """普通节点流转：只允许沿签发链逐级推进，跳级由服务端拒绝。"""
        target = ACTION_RULES[action]
        current = str(entry.get("status") or "")
        if current not in STATUS_ORDER:
            return None, f"检测记录当前状态「{current}」不在签发链上，请先执行存量迁移"
        if STATUS_ORDER.index(target) != STATUS_ORDER.index(current) + 1:
            return None, f"检测记录当前为「{current}」，不能跳级执行「{action}」，签发链必须按 {CHAIN_LABEL} 逐级推进"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self._present(entry), f"检测记录已{action}"

    def _review(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """现场复核：签发的前置节点。已复核未签发、已签发待复评时都允许复核，以最后一次为准。"""
        current = str(entry.get("status") or "")
        if current not in ("已评定", "已复核", "已签发"):
            return None, f"检测记录当前为「{current}」，完成评定后才能现场复核"
        score = parse_score(values.get("复核评分") or values.get("技术状况评分"))
        if score is None:
            return None, "现场复核必须录入有效的复核评分"
        entry["复核评分"] = score
        entry["现场复核时间"] = str(values.get("复核时间") or now_text())
        entry["status"] = "已复核"
        entry["pending"] = True
        return self._present(entry), "现场复核已登记，可签发结论"

    def issue(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """单条签发：按检测编号加锁，冲突以最后一次现场复核为准。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检测记录 {entry_id} 不存在或已归档"
        code = str(entry.get("检测编号") or entry_id)
        with self._lock_for(code):
            return self._issue_locked(entry, values, batch_id="")

    def _issue_locked(self, entry: dict[str, Any], values: dict[str, Any], batch_id: str) -> tuple[dict[str, Any] | None, str]:
        code = str(entry.get("检测编号") or "")
        if entry.get("status") == "已签发" and entry.get("签发复核时间") == entry.get("现场复核时间"):
            return None, f"检测编号 {code} 已按最后一次现场复核签发，重复签发已拦截"
        if entry.get("status") != "已复核":
            return None, f"检测记录当前为「{entry.get('status')}」，未现场复核不得签发"
        review_at = str(entry.get("现场复核时间") or "")
        request_review = str(values.get("复核时间") or "").strip()
        if request_review and request_review != review_at:
            return None, f"检测编号 {code} 的签发依据已过期：评分冲突以最后一次现场复核（{review_at}）为准，请重新发起"
        with store.transaction(*TRANSACTION_MODULES):
            self._apply_issuance(entry, batch_id=batch_id)
        return self._present(entry), f"检测编号 {code} 已签发，结论已同步检测清单、工程待办与限载投影"

    def _apply_issuance(self, entry: dict[str, Any], batch_id: str) -> None:
        """签发落库：检测清单、工程待办、限载投影三处同事务写入，任一失败整体回滚。"""
        score = parse_score(entry.get("复核评分"))
        if score is None:
            raise ValueError(f"检测编号 {entry.get('检测编号')} 缺少有效复核评分，签发已回滚")
        grade = grade_of(score)
        now = now_text()
        version = int(entry.get("评分版本") or 0)
        if version > 0:
            # 历史等级按检测时版本保留，不被新评分覆盖
            entry.setdefault("历史版本", []).append({
                "版本": version,
                "技术状况评分": entry.get("技术状况评分"),
                "技术状况等级": entry.get("技术状况等级"),
                "签发时间": entry.get("签发时间"),
            })
        entry["评分版本"] = version + 1
        entry["技术状况评分"] = score
        entry["技术状况等级"] = grade
        entry["签发状态"] = "已签发"
        entry["签发时间"] = now
        entry["签发复核时间"] = entry.get("现场复核时间")
        entry["status"] = "已签发"
        entry["pending"] = True
        entry["abnormal"] = grade in ("四类", "五类")
        code = str(entry.get("检测编号") or "")
        name = str(entry.get("桥梁名称") or "")
        # 结论回写工程待办：同一检测编号只更新不新增，重复签发不会产生重复任务
        project_service.sync_inspection_todo(
            code=code,
            bridge_name=name,
            conclusion=f"{grade} / {score:g}分",
            score=score,
            version=entry["评分版本"],
            issued_at=now,
        )
        # 结论回写限载投影：同一检测编号只保留一个生效版本，旧评分不再重复显示
        load_limit_service.project_issuance(
            code=code,
            bridge_name=name,
            score=score,
            grade=grade,
            version=entry["评分版本"],
            batch_id=batch_id,
            issued_at=now,
        )

    def issue_batch(self, batch_id: str, ids: list[int]) -> tuple[dict[str, Any] | None, str]:
        """分组提交：整批一个事务，成功后每个检测编号只保留一个版本。

        断点续做：已签发且复核版本未变的记录跳过不重复签发；批次号已完成的
        重复提交直接幂等返回。任一条失败整批回滚，不留下半批限载。
        """
        batch_id = str(batch_id or "").strip()
        if not batch_id:
            return None, "分组提交缺少批次号"
        if not ids:
            return None, "分组提交未选择检测记录"
        batches = store.rows("issue_batch")
        for batch in batches:
            if batch.get("batch_id") == batch_id and batch.get("status") == "完成":
                return dict(batch), f"批次 {batch_id} 已受理完成，断点续做跳过重复提交"
        entries: list[dict[str, Any]] = []
        for raw_id in ids:
            entry = store.find(MODULE, int(raw_id))
            if entry is None:
                return None, f"检测记录 {raw_id} 不存在，分组提交未执行"
            entries.append(entry)
        # 按检测编号排序后依次取锁，避免两个批次交叉持锁死锁
        entries.sort(key=lambda row: str(row.get("检测编号") or row.get("id", 0)))
        try:
            with ExitStack() as stack:
                for entry in entries:
                    stack.enter_context(self._lock_for(str(entry.get("检测编号") or entry.get("id", 0))))
                with store.transaction(*TRANSACTION_MODULES):
                    done: list[str] = []
                    skipped: list[str] = []
                    for entry in entries:
                        code = str(entry.get("检测编号") or "")
                        if entry.get("status") == "已签发" and entry.get("签发复核时间") == entry.get("现场复核时间"):
                            skipped.append(code)
                            done.append(code)
                            continue
                        if entry.get("status") != "已复核":
                            raise ValueError(f"检测编号 {code} 未现场复核，整批已回滚")
                        self._apply_issuance(entry, batch_id=batch_id)
                        done.append(code)
                    batch = {
                        "batch_id": batch_id,
                        "status": "完成",
                        "done": done,
                        "skipped": skipped,
                        "updated_at": now_text(),
                    }
                    batches.append(batch)
        except ValueError as exc:
            return None, f"{exc}，本批未留下任何限载投影"
        return dict(batch), f"批次 {batch_id} 签发完成：新生效 {len(done) - len(skipped)} 条，断点续做跳过 {len(skipped)} 条"

    def batch_status(self, batch_id: str) -> dict[str, Any] | None:
        for batch in store.rows("issue_batch"):
            if batch.get("batch_id") == batch_id:
                return dict(batch)
        return None

    def migrate_legacy(self) -> int:
        """存量迁移：未签发检测编号回填到正确节点，按采集顺序（检测日期、id）处理。"""
        rows = store.rows(MODULE)
        legacy = [row for row in rows if "签发状态" not in row]
        if not legacy:
            return 0
        legacy.sort(key=lambda row: (str(row.get("检测日期") or "9999-12-31"), int(row.get("id", 0))))
        for row in legacy:
            archived = row.get("status") == "已归档"
            row["签发状态"] = "已签发" if archived else "未签发"
            row["评分版本"] = 1 if archived else 0
            row["现场复核时间"] = None
            row["复核评分"] = None
            row["签发时间"] = None
            row["签发复核时间"] = None
            row["技术状况等级"] = row.get("技术状况等级") or ""
            row["历史版本"] = []
        return len(legacy)
