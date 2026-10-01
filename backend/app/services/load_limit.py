"""限载投影业务规则：只读投影 + 签发回写。

限载只能由定检签发产生，未签发不得生成限载；同一检测编号只保留一个
生效版本，重新签发时旧版本置为已失效，限载页面不再重复显示旧评分。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "load_limit"


def load_of(score: float) -> str:
    """按技术状况评分折算限载吨位。"""
    if score >= 90:
        return "不限载"
    if score >= 80:
        return "30t"
    if score >= 70:
        return "20t"
    if score >= 60:
        return "10t"
    return "5t"


class LoadLimitService:
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
            rows = [
                row for row in rows
                if keyword in str(row.get("检测编号", "")) or keyword in str(row.get("桥梁名称", ""))
            ]
        if status and status != "全部":
            rows = [row for row in rows if row.get("投影状态") == status]
        elif not status:
            # 默认只看生效中的投影，历史版本不重复占屏
            rows = [row for row in rows if row.get("投影状态") == "生效中"]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def project_issuance(
        self,
        *,
        code: str,
        bridge_name: str,
        score: float,
        grade: str,
        version: int,
        batch_id: str,
        issued_at: str,
    ) -> dict[str, Any]:
        """签发回写限载投影：旧版本全部失效，只保留新签发的一个生效版本。"""
        rows = store.rows(MODULE)
        for row in rows:
            if row.get("检测编号") == code and row.get("投影状态") == "生效中":
                row["投影状态"] = "已失效"
                row["失效时间"] = issued_at
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        entry = {
            "id": next_id,
            "限载编号": f"LOAD-{next_id:04d}",
            "检测编号": code,
            "桥梁名称": bridge_name,
            "技术状况评分": score,
            "技术状况等级": grade,
            "评分版本": version,
            "限载吨位": load_of(score),
            "投影状态": "生效中",
            "批次号": batch_id,
            "签发时间": issued_at,
            "status": "生效中",
            "pending": False,
            "abnormal": grade in ("四类", "五类"),
        }
        rows.append(entry)
        return entry
