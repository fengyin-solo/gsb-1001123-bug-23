"""养护工程业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "project"
REQUIRED_FIELDS = ["工程编号", "工程名称", "工程类型"]
STATUS_ORDER = ["待开工", "施工中", "已竣工", "已验收"]
ACTION_RULES = {"批准开工": "施工中", "竣工验收": "已竣工", "驳回验收": "已竣工"}
NEGATIVE_ACTIONS = ["驳回验收"]


def _next_project_number(rows: list[dict[str, Any]]) -> str:
    """从现有工程编号推出下一个编号，保证签发回写的待办不与存量撞号。"""
    top = 0
    for row in rows:
        code = str(row.get("工程编号") or "")
        if code.startswith("PROJ-") and code[5:].isdigit():
            top = max(top, int(code[5:]))
    return f"PROJ-{top + 1:04d}"


class ProjectService:
    def _present(self, row: dict[str, Any]) -> dict[str, Any]:
        """列表/明细投影：把内部状态映射到清单字段，不动仓库里的原始行。"""
        item = dict(row)
        item["工程状态"] = row.get("status", "")
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
            rows = [row for row in rows if keyword in str(row.get("工程编号", ""))]
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
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"养护工程 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于养护工程可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"养护工程已{action}"

    def sync_inspection_todo(
        self,
        *,
        code: str,
        bridge_name: str,
        conclusion: str,
        score: float,
        version: int,
        issued_at: str,
    ) -> tuple[dict[str, Any], bool]:
        """定检签发结论回写工程待办。

        以来源检测编号为幂等键：已存在的待办只更新结论与版本，不重复新增，
        再次签发不会产生重复任务；工程详情因此始终与检测清单对得上。
        """
        rows = store.rows(MODULE)
        for row in rows:
            if row.get("来源检测编号") == code:
                row["定检结论"] = conclusion
                row["技术状况评分"] = score
                row["评分版本"] = version
                row["签发时间"] = issued_at
                return row, False
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "工程编号": _next_project_number(rows),
            "工程名称": f"{bridge_name}定检处治工程",
            "工程类型": "桥梁养护",
            "施工路段": bridge_name,
            "承建单位": "",
            "开工日期": "",
            "竣工日期": "",
            "工程状态": "待开工",
            "status": "待开工",
            "pending": True,
            "abnormal": False,
            "来源检测编号": code,
            "定检结论": conclusion,
            "技术状况评分": score,
            "评分版本": version,
            "签发时间": issued_at,
        }
        rows.append(entry)
        return entry, True
