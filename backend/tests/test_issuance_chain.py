"""签发链验证：单向流转、三处回写、并发唯一、批次断点续做与存量迁移。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)


def fresh_inspection(score: float | None = None) -> dict:
    """登记一条检测记录并推进到已评定，返回记录 id。"""
    resp = client.post("/api/bridge", json={"values": {
        "检测编号": f"BRID-T{fresh_inspection.seq:04d}",
        "桥梁名称": f"测试桥{fresh_inspection.seq}",
        "检测类型": "定期检测",
        "检测日期": "2026-09-10",
    }})
    fresh_inspection.seq += 1
    assert resp.json()["ok"], resp.json()
    entry_id = resp.json()["entry"]["id"]
    assert client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "开始检测"}}).json()["ok"]
    assert client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "完成评定"}}).json()["ok"]
    if score is not None:
        resp = client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "现场复核", "复核评分": score}})
        assert resp.json()["ok"], resp.json()
    return {"id": entry_id}


fresh_inspection.seq = 1


def test_legacy_migrated_on_startup() -> None:
    """存量未签发检测编号已回填到正确节点：未签发、零版本。"""
    rows = store.rows("bridge")
    legacy = [row for row in rows if str(row.get("检测编号", "")).startswith("BRID-000")]
    assert legacy, "种子数据应存在"
    for row in legacy:
        assert row["签发状态"] == "未签发"
        assert row["评分版本"] == 0
        assert row["status"] in ("待检测", "检测中", "已评定")


def test_skip_level_rejected() -> None:
    """跳级由服务端拒绝：待检测不能直接现场复核或签发。"""
    resp = client.post("/api/bridge", json={"values": {"检测编号": "BRID-SKIP", "桥梁名称": "跳级桥", "检测类型": "定期检测"}})
    entry_id = resp.json()["entry"]["id"]
    resp = client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "现场复核", "复核评分": 88}})
    assert not resp.json()["ok"]
    assert "完成评定后才能现场复核" in resp.json()["message"]
    resp = client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "签发结论"}})
    assert not resp.json()["ok"]


def test_issue_requires_review() -> None:
    """未现场复核不得签发。"""
    item = fresh_inspection()
    resp = client.post(f"/api/bridge/{item['id']}/actions", json={"values": {"action": "签发结论"}})
    assert not resp.json()["ok"]
    assert "未现场复核不得签发" in resp.json()["message"]
    assert not store.rows("load_limit"), "未签发不得生成限载"


def test_issue_syncs_three_places() -> None:
    """签发结论同步检测清单、工程待办、限载投影。"""
    item = fresh_inspection(score=85)
    resp = client.post(f"/api/bridge/{item['id']}/actions", json={"values": {"action": "签发结论"}})
    assert resp.json()["ok"], resp.json()
    entry = resp.json()["entry"]
    assert entry["签发状态"] == "已签发"
    assert entry["评分版本"] == 1
    assert entry["技术状况等级"] == "二类"
    code = entry["检测编号"]

    todos = [row for row in store.rows("project") if row.get("来源检测编号") == code]
    assert len(todos) == 1, "工程待办应新增一条"
    assert todos[0]["定检结论"] == "二类 / 85分"
    assert todos[0]["评分版本"] == 1

    limits = [row for row in store.rows("load_limit") if row.get("检测编号") == code]
    assert len(limits) == 1
    assert limits[0]["投影状态"] == "生效中"
    assert limits[0]["限载吨位"] == "30t"


def test_duplicate_issue_blocked_and_reissue_keeps_single_version() -> None:
    """重复签发拦截；新复核后再签发只保留一个生效版本，待办不重复。"""
    item = fresh_inspection(score=85)
    entry_id = item["id"]
    assert client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "签发结论"}}).json()["ok"]
    code = client.get(f"/api/bridge/{entry_id}").json()["检测编号"]

    # 同一复核版本重复签发 → 拦截，不产生重复任务
    resp = client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "签发结论"}})
    assert not resp.json()["ok"]
    assert "重复签发已拦截" in resp.json()["message"]
    assert len([row for row in store.rows("project") if row.get("来源检测编号") == code]) == 1

    # 新的现场复核后允许再签发：版本递增，历史保留，限载旧版失效
    assert client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "现场复核", "复核评分": 72}}).json()["ok"]
    resp = client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "签发结论"}})
    assert resp.json()["ok"], resp.json()
    entry = resp.json()["entry"]
    assert entry["评分版本"] == 2
    assert entry["技术状况等级"] == "三类"
    assert entry["历史版本"][0]["技术状况等级"] == "二类", "历史等级按检测时版本保留"

    limits = [row for row in store.rows("load_limit") if row.get("检测编号") == code]
    active = [row for row in limits if row["投影状态"] == "生效中"]
    assert len(active) == 1 and active[0]["评分版本"] == 2, "限载只保留一个生效版本"
    assert any(row["投影状态"] == "已失效" for row in limits)

    todos = [row for row in store.rows("project") if row.get("来源检测编号") == code]
    assert len(todos) == 1 and todos[0]["评分版本"] == 2, "再次签发不新增重复任务"


def test_stale_review_conflict_rejected() -> None:
    """评分冲突以最后一次现场复核为准：携带过期复核时间的签发被拒。"""
    item = fresh_inspection(score=90)
    entry_id = item["id"]
    first_review = client.get(f"/api/bridge/{entry_id}").json()["现场复核时间"]
    # 再次复核，复核时间前进
    assert client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "现场复核", "复核评分": 66}}).json()["ok"]
    resp = client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "签发结论", "复核时间": first_review}})
    assert not resp.json()["ok"]
    assert "以最后一次现场复核" in resp.json()["message"]


def test_batch_rollback_leaves_no_partial_limits() -> None:
    """分组提交任一失败整体回滚，不留下半批限载。"""
    ok_item = fresh_inspection(score=95)
    bad_item = fresh_inspection()  # 未复核，必然失败
    before_limits = len(store.rows("load_limit"))
    before_projects = len(store.rows("project"))
    resp = client.post("/api/bridge/issue-batch", json={"values": {
        "batch_id": "BATCH-ROLLBACK",
        "ids": [ok_item["id"], bad_item["id"]],
    }})
    assert not resp.json()["ok"]
    assert "未留下任何限载投影" in resp.json()["message"]
    assert len(store.rows("load_limit")) == before_limits, "失败回滚不得留下半批限载"
    assert len(store.rows("project")) == before_projects
    assert client.get("/api/bridge/" + str(ok_item["id"])).json()["签发状态"] == "未签发"


def test_batch_resume_and_idempotent_retry() -> None:
    """断点续做：修复断点后同批次号重试只补未签发项；完成批次重复提交幂等。"""
    first = fresh_inspection(score=88)
    second = fresh_inspection()  # 先不复核，制造断点
    batch_id = "BATCH-RESUME"
    resp = client.post("/api/bridge/issue-batch", json={"values": {"batch_id": batch_id, "ids": [first["id"], second["id"]]}})
    assert not resp.json()["ok"]

    # 补上复核后同批次号重试：第一条已回滚需重做，第二条从断点续做
    assert client.post(f"/api/bridge/{second['id']}/actions", json={"values": {"action": "现场复核", "复核评分": 77}}).json()["ok"]
    resp = client.post("/api/bridge/issue-batch", json={"values": {"batch_id": batch_id, "ids": [first["id"], second["id"]]}})
    assert resp.json()["ok"], resp.json()
    assert resp.json()["entry"]["status"] == "完成"

    codes = [client.get(f"/api/bridge/{item['id']}").json()["检测编号"] for item in (first, second)]
    for code in codes:
        active = [row for row in store.rows("load_limit") if row.get("检测编号") == code and row["投影状态"] == "生效中"]
        assert len(active) == 1, "分组提交成功后只保留一个版本"

    # 完成批次重复提交：幂等返回，不再新增限载与任务
    limits_before = len(store.rows("load_limit"))
    projects_before = len(store.rows("project"))
    resp = client.post("/api/bridge/issue-batch", json={"values": {"batch_id": batch_id, "ids": [first["id"], second["id"]]}})
    assert resp.json()["ok"]
    assert "断点续做跳过重复提交" in resp.json()["message"]
    assert len(store.rows("load_limit")) == limits_before
    assert len(store.rows("project")) == projects_before

    # 跨批次重发同一批记录：已签发项被跳过而非重复签发
    resp = client.post("/api/bridge/issue-batch", json={"values": {"batch_id": "BATCH-RESUME-2", "ids": [first["id"], second["id"]]}})
    assert resp.json()["ok"]
    assert len(resp.json()["entry"]["skipped"]) == 2
    assert len(store.rows("load_limit")) == limits_before


def test_concurrent_issue_single_version() -> None:
    """并发签发按检测编号只生效一版。"""
    item = fresh_inspection(score=83)
    entry_id = item["id"]
    results: list[dict] = []

    def fire() -> None:
        resp = client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "签发结论"}})
        results.append(resp.json())

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for item in results if item["ok"]) == 1, "并发签发只生效一版"
    code = client.get(f"/api/bridge/{entry_id}").json()["检测编号"]
    active = [row for row in store.rows("load_limit") if row.get("检测编号") == code and row["投影状态"] == "生效中"]
    assert len(active) == 1
    assert len([row for row in store.rows("project") if row.get("来源检测编号") == code]) == 1


def test_export_and_load_limit_default_view() -> None:
    """限载列表默认只显示生效版本；导出接口可用。"""
    item = fresh_inspection(score=68)
    entry_id = item["id"]
    assert client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "签发结论"}}).json()["ok"]
    assert client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "现场复核", "复核评分": 91}}).json()["ok"]
    assert client.post(f"/api/bridge/{entry_id}/actions", json={"values": {"action": "签发结论"}}).json()["ok"]

    resp = client.get("/api/load_limit")
    assert resp.status_code == 200
    rows = resp.json()["items"]
    assert all(row["投影状态"] == "生效中" for row in rows), "限载页不再重复显示旧评分"

    resp = client.get("/api/load_limit", params={"status": "已失效"})
    assert any(row["评分版本"] == 1 for row in resp.json()["items"]), "历史版本仍可查"

    resp = client.get("/api/bridge/export")
    assert resp.status_code == 200
    assert resp.json()["module"] == "bridge"


@pytest.fixture(autouse=True, scope="module")
def _reset_seq() -> None:
    fresh_inspection.seq = 100
