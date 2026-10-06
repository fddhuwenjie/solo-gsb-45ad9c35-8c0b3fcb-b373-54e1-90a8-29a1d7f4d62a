# -*- coding: utf-8 -*-
"""页面提交与双页面交错的回归测试。

用两个独立测试客户端模拟两个浏览器页面（各自持有自己看到的工作区修订号），
覆盖：
- 页面“仅改禁飞区 → 提交核准”：核准前后均不应多产生一次 PUT /api/state；
  审查冲突/审查错误时修订号不增加、草稿可改后再提；
- 已发布后航线自动保存携带 X-Expected-Workspace-Rev：他页已变更时被 409
  拒绝（不写库、不递增修订号、不覆盖他页已核准内容），同步最新修订号后
  重新提交成功；未携带该头时保持原“无条件保存”行为（未发布自动保存不回归）；
- 双页面交错：A 页核准成功后，B 页的航线保存被拒绝且不会用 B 页缓存的
  旧禁飞区覆盖 A 页已核准内容；B 页核准也必须使用最新修订号；
- 保存与核准相邻：保存先落库后，核准须用保存返回的新修订号（旧号 409）；
- 核准通过/失败均不改写发布快照。
"""
import contextlib
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# 干净航线：4km 直线（y=100），200m 等高，08:00 起飞
ROUTE = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 100, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 100, "alt": 200},
    ],
}
BLOCK_NFZ = {
    "id": "nz1", "name": "阻断区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}
FAR_NFZ = {
    "id": "nzFar", "name": "远侧限制区", "enabled": True,
    "vertices": [[100, 500], [200, 500], [200, 600], [100, 600]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}


def clone(obj):
    return json.loads(json.dumps(obj))


class WorkspaceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        # 两个独立客户端 ≈ 两个浏览器页面
        self.page_a = app.app.test_client()
        self.page_b = app.app.test_client()

    def tearDown(self):
        app.DB_PATH = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # —— 辅助 ——
    def get_state(self, client):
        r = client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        return r.get_json(), int(r.headers["X-Workspace-Rev"])

    def put(self, client, state, expected_rev=None):
        headers = {}
        if expected_rev is not None:
            headers["X-Expected-Workspace-Rev"] = str(expected_rev)
        return client.put("/api/state", json=state, headers=headers)

    def put_ok(self, client, state, expected_rev=None):
        r = self.put(client, state, expected_rev)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["rev"]

    def save_version(self, client, state, name="v1"):
        r = client.post("/api/versions", json={"name": name, "state": state})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def approve(self, client, nfzs, rev, seq):
        return client.post("/api/nfz-approval",
                           json={"nfzs": nfzs, "workspaceRev": rev, "releaseSeq": seq})

    def publish(self, client, vid, rev, seq):
        return client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": seq})

    def publish_route(self, client):
        """以 ROUTE 发布 #1（工作区禁飞区为空），返回 (vid, rev)。"""
        rev = self.put_ok(client, {"route": {"waypoints": []}, "nfzs": []})
        vid = self.save_version(client, {"route": ROUTE, "nfzs": []})
        r = self.publish(client, vid, rev, 0)
        self.assertEqual(r.status_code, 200, r.get_json())
        return vid, rev

    def release_snapshot(self, client):
        r = client.get("/api/release")
        self.assertEqual(r.status_code, 200)
        return r.get_json()["release"]

    # —— 条件 PUT：乐观并发 ——
    def test_conditional_put_rejects_stale_without_writing(self):
        rev = self.put_ok(self.page_a, {"route": {"waypoints": []}, "nfzs": []})
        # 他页先保存，修订号推进
        rev2 = self.put_ok(self.page_b, {"route": {"note": "他页内容"}, "nfzs": []})
        self.assertEqual(rev2, rev + 1)
        # 本页携带旧修订号保存 → 409，附明细，不写库、不递增
        r = self.put(self.page_a, {"route": {"note": "本页内容"}, "nfzs": []}, rev)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev2})
        state, hdr_rev = self.get_state(self.page_a)
        self.assertEqual(hdr_rev, rev2)
        self.assertEqual(state["route"], {"note": "他页内容"})

    def test_conditional_put_succeeds_with_fresh_rev(self):
        rev = self.put_ok(self.page_a, {"route": {"waypoints": []}, "nfzs": []})
        rev2 = self.put_ok(self.page_a, {"route": {"note": "x"}, "nfzs": []}, rev)
        self.assertEqual(rev2, rev + 1)

    def test_conditional_put_bad_header(self):
        r = self.page_a.put("/api/state", json={"route": {}, "nfzs": []},
                            headers={"X-Expected-Workspace-Rev": "abc"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")

    def test_unconditional_put_keeps_unpublished_autosave(self):
        # 不携带条件头时始终成功（未发布时的自动保存行为不回归）
        _, rev = self.get_state(self.page_a)
        self.put_ok(self.page_b, {"route": {"note": "他页"}, "nfzs": []})
        r2 = self.put(self.page_a, {"route": {"note": "本页"}, "nfzs": []})
        self.assertEqual(r2.status_code, 200)

    # —— 页面提交：仅改禁飞区不得预先 PUT ——
    def test_page_approval_only_does_not_pre_save(self):
        _, rev = self.publish_route(self.page_a)
        # 页面只改禁飞区草稿：不调用 PUT，直接提交核准
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["rev"], rev + 1)
        state, hdr_rev = self.get_state(self.page_a)
        self.assertEqual(hdr_rev, rev + 1)   # 只因核准本身 +1，无额外保存
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"], {"waypoints": []})

    def test_conflict_approval_bumps_nothing_and_keeps_draft(self):
        _, rev = self.publish_route(self.page_a)
        snap_before = self.release_snapshot(self.page_a)
        # 候选禁飞区压住发布快照航线 → 拒绝，修订号不变、草稿内容未落库
        r = self.approve(self.page_a, [clone(BLOCK_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertTrue(body["entries"])
        state, hdr_rev = self.get_state(self.page_a)
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])
        # 发布快照不动；草稿改后可再次提交
        self.assertEqual(self.release_snapshot(self.page_a), snap_before)
        r2 = self.approve(self.page_a, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r2.status_code, 200, r2.get_json())
        self.assertEqual(r2.get_json()["rev"], rev + 1)

    # —— 双页面交错：他页核准后本页保存不得覆盖已核准禁飞区 ——
    def test_two_page_interleave_approval_then_stale_save(self):
        _, rev = self.publish_route(self.page_a)
        # 两页都看到同一修订号
        _, rev_a = self.get_state(self.page_a)
        _, rev_b = self.get_state(self.page_b)
        self.assertEqual((rev_a, rev_b), (rev, rev))

        # A 页仅改禁飞区并核准成功：工作区禁飞区变为 FAR_NFZ，修订号 +1
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev_a, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_after = r.get_json()["rev"]
        self.assertEqual(rev_after, rev + 1)

        # B 页随后保存航线（按前端语义：携带旧修订号、带本页缓存的旧禁飞区 []）
        route_b = clone(ROUTE)
        route_b["waypoints"][0]["name"] = "B页改名"
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev_b)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev_after})
        # 关键：B 页缓存的旧禁飞区没有覆盖 A 页已核准内容
        state, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_after)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"], {"waypoints": []})

        # B 页同步最新修订号后重试保存：只落航线，禁飞区沿用已核准内容
        route_merged = clone(route_b)
        rev_b2 = self.put_ok(self.page_b,
                             {"route": route_merged, "nfzs": [clone(FAR_NFZ)]},
                             rev_after)
        state, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_b2)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"]["waypoints"][0]["name"], "B页改名")

        # B 页用旧修订号提交核准仍被拒，明细告知当前值；用新号则通过
        r = self.approve(self.page_b, [], rev, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev_b2})
        r = self.approve(self.page_b, [], rev_b2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.get_state(self.page_b)
        self.assertEqual(state["nfzs"], [])          # 新核准生效
        self.assertEqual(state["route"]["waypoints"][0]["name"], "B页改名")  # 航线保留

    def test_two_page_interleave_release_seq_change(self):
        vid, rev = self.publish_route(self.page_a)
        # A 页再次发布，发布号推进到 2（工作区修订号不变）
        r = self.publish(self.page_a, vid, rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        # B 页拿旧发布号提交核准 → 409 附 releaseSeq 明细，且修订号不增加
        r = self.approve(self.page_b, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["releaseSeq"],
                         {"expected": 1, "actual": 2})
        _, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev)

    # —— 保存与核准相邻：顺序与修订号协调 ——
    def test_adjacent_save_then_approval_uses_new_rev(self):
        _, rev = self.publish_route(self.page_a)
        # 先有一次航线自动保存落库（前端会等它完成），修订号推进
        route2 = clone(ROUTE)
        route2["waypoints"][0]["name"] = "自动保存改名"
        rev2 = self.put_ok(self.page_a,
                           {"route": route2, "nfzs": []}, rev)
        # 核准必须携带保存后的新修订号：旧号被拒、新号通过
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        # 核准通过：仅替换禁飞区，最新工作区航线保留
        state, hdr_rev = self.get_state(self.page_a)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"]["waypoints"][0]["name"], "自动保存改名")
        self.assertEqual(hdr_rev, rev2 + 1)

    def test_approval_never_touches_release_snapshot(self):
        _, rev = self.publish_route(self.page_a)
        snap_before = self.release_snapshot(self.page_a)
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.release_snapshot(self.page_a), snap_before)
        # 失败路径同样不动快照
        _, rev2 = self.get_state(self.page_a)
        r = self.approve(self.page_a, [clone(BLOCK_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.release_snapshot(self.page_a), snap_before)

    # —— 双页交错：两页各改航线（修复“409 后换新号整份覆盖且提示成功”）——
    def test_two_page_both_edit_route_no_silent_overwrite(self):
        # 发布 #1：发布快照航线为 ROUTE，发布时工作区航线为空、禁飞区为空
        _, rev = self.publish_route(self.page_a)
        # 发布后工作区航线先恢复为 ROUTE（两页随后都基于它编辑）
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        _, rev_a = self.get_state(self.page_a)
        _, rev_b = self.get_state(self.page_b)
        self.assertEqual(rev_a, rev_b)

        route_a = clone(ROUTE)
        route_a["waypoints"][0]["name"] = "A页航线"
        route_b = clone(ROUTE)
        route_b["waypoints"][1]["name"] = "B页航线"

        # A 页先保存成功
        rev_a2 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev_a)
        # B 页携带旧修订号保存 → 409，服务器航线仍是 A 页的，未被覆盖；
        # 关键：即使 B 页随后“换新号重试整份写入”，服务器也必须保留 A 页内容
        # 直到 B 页走“确认提交本页航线”的显式分流（旧前端曾在此直接覆盖）
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev_b)
        self.assertEqual(r.status_code, 409)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_a2)
        self.assertEqual(st["route"]["waypoints"][0]["name"], "A页航线")
        self.assertEqual(st["route"]["waypoints"][1]["name"], "B")
        self.assertEqual(st["nfzs"], [])

        # B 页取回最新内容并比对：服务器航线 ≠ 编辑前所见（双方都改了），
        # 本页航线与禁飞区草稿保留，暂停自动保存——不自动重试
        # （前端协议：只有在用户“确认提交本页航线”后才用最新修订号条件写入）
        rev_b_fresh = hdr_rev
        # 用户确认提交 B 页航线（禁飞区沿用已核准基线 []）→ 成功，服务器变为 B 页航线
        rev_b2 = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, rev_b_fresh)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_b2)
        self.assertEqual(st["route"]["waypoints"][0]["name"], "A")  # B 页未改首点
        self.assertEqual(st["route"]["waypoints"][1]["name"], "B页航线")

        # 发布快照始终是最初发布的 ROUTE，未被任何自动保存改写
        snap = self.release_snapshot(self.page_a)
        self.assertEqual([w["name"] for w in snap["state"]["route"]["waypoints"]],
                         ["A", "B"])
        self.assertEqual(snap["state"]["nfzs"], [])

    def test_two_page_route_conflict_then_second_409_keeps_local(self):
        # 确认提交后再遇 409：继续保留本页编辑，不覆盖、不提示成功
        _, rev = self.publish_route(self.page_a)
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        route_a = clone(ROUTE); route_a["waypoints"][0]["name"] = "A-1"
        route_b = clone(ROUTE); route_b["waypoints"][1]["name"] = "B-1"
        rev_a2 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev)

        # B 页用旧号 → 409；A 页又保存了一次（A-2），B 页用刚取回的号仍 409
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev)
        self.assertEqual(r.status_code, 409)
        route_a2 = clone(route_a); route_a2["waypoints"][0]["name"] = "A-2"
        rev_a3 = self.put_ok(self.page_a, {"route": route_a2, "nfzs": []}, rev_a2)
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev_a2)
        self.assertEqual(r.status_code, 409)
        # 服务器仍是 A 页最新航线；B 页内容从未写库
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_a3)
        self.assertEqual(st["route"]["waypoints"][0]["name"], "A-2")
        self.assertEqual(st["route"]["waypoints"][1]["name"], "B")
        # B 页最终用最新号确认提交 → 成功
        rev_b = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, rev_a3)
        st, _ = self.get_state(self.page_b)
        self.assertEqual([w["name"] for w in st["route"]["waypoints"]], ["A", "B-1"])
        self.assertEqual(rev_b, rev_a3 + 1)

    def test_two_page_nfz_draft_survives_route_conflict_and_approval(self):
        # 双方都改航线时，B 页还带着禁飞区草稿：冲突处理全程草稿不得落库；
        # B 页确认提交航线时请求体只含已核准禁飞区（此处为空），草稿仍在本页
        _, rev = self.publish_route(self.page_a)
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        route_a = clone(ROUTE); route_a["corridorHalfWidth"] = 0.5
        route_b = clone(ROUTE); route_b["unitScale"] = 200
        rev_a2 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev)
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev)
        self.assertEqual(r.status_code, 409)

        # B 页本地持有一个禁飞区草稿（模拟画布草稿，绝不随航线保存落库）
        # 确认提交航线：请求体禁飞区仍为已核准基线 []，草稿不在请求中
        rev_b = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, rev_a2)
        st, _ = self.get_state(self.page_b)
        self.assertEqual(st["nfzs"], [])                # 草稿未落库
        self.assertEqual(st["route"]["unitScale"], 200)  # B 页航线已保存

        # 随后 B 页提交草稿核准（FAR_NFZ 不压发布快照航线）→ 通过；
        # 工作区航线保留为 B 页航线，发布快照航线不变
        r = self.approve(self.page_b, [clone(FAR_NFZ)], rev_b, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(st["nfzs"], [FAR_NFZ])
        self.assertEqual(st["route"]["unitScale"], 200)
        snap = self.release_snapshot(self.page_b)
        self.assertEqual(snap["state"]["nfzs"], [])
        self.assertEqual(snap["state"]["route"]["unitScale"], 100)

    def test_adjacent_approval_after_route_conflict_must_use_latest_rev(self):
        # 保存定时器/在途请求与禁飞区核准相邻：航线冲突未解决时，
        # 不得拿旧修订号把核准做成；用旧号必 409，最新号通过且快照不动
        _, rev = self.publish_route(self.page_a)
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        route_a = clone(ROUTE); route_a["waypoints"][0]["alt"] = 300
        route_b = clone(ROUTE); route_b["waypoints"][1]["alt"] = 300
        rev_a2 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev)
        # B 页在途保存撞 409（双方航线冲突，前端此刻暂停，不继续核准）
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev)
        self.assertEqual(r.status_code, 409)
        # 若仍拿旧号提交核准 → 409 stale，工作区/发布均不变
        r = self.approve(self.page_b, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "stale")
        snap = self.release_snapshot(self.page_b)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_a2)
        self.assertEqual(st["route"]["waypoints"][0]["alt"], 300)  # A 页航线保留
        # B 页解决冲突（确认提交本页航线）后，再用最新号核准 → 通过
        rev_b = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, rev_a2)
        r = self.approve(self.page_b, [clone(FAR_NFZ)], rev_b, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        st, _ = self.get_state(self.page_b)
        self.assertEqual(st["nfzs"], [FAR_NFZ])
        self.assertEqual(st["route"]["waypoints"][1]["alt"], 300)
        # 自动保存/核准全程不改写发布快照
        self.assertEqual(self.release_snapshot(self.page_b), snap)

    # —— 启动交错：GET /api/state →（他页改航线/核准禁飞区、推进修订号）→ GET /api/release ——
    def _setup_published_route_for_startup(self):
        """发布 #1（快照航线 ROUTE），随后把工作区航线恢复为 ROUTE。
        返回 (发布时修订号, 启动页 GET /api/state 所见修订号)。"""
        _, rev = self.publish_route(self.page_a)
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        st, seen = self.get_state(self.page_b)
        self.assertEqual(st["route"], ROUTE)
        return rev, seen

    def _broken_state_client(self):
        """返回一个 GET /api/state 失败（模拟协调取回失败）的页面客户端。"""
        client = app.app.test_client()
        client.raise_server_exceptions = False
        original_get_db = app.get_db

        def broken_get_db():
            raise RuntimeError("simulated GET /api/state failure")

        app.get_db = broken_get_db
        return client, original_get_db

    def _restore_get_db(self, original_get_db):
        app.get_db = original_get_db

    @contextlib.contextmanager
    def _silence_server_logs(self):
        """屏蔽模拟 500 时 Flask 打印的服务端 traceback（响应本身仍为 500）。"""
        loggers = [logging.getLogger("flask.app"), logging.getLogger("werkzeug"),
                   app.app.logger]
        saved = [lg.level for lg in loggers]
        for lg in loggers:
            lg.setLevel(logging.CRITICAL)
        try:
            yield
        finally:
            for lg, lvl in zip(loggers, saved):
                lg.level = lvl

    def test_startup_interleave_remote_only_route_change_is_adopted(self):
        # B 页先 GET /api/state（rev=seen）；其间 A 页改航线并保存（rev=seen+1）；
        # B 页随后 GET /api/release 发现修订号已推进、发布存在。
        # 协议：B 页协调取回最新工作区后，仅他页改航线（本页航线未改）→
        # 直接采用远端航线、不发起任何 PUT；协调 PUT 若误用“新修订号 + 旧航线”
        # 会静默回滚 A 页航线——本测试钉死该写回必须不发生。
        _, seen = self._setup_published_route_for_startup()
        route_a = clone(ROUTE)
        route_a["waypoints"][0]["name"] = "A页启动交错改名"
        rev_after = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, seen)
        self.assertEqual(rev_after, seen + 1)

        # B 页读 /api/release：确认发布存在且工作区修订号已变
        rel = self.page_b.get("/api/release").get_json()
        self.assertEqual(rel["workspaceRev"], rev_after)
        self.assertIsNotNone(rel["release"])
        # B 页协调取回最新工作区（前端 GET /api/state）
        fresh, fresh_rev = self.get_state(self.page_b)
        self.assertEqual(fresh_rev, rev_after)
        # 本页航线（编辑前所见）与远端航线比对：本页未改、远端改了 → 采用远端，
        # 不写库。模拟前端语义：绝不 PUT。
        self.assertEqual(fresh["route"]["waypoints"][0]["name"], "A页启动交错改名")
        # 反面钉死：用 /api/release 的新修订号配 GET /api/state 所见旧航线写回，
        # 正是要修复的事故——即便这样发了 PUT，服务端条件头也必须拦住：
        # 前端此时持有的条件号若仍是 seen，则 409；服务器航线保持 A 页内容
        r = self.put(self.page_b, {"route": clone(ROUTE), "nfzs": []}, seen)
        self.assertEqual(r.status_code, 409)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_after)
        self.assertEqual(st["route"]["waypoints"][0]["name"], "A页启动交错改名")

    def test_startup_interleave_both_routes_conflict_no_auto_write(self):
        # 两页在启动交错窗口内各自改了航线：B 页取回最新工作区后进入冲突分流，
        # 自动保存暂停——不自动写库；只有用户显式“确认提交本页航线”（最新号
        # 条件 PUT）才落库，期间禁飞区草稿同样不落库。
        _, seen = self._setup_published_route_for_startup()
        route_b = clone(ROUTE)
        route_b["waypoints"][1]["name"] = "B页启动航线"
        route_a = clone(ROUTE)
        route_a["waypoints"][0]["name"] = "A页启动航线"
        rev_after = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, seen)

        fresh, fresh_rev = self.get_state(self.page_b)
        self.assertEqual(fresh_rev, rev_after)
        # 前端判定：localChanged(route_b≠ROUTE) 且 remoteChanged(A页航线≠ROUTE)
        # → 冲突分流，不自动 PUT。服务器仍只有 A 页航线
        st, _ = self.get_state(self.page_b)
        self.assertEqual(st["route"]["waypoints"][0]["name"], "A页启动航线")
        self.assertEqual(st["route"]["waypoints"][1]["name"], "B")
        # 旧号条件写必被拒
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, seen)
        self.assertEqual(r.status_code, 409)
        # 显式确认提交（最新号）→ 成功，B 页航线落库
        rev_b = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, fresh_rev)
        st, _ = self.get_state(self.page_b)
        self.assertEqual([w["name"] for w in st["route"]["waypoints"]],
                         ["A", "B页启动航线"])
        self.assertEqual(rev_b, rev_after + 1)
        # 发布快照航线始终是 ROUTE
        snap = self.release_snapshot(self.page_b)
        self.assertEqual([w["name"] for w in snap["state"]["route"]["waypoints"]],
                         ["A", "B"])

    def test_startup_interleave_remote_approval_local_route_conditional_save(self):
        # 交错窗口内他页仅核准禁飞区（远端航线未变），本页改了航线：
        # 协调合入已核准禁飞区基线（本页禁飞区草稿不落库），用最新修订号
        # 条件保存本页航线，请求体只带已核准禁飞区。
        _, seen = self._setup_published_route_for_startup()
        # A 页核准远侧禁飞区：修订号推进，航线不变
        r = self.approve(self.page_a, [clone(FAR_NFZ)], seen, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_after = r.get_json()["rev"]

        # B 页（启动中）持有本页航线编辑与一个禁飞区草稿
        route_b = clone(ROUTE)
        route_b["corridorHalfWidth"] = 0.42
        draft_nfz = clone(BLOCK_NFZ)
        draft_nfz["id"] = "draftOnly"

        rel = self.page_b.get("/api/release").get_json()
        self.assertEqual(rel["workspaceRev"], rev_after)
        fresh, fresh_rev = self.get_state(self.page_b)
        self.assertEqual(fresh_rev, rev_after)
        self.assertEqual(fresh["route"], ROUTE)          # 远端航线未变
        self.assertEqual(fresh["nfzs"], [FAR_NFZ])       # 他页已核准禁飞区
        # 合入后：本页航线用最新号条件保存，请求体禁飞区为已核准基线 FAR_NFZ，
        # 本页草稿 BLOCK 禁飞区不在请求中
        rev_b = self.put_ok(self.page_b,
                            {"route": route_b, "nfzs": [clone(FAR_NFZ)]},
                            fresh_rev)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_b)
        self.assertEqual(st["route"]["corridorHalfWidth"], 0.42)
        self.assertEqual(st["nfzs"], [FAR_NFZ])          # 草稿绝未落库

    def test_startup_reconcile_fetch_failure_blocks_pending_save_until_retry(self):
        # 交错窗口内他页核准禁飞区并推进修订号；B 页启动协调所需的再次
        # GET /api/state 失败：必须保留本页航线/禁飞区草稿、不放行待发保存，
        # 绝不拿 /api/release 的新修订号把旧航线+旧禁飞区整份写回；
        # 重试取回最新工作区后，才按最新修订号条件保存本页航线（合入已核准禁飞区）。
        _, seen = self._setup_published_route_for_startup()
        route_b = clone(ROUTE)
        route_b["unitScale"] = 120
        # A 页在 B 页两次读取之间核准禁飞区
        r = self.approve(self.page_a, [clone(FAR_NFZ)], seen, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_after = r.get_json()["rev"]

        # B 页 GET /api/release 成功：发布存在、修订号已推进
        rel = self.page_b.get("/api/release").get_json()
        self.assertEqual(rel["workspaceRev"], rev_after)
        # 随后协调 GET /api/state 失败（网络/500）
        broken, original_get_db = self._broken_state_client()
        try:
            with self._silence_server_logs():
                r = broken.get("/api/state")
            self.assertEqual(r.status_code, 500)
        finally:
            self._restore_get_db(original_get_db)

        # 协调失败期间：服务端工作区保持 A 页已核准状态，B 页不得写入
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_after)
        self.assertEqual(st["nfzs"], [FAR_NFZ])
        self.assertEqual(st["route"], ROUTE)
        # 前端持有的条件号仍是 GET /api/state 所见的 seen：任何误发的条件 PUT
        # 都被 409 拒绝，旧航线/旧禁飞区整份写回不可能落库
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, seen)
        self.assertEqual(r.status_code, 409)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(st["nfzs"], [FAR_NFZ])
        self.assertEqual(st["route"], ROUTE)
        self.assertEqual(hdr_rev, rev_after)
        # 未核准/发布相邻写入同样被门控（前端不发请求）：这里钉死服务端侧
        # 旧号核准必 409，且即便通过了并发检查，快照航线也不被改写
        r = self.approve(self.page_b, [], seen, 1)
        self.assertEqual(r.status_code, 409)

        # 重试：GET /api/state 恢复，B 页取回最新工作区（合入 FAR_NFZ 基线），
        # 本页航线按最新修订号条件保存，草稿（此处无）不落库
        fresh, fresh_rev = self.get_state(self.page_b)
        self.assertEqual(fresh["nfzs"], [FAR_NFZ])
        rev_b = self.put_ok(self.page_b,
                            {"route": route_b, "nfzs": [clone(FAR_NFZ)]},
                            fresh_rev)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_b)
        self.assertEqual(st["route"]["unitScale"], 120)
        self.assertEqual(st["nfzs"], [FAR_NFZ])
        # 发布快照航线不变
        snap = self.release_snapshot(self.page_b)
        self.assertEqual(snap["state"]["route"], ROUTE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
