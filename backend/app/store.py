"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
跨模块的联动写入（例如定检签发要同时落检测清单、工程待办、限载投影）通过
snapshot/restore 做到任一落库失败整体回滚，不留半批数据。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def snapshot(self, *modules: str) -> dict[str, list[dict[str, Any]]]:
        """给指定模块拍深拷贝快照，供失败时整体回滚。"""
        return {name: copy.deepcopy(self.rows(name)) for name in modules}

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        """把快照整体写回，丢弃快照之后产生的全部改动。"""
        for name, rows in snapshot.items():
            self._tables[name] = rows

    @contextmanager
    def transaction(self, *modules: str) -> Iterator[None]:
        """跨模块事务：任一环节抛错即回滚到进入前的状态。"""
        snapshot = self.snapshot(*modules)
        try:
            yield
        except Exception:
            self.restore(snapshot)
            raise

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
