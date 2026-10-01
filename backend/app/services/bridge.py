"""桥梁定检业务规则：状态流转、字段校验与筛选口径都收在这里。

集中签发链：现场复核 → 签发 → 限载投影，单向推进，跳级由服务端拒绝。
签发结论在同一事务里回写检测清单（bridge）、工程待办（project）、限载页面（bridge_info），
任一落库失败整体回滚；并发签发按检测编号只生效一版，冲突以最后一次现场复核为准。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "bridge"
PROJECT_MODULE = "project"
LOAD_LIMIT_MODULE = "bridge_info"

REQUIRED_FIELDS = ["检测编号", "桥梁名称", "检测类型"]
STATUS_ORDER = ["待检测", "检测中", "已评定", "已归档"]
ACTION_RULES = {"开始检测": "检测中", "完成评定": "已评定", "归档报告": "已归档"}
NEGATIVE_ACTIONS = []

# 签发链节点：现场复核 → 签发 → 限载投影，只能单向推进
CHAIN_ORDER = ["待复核", "已复核", "已签发", "已限载"]

# 存量检测状态 → 签发链节点：未签发的存量编号迁移到正确节点
LEGACY_CHAIN_MAP = {
    "待检测": "待复核",
    "检测中": "待复核",
    "已评定": "已复核",
    "已归档": "已签发",
}

# 技术状况评分 → 评定等级；四类、五类触发限载投影
GRADE_BANDS = [(90, "一类"), (75, "二类"), (60, "三类"), (40, "四类"), (0, "五类")]
LOAD_LIMIT_BY_GRADE = {"四类": "限载30t", "五类": "限载15t"}


class IssuanceError(Exception):
    """签发链路可读的失败原因：回滚整体事务后直接抛给路由层。"""


class BatchItemError(IssuanceError):
    """集中签发某一项失败：带上断点位置，整批回滚。"""

    def __init__(self, index: int, message: str) -> None:
        super().__init__(message)
        self.index = index


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= score <= 100:
        return None
    return score


def _grade_of(score: float) -> str:
    for floor, grade in GRADE_BANDS:
        if score >= floor:
            return grade
    return GRADE_BANDS[-1][1]


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class BridgeService:
    def __init__(self) -> None:
        # 集中签发批次断点日志：独立于业务表，批次回滚后断点仍然保留
        self._journals: dict[str, dict[str, Any]] = {}
        self.migrate_legacy()

    # ------------------------------------------------------------------
    # 存量迁移：未签发检测编号回填到正确节点，按采集顺序（检测日期）回放
    # ------------------------------------------------------------------
    def migrate_legacy(self) -> int:
        with store.lock:
            legacy = [row for row in store.rows(MODULE) if "签发状态" not in row]
            legacy.sort(key=lambda row: (str(row.get("检测日期") or ""), int(row.get("id", 0))))
            for row in legacy:
                chain = LEGACY_CHAIN_MAP.get(str(row.get("status") or ""), "待复核")
                row["签发状态"] = chain
                row["versions"] = []
                row["评分版本"] = "—"
                if chain in ("已复核", "已签发"):
                    # 已评定/已归档的存量记录回填第 1 版复核结论，复核时间取采集时间
                    score = _to_score(row.get("技术状况评分")) or 0.0
                    row["versions"].append({
                        "version": 1,
                        "评分": score,
                        "等级": _grade_of(score),
                        "复核时间": str(row.get("检测日期") or ""),
                        "复核人": "存量迁移",
                        "来源": "存量回填",
                    })
                    row["评定等级"] = _grade_of(score)
                    row["评分版本"] = "第1版"
                else:
                    row["评定等级"] = row.get("评定等级") or "—"
                if chain == "已签发":
                    # 历史已归档视为已签发：结论生效但限载投影未生成，留给断点续做
                    row["issued_version"] = 1
                    row["签发时间"] = str(row.get("检测日期") or "")
                row["检测状态"] = chain
            return len(legacy)

    # ------------------------------------------------------------------
    # 列表与登记（保留原有口径）
    # ------------------------------------------------------------------
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
            rows = [row for row in rows if row.get("签发状态") == status or row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": _next_id(rows)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["桥梁编号"] = str(values.get("桥梁编号") or "").strip()
        entry["检测日期"] = str(values.get("检测日期") or "")
        entry["技术状况评分"] = values.get("技术状况评分") or ""
        entry["主要病害"] = str(values.get("主要病害") or "")
        entry["检测单位"] = str(values.get("检测单位") or "")
        entry["status"] = STATUS_ORDER[0]
        entry["签发状态"] = CHAIN_ORDER[0]
        entry["versions"] = []
        entry["评定等级"] = "—"
        entry["评分版本"] = "—"
        entry["检测状态"] = CHAIN_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检测记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于桥梁定检可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"检测记录已{action}"

    # ------------------------------------------------------------------
    # 签发链第一步：现场复核，每次复核生成一个评分版本
    # ------------------------------------------------------------------
    def review_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"检测记录 {entry_id} 不存在或已归档"
            score = _to_score(values.get("评分"))
            if score is None:
                return None, "现场复核需要 0-100 的技术状况评分"
            reviewer = str(values.get("复核人") or "").strip() or "现场复核组"
            versions: list[dict[str, Any]] = entry.setdefault("versions", [])
            version_no = len(versions) + 1
            # 历史等级按检测时版本保留：版本落库后不再改写
            versions.append({
                "version": version_no,
                "评分": score,
                "等级": _grade_of(score),
                "复核时间": _now(),
                "复核人": reviewer,
                "来源": "现场复核",
            })
            # 已签发/已限载的记录来了新复核：回到待签发，生效结论仍以已签发版本为准
            entry["签发状态"] = "已复核"
            entry["检测状态"] = "已复核"
            entry["pending"] = True
            return entry, f"检测编号{entry.get('检测编号')}已完成第{version_no}版现场复核"

    # ------------------------------------------------------------------
    # 签发链第二步：签发，结论同事务回写检测清单、工程待办、限载页面
    # ------------------------------------------------------------------
    def issue_entry(
        self,
        entry_id: int,
        expect_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"检测记录 {entry_id} 不存在或已归档"
            chain = str(entry.get("签发状态") or "待复核")
            if chain == "待复核":
                return None, f"检测编号{entry.get('检测编号')}尚未现场复核，跳级签发已被服务端拒绝"
            if chain in ("已签发", "已限载"):
                issued = entry.get("issued_version")
                return None, (
                    f"检测编号{entry.get('检测编号')}已按第{issued}版复核结论签发，"
                    "重复签发已拦截；如有新复核结论请先完成现场复核"
                )
            versions: list[dict[str, Any]] = entry.get("versions") or []
            if not versions:
                return None, f"检测编号{entry.get('检测编号')}没有可签发的复核结论"
            latest = versions[-1]
            if expect_version is not None and expect_version != int(latest["version"]):
                return None, (
                    f"评分版本冲突：检测到第{latest['version']}版现场复核，"
                    "并发签发只生效一版，以最后一次现场复核为准，请刷新后重试"
                )
            try:
                with store.transaction(MODULE, PROJECT_MODULE, LOAD_LIMIT_MODULE):
                    self._write_back(entry, latest, publish=False)
            except IssuanceError as exc:
                return None, str(exc)
            if entry.get("投影结果") == "被更新复核覆盖":
                return entry, (
                    f"检测编号{entry.get('检测编号')}已按第{latest['version']}版复核结论签发，"
                    "但其复核时间早于限载页面现有投影，以最后一次现场复核为准，限载页面保留更新结论"
                )
            return entry, (
                f"检测编号{entry.get('检测编号')}已按第{latest['version']}版复核结论签发，"
                "结论已同步检测清单、工程待办和限载页面"
            )

    # ------------------------------------------------------------------
    # 签发链第三步：限载投影发布；未签发不得生成限载
    # ------------------------------------------------------------------
    def project_load(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"检测记录 {entry_id} 不存在或已归档"
            chain = str(entry.get("签发状态") or "待复核")
            if chain in ("待复核", "已复核"):
                return None, f"检测编号{entry.get('检测编号')}未签发不得生成限载，跳级已被服务端拒绝"
            if chain == "已限载":
                return entry, f"检测编号{entry.get('检测编号')}的限载投影已是最新版本，无需重复生成"
            versions: list[dict[str, Any]] = entry.get("versions") or []
            issued = int(entry.get("issued_version") or 0)
            version = next((v for v in versions if int(v["version"]) == issued), None)
            if version is None:
                return None, f"检测编号{entry.get('检测编号')}缺少已签发的评分版本，无法生成限载"
            try:
                with store.transaction(MODULE, LOAD_LIMIT_MODULE):
                    projected = self._project_load_limit(entry, version, publish=True)
                    entry["投影结果"] = "已生效" if projected else "被更新复核覆盖"
                    self._mark_projected(entry)
            except IssuanceError as exc:
                return None, str(exc)
            if not projected:
                return entry, (
                    f"检测编号{entry.get('检测编号')}的复核结论早于桥梁{entry.get('桥梁编号')}现有投影，"
                    "以最后一次现场复核为准，限载页面保留更新结论"
                )
            return entry, f"检测编号{entry.get('检测编号')}限载投影已发布到限载页面"

    # ------------------------------------------------------------------
    # 集中签发：整批一个事务，成功只保留一个版本，失败回滚不留半批限载；
    # 断点日志独立于业务事务，支持断点续做与重复签发拦截
    # ------------------------------------------------------------------
    def issue_batch(
        self,
        batch_id: str | None,
        codes: list[str],
    ) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            batch_id = (batch_id or "").strip() or f"BATCH-{datetime.now():%Y%m%d%H%M%S}"
            journal = self._journals.get(batch_id)
            if journal is not None:
                # 同批次号再次提交：按断点续做处理，以日志里的清单为准
                return self._run_batch(journal, resumed=True)
            if not codes:
                return None, "集中签发需要至少一个检测编号"
            journal = {
                "batch_id": batch_id,
                "items": [{"检测编号": code, "status": "待办", "message": ""} for code in codes],
                "status": "进行中",
                "checkpoint": 0,
                "runs": 0,
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._journals[batch_id] = journal
            return self._run_batch(journal, resumed=False)

    def resume_batch(self, batch_id: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            journal = self._journals.get(batch_id)
            if journal is None:
                return None, f"签发批次 {batch_id} 不存在，无法断点续做"
            return self._run_batch(journal, resumed=True)

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self._journals.get(batch_id)

    def _run_batch(self, journal: dict[str, Any], *, resumed: bool) -> tuple[dict[str, Any] | None, str]:
        items: list[dict[str, Any]] = journal["items"]
        if journal["status"] == "成功":
            return journal, f"批次{journal['batch_id']}已签发完成，重复提交已拦截"
        journal["runs"] += 1
        self._reconcile_journal(journal)
        done_this_run: list[int] = []
        try:
            with store.transaction(MODULE, PROJECT_MODULE, LOAD_LIMIT_MODULE):
                for index in range(journal["checkpoint"], len(items)):
                    item = items[index]
                    if item["status"] in ("成功", "跳过"):
                        continue
                    entry = self._find_by_code(item["检测编号"])
                    if entry is None:
                        raise BatchItemError(index, f"检测编号{item['检测编号']}不存在")
                    chain = str(entry.get("签发状态") or "待复核")
                    if chain in ("已签发", "已限载"):
                        # 重复签发拦截：已生效版本保持不变，整批只保留一个版本
                        item["status"] = "跳过"
                        item["message"] = f"已按第{entry.get('issued_version')}版签发，重复签发已拦截"
                        continue
                    if chain != "已复核":
                        raise BatchItemError(index, f"检测编号{item['检测编号']}尚未现场复核，跳级签发被拒绝")
                    versions: list[dict[str, Any]] = entry.get("versions") or []
                    if not versions:
                        raise BatchItemError(index, f"检测编号{item['检测编号']}没有可签发的复核结论")
                    try:
                        self._write_back(entry, versions[-1], publish=True)
                    except IssuanceError as exc:
                        raise BatchItemError(index, str(exc)) from exc
                    item["status"] = "成功"
                    suffix = "，投影被更新复核覆盖" if entry.get("投影结果") == "被更新复核覆盖" else ""
                    item["message"] = f"已按第{versions[-1]['version']}版复核结论签发并生成限载{suffix}"
                    done_this_run.append(index)
        except BatchItemError as exc:
            # 业务表已整体回滚：本轮新签发的项回到待办，断点记到失败项
            for index in done_this_run:
                items[index]["status"] = "待办"
                items[index]["message"] = ""
            items[exc.index]["status"] = "失败"
            items[exc.index]["message"] = str(exc)
            journal["status"] = "失败"
            journal["updated_at"] = _now()
            self._refresh_checkpoint(journal)
            return None, (
                f"批次{journal['batch_id']}第{exc.index + 1}项失败，整批已回滚、未留下半批限载：{exc}；"
                "修复后可用断点续做继续"
            )
        journal["status"] = "成功"
        journal["updated_at"] = _now()
        self._refresh_checkpoint(journal)
        issued = sum(1 for item in items if item["status"] == "成功")
        skipped = sum(1 for item in items if item["status"] == "跳过")
        prefix = "断点续做完成" if resumed else "集中签发完成"
        return journal, f"{prefix}：批次{journal['batch_id']}新签发{issued}项、重复拦截{skipped}项，每编号只保留一个生效版本"

    def _reconcile_journal(self, journal: dict[str, Any]) -> None:
        """续做前对账：日志里标了成功/跳过、但业务表并未生效的项，回到待办重签。"""
        items: list[dict[str, Any]] = journal["items"]
        for item in items:
            if item["status"] not in ("成功", "跳过"):
                continue
            entry = self._find_by_code(item["检测编号"])
            if entry is None or str(entry.get("签发状态") or "") not in ("已签发", "已限载"):
                item["status"] = "待办"
                item["message"] = ""
        self._refresh_checkpoint(journal)

    def _refresh_checkpoint(self, journal: dict[str, Any]) -> None:
        items: list[dict[str, Any]] = journal["items"]
        pending = [i for i, item in enumerate(items) if item["status"] in ("待办", "失败")]
        journal["checkpoint"] = pending[0] if pending else len(items)

    # ------------------------------------------------------------------
    # 同事务回写：检测清单 + 工程待办 + 限载页面，任一失败整体回滚
    # ------------------------------------------------------------------
    def _write_back(self, entry: dict[str, Any], version: dict[str, Any], *, publish: bool) -> None:
        code = str(entry.get("检测编号") or "")
        grade = str(version["等级"])
        score = version["评分"]

        # 1) 检测清单：结论与评分版本回写到检测记录本身
        entry["技术状况评分"] = score
        entry["评定等级"] = grade
        entry["评分版本"] = f"第{version['version']}版"
        entry["issued_version"] = int(version["version"])
        entry["签发时间"] = _now()
        entry["abnormal"] = grade in LOAD_LIMIT_BY_GRADE

        # 2) 工程待办：按来源检测编号 upsert，再次签发只更新不新增，杜绝重复任务
        todo = self._find_project_todo(code)
        if todo is None:
            rows = store.rows(PROJECT_MODULE)
            todo = {
                "id": _next_id(rows),
                "工程编号": f"TODO-{code}",
                "工程名称": f"{entry.get('桥梁名称')}病害处治",
                "工程类型": "桥梁定检结论",
                "施工路段": str(entry.get("桥梁名称") or ""),
                "承建单位": "待定",
                "开工日期": "—",
                "竣工日期": "—",
                "工程状态": "待开工",
                "status": "待开工",
                "pending": True,
                "abnormal": grade in LOAD_LIMIT_BY_GRADE,
                "来源检测编号": code,
            }
            rows.append(todo)
        todo["结论评分"] = score
        todo["结论等级"] = grade
        todo["评分版本"] = entry["评分版本"]
        todo["签发时间"] = entry["签发时间"]
        todo["abnormal"] = grade in LOAD_LIMIT_BY_GRADE

        # 3) 限载页面：按桥梁编号 upsert 投影，旧评分被替换而不是重复显示
        projected = self._project_load_limit(entry, version, publish=publish)
        entry["投影结果"] = "已生效" if projected else "被更新复核覆盖"

        if publish:
            self._mark_projected(entry)
        else:
            entry["签发状态"] = "已签发"
            entry["检测状态"] = "已签发"

    def _project_load_limit(self, entry: dict[str, Any], version: dict[str, Any], *, publish: bool) -> bool:
        """把签发结论投影到限载页面；返回 False 表示被更新的复核结论覆盖、未生效。

        冲突以最后一次现场复核为准：投影行已挂着的复核时间比本版本新时，
        旧结论不得覆盖限载页面，只回写检测清单与工程待办。
        """
        bridge_code = str(entry.get("桥梁编号") or "").strip()
        if not bridge_code:
            raise IssuanceError(f"检测编号{entry.get('检测编号')}未登记桥梁编号，签发整体回滚")
        projection = self._find_load_limit(bridge_code)
        if projection is None:
            raise IssuanceError(f"桥梁档案{bridge_code}不存在，限载投影落库失败，签发整体回滚")
        incoming_review = str(version.get("复核时间") or "")
        existing_review = str(projection.get("投影复核时间") or "")
        same_source = str(projection.get("来源检测编号") or "") == str(entry.get("检测编号") or "")
        if existing_review and existing_review > incoming_review and not same_source:
            return False
        grade = str(version["等级"])
        projection["上次评定等级"] = grade
        projection["评分版本"] = f"第{version['version']}版"
        projection["限载值"] = LOAD_LIMIT_BY_GRADE.get(grade, "—")
        projection["来源检测编号"] = str(entry.get("检测编号") or "")
        projection["投影复核时间"] = incoming_review
        projection["投影状态"] = "已发布" if publish else "待发布"
        if publish:
            projection["限载时间"] = _now()
            projection["桥梁状态"] = "限载" if grade in LOAD_LIMIT_BY_GRADE else "正常"
            projection["status"] = projection["桥梁状态"]
            projection["abnormal"] = grade in LOAD_LIMIT_BY_GRADE
            projection["pending"] = projection["abnormal"]
        return True

    def _mark_projected(self, entry: dict[str, Any]) -> None:
        entry["签发状态"] = "已限载"
        entry["检测状态"] = "已限载"
        entry["限载时间"] = _now()
        entry["pending"] = False

    # ------------------------------------------------------------------
    # 查找辅助
    # ------------------------------------------------------------------
    def _find_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("检测编号") or "") == code:
                return row
        return None

    def _find_project_todo(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(PROJECT_MODULE):
            if str(row.get("来源检测编号") or "") == code:
                return row
        return None

    def _find_load_limit(self, bridge_code: str) -> dict[str, Any] | None:
        for row in store.rows(LOAD_LIMIT_MODULE):
            if str(row.get("桥梁编号") or "") == bridge_code:
                return row
        return None
