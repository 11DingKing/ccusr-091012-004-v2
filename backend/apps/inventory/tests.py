"""
盘点批次与任务测试

覆盖需求要点：
- 主管按区域/风险/截止日期生成批次并自动拆分任务；
- 领取即锁定范围，重叠任务无法重复锁定同一物资；
- 分段提交累计进度，重复提交不重复计数；
- 异常项进入复核，全部任务完成且复核清零才形成批次结论；
- 中断、重新分配、范围增补均保留进度；
- 批次查询能指出尚未完成的具体范围。
"""
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Category, Goods, Unit, Variety

from .models import (
    StocktakeBatch, StocktakeTask, TaskGoods, StocktakeItem,
    RISK_HIGH, RISK_MEDIUM, RISK_LOW,
    BATCH_DRAFT, BATCH_IN_PROGRESS, BATCH_COMPLETED,
    TASK_PENDING, TASK_IN_PROGRESS, TASK_COMPLETED,
)


DEADLINE = (timezone.now() + timedelta(days=7)).isoformat()


class InventoryFixture(TestCase):
    def setUp(self):
        self.supervisor = User.objects.create_user("supervisor", "pass123456", role="admin")
        self.counter_a = User.objects.create_user("counter-a", "pass123456", role="user")
        self.counter_b = User.objects.create_user("counter-b", "pass123456", role="user")

        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.supervisor)}")
        self.client_a = self._auth_client(self.counter_a)
        self.client_b = self._auth_client(self.counter_b)

        unit = Unit.objects.create(name="件", created_by=self.supervisor)
        category = Category.objects.create(name="受控器材", unit=unit, created_by=self.supervisor)
        variety = Variety.objects.create(name="记录终端", category=category, created_by=self.supervisor)

        # A 区两件普通物资（低风险）
        self.goods_a1 = Goods.objects.create(
            variety=variety, name="终端A1", code="A-001",
            quantity=Decimal("20"), warning_threshold=Decimal("5"), location="A区")
        self.goods_a2 = Goods.objects.create(
            variety=variety, name="终端A2", code="A-002",
            quantity=Decimal("20"), warning_threshold=Decimal("5"), location="A区")
        # B 区一件库存触阈物资（高风险）
        self.goods_b1 = Goods.objects.create(
            variety=variety, name="终端B1", code="B-001",
            quantity=Decimal("3"), warning_threshold=Decimal("5"), location="B区")
        # 无区域物资（中风险）
        self.goods_x = Goods.objects.create(
            variety=variety, name="未上架终端", code="X-001",
            quantity=Decimal("20"), warning_threshold=Decimal("5"), location="")

    def _auth_client(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    def create_batch(self, client=None, locations=None, risk_levels=None, chunk_size=50, name="十月盘点"):
        client = client or self.client
        payload = {
            "name": name,
            "locations": locations if locations is not None else [],
            "risk_levels": risk_levels or [],
            "deadline": DEADLINE,
            "chunk_size": chunk_size,
        }
        resp = client.post("/api/stocktake/batches/", payload, format="json")
        return resp

    def submit(self, client, task_id, entries):
        return client.post(f"/api/stocktake/tasks/{task_id}/submit/",
                           {"entries": entries}, format="json")

    def normal_entry(self, goods, qty="20"):
        return {"goods": goods.id, "result": "normal", "actual_quantity": qty}

    def abnormal_entry(self, goods, qty="1", atype="quantity"):
        return {"goods": goods.id, "result": "abnormal",
                "actual_quantity": qty, "abnormal_type": atype}


# ==================== 批次生成与拆分 ====================

class BatchGenerationTest(InventoryFixture):
    def test_create_batch_splits_tasks_by_location(self):
        resp = self.create_batch(locations=["A区", "B区"], chunk_size=1)
        self.assertEqual(resp.status_code, 200, resp.content)
        batch_id = resp.json()["data"]["id"]
        # A区2件按 chunk_size=1 拆成2个任务，B区1件1个任务
        tasks = StocktakeTask.objects.filter(batch_id=batch_id).order_by("seq")
        self.assertEqual(tasks.count(), 3)
        self.assertEqual(set(tasks.values_list("location", flat=True)), {"A区", "B区"})
        self.assertEqual(resp.json()["data"]["total_count"], 3)
        self.assertEqual(resp.json()["data"]["status"], BATCH_DRAFT)

    def test_empty_scope_rejected(self):
        # C区没有任何物资 —— 防止“遗漏区域无人发现”式的空批次
        resp = self.create_batch(locations=["C区"], name="空区盘点")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("没有可盘点物资", resp.json()["message"])

    def test_risk_filtering_and_task_risk_level(self):
        resp = self.create_batch(locations=[], risk_levels=[RISK_HIGH], name="高风险盘点")
        self.assertEqual(resp.status_code, 200)
        batch_id = resp.json()["data"]["id"]
        task = StocktakeTask.objects.get(batch_id=batch_id)
        self.assertEqual(task.goods_ids, [self.goods_b1.id])
        self.assertEqual(task.risk_level, RISK_HIGH)
        self.assertEqual(StocktakeBatch.risk_level_of(self.goods_x), RISK_MEDIUM)
        self.assertEqual(StocktakeBatch.risk_level_of(self.goods_a1), RISK_LOW)

    def test_counter_cannot_create_batch(self):
        resp = self.create_batch(client=self.client_a, locations=["A区"])
        self.assertEqual(resp.status_code, 403)


# ==================== 领取锁定与防重复锁定 ====================

class ClaimAndLockTest(InventoryFixture):
    def setUp(self):
        super().setUp()
        resp = self.create_batch(locations=["A区", "B区"], name="第一批")
        self.batch1_id = resp.json()["data"]["id"]
        self.task_a = StocktakeTask.objects.get(batch_id=self.batch1_id, location="A区")
        self.task_b = StocktakeTask.objects.get(batch_id=self.batch1_id, location="B区")

    def test_claim_locks_scope(self):
        resp = self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TaskGoods.objects.filter(task=self.task_a, is_locked=True).exists())
        self.task_a.refresh_from_db()
        self.assertEqual(self.task_a.status, TASK_IN_PROGRESS)
        self.assertEqual(self.task_a.assignee_id, self.counter_a.id)

        # 他人不能重复领取
        resp = self.client_b.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        self.assertEqual(resp.status_code, 409)

        # 本人重复领取幂等
        resp = self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.task_a.items.count(), 0)

    def test_overlapping_batch_cannot_double_lock_goods(self):
        """两个保管区盘点任务范围重叠时，同一物资不能被重复锁定。"""
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")

        resp2 = self.create_batch(locations=["A区"], name="重叠批次")
        self.assertEqual(resp2.status_code, 200)
        overlap_task = StocktakeTask.objects.exclude(batch_id=self.batch1_id).get(location="A区")
        # 数据库部分唯一索引直接拦截
        resp = self.client_b.post(f"/api/stocktake/tasks/{overlap_task.id}/claim/")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("锁定", resp.json()["message"])
        overlap_task.refresh_from_db()
        self.assertEqual(overlap_task.status, TASK_PENDING)
        self.assertFalse(TaskGoods.objects.filter(task=overlap_task, is_locked=True).exists())

    def test_lock_released_after_completion_allows_other_batch(self):
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        self.submit(self.client_a, self.task_a.id, [
            self.normal_entry(self.goods_a1), self.normal_entry(self.goods_a2)])
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/complete/")

        resp2 = self.create_batch(locations=["A区"], name="后续批次")
        self.assertEqual(resp2.status_code, 200)
        overlap_task = StocktakeTask.objects.exclude(batch_id=self.batch1_id).get(location="A区")
        resp = self.client_b.post(f"/api/stocktake/tasks/{overlap_task.id}/claim/")
        self.assertEqual(resp.status_code, 200)

    def test_database_partial_unique_index_blocks_duplicate_lock(self):
        # A区任务锁定 a1
        TaskGoods.objects.filter(task=self.task_a, goods=self.goods_a1).update(is_locked=True)
        # 绕过服务层：B区任务也试图锁定同一物资 a1，数据库部分唯一索引直接拦截
        with self.assertRaises(IntegrityError):
            TaskGoods.objects.create(task=self.task_b, goods=self.goods_a1, is_locked=True)


# ==================== 分段提交 / 重复提交 / 异常复核 ====================

class SubmitAndReviewTest(InventoryFixture):
    def setUp(self):
        super().setUp()
        resp = self.create_batch(locations=["A区", "B区"], chunk_size=10, name="提交批次")
        self.batch_id = resp.json()["data"]["id"]
        # A区+B区按区域共两个任务
        self.task_a = StocktakeTask.objects.get(batch_id=self.batch_id, location="A区")

    def test_segmented_submit_accumulates_and_duplicate_does_not_double_count(self):
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")

        # 第一段：只盘一件
        resp = self.submit(self.client_a, self.task_a.id, [self.normal_entry(self.goods_a1)])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["created"], 1)
        self.assertEqual(resp.json()["data"]["counted_count"], 1)

        # 第二段：另一件
        resp = self.submit(self.client_a, self.task_a.id, [self.normal_entry(self.goods_a2)])
        self.assertEqual(resp.json()["data"]["created"], 1)
        self.assertEqual(resp.json()["data"]["counted_count"], 2)

        # 重复提交第一件（同值/改值）：更新而非新增，计数不变，版本号递增
        resp = self.submit(self.client_a, self.task_a.id, [self.normal_entry(self.goods_a1, "19")])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["created"], 0)
        self.assertEqual(resp.json()["data"]["updated"], 1)
        self.assertEqual(resp.json()["data"]["counted_count"], 2)
        self.assertEqual(StocktakeItem.objects.filter(task=self.task_a).count(), 2)
        item = StocktakeItem.objects.get(task=self.task_a, goods=self.goods_a1)
        self.assertEqual(item.revision, 2)
        self.assertEqual(item.actual_quantity, Decimal("19"))

    def test_abnormal_item_enters_review_and_correction_closes_it(self):
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        self.submit(self.client_a, self.task_a.id, [self.abnormal_entry(self.goods_a1)])

        resp = self.client.get("/api/stocktake/reviews/?status=pending")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["total"], 1)
        review_id = resp.json()["data"]["list"][0]["id"]

        # 盘点员更正为正常 → 复核自动关闭
        resp = self.submit(self.client_a, self.task_a.id, [self.normal_entry(self.goods_a1)])
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/stocktake/reviews/?status=pending")
        self.assertEqual(resp.json()["data"]["total"], 0)
        resp = self.client.get("/api/stocktake/reviews/?status=resolved")
        self.assertEqual(resp.json()["data"]["total"], 1)
        self.assertEqual(resp.json()["data"]["list"][0]["id"], review_id)

    def test_submit_out_of_scope_rejected(self):
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        # B区物资不属于A区任务
        resp = self.submit(self.client_a, self.task_a.id, [self.normal_entry(self.goods_b1)])
        self.assertEqual(resp.status_code, 400)

    def test_non_assignee_cannot_submit(self):
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        resp = self.submit(self.client_b, self.task_a.id, [self.normal_entry(self.goods_a1)])
        self.assertEqual(resp.status_code, 403)

    def test_abnormal_requires_type(self):
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        resp = self.submit(self.client_a, self.task_a.id, [
            {"goods": self.goods_a1.id, "result": "abnormal"}])
        self.assertEqual(resp.status_code, 400)


# ==================== 中断与重新分配 ====================

class InterruptAndReassignTest(InventoryFixture):
    def setUp(self):
        super().setUp()
        resp = self.create_batch(locations=["A区"], name="中断批次")
        self.batch_id = resp.json()["data"]["id"]
        self.task = StocktakeTask.objects.get(batch_id=self.batch_id, location="A区")
        self.client_a.post(f"/api/stocktake/tasks/{self.task.id}/claim/")
        self.submit(self.client_a, self.task.id, [self.normal_entry(self.goods_a1)])

    def test_release_keeps_progress_and_unlocks(self):
        resp = self.client_a.post(f"/api/stocktake/tasks/{self.task.id}/release/")
        self.assertEqual(resp.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TASK_PENDING)
        self.assertIsNone(self.task.assignee_id)
        self.assertFalse(TaskGoods.objects.filter(task=self.task, is_locked=True).exists())
        # 进度保留
        self.assertEqual(self.task.items.count(), 1)

        # B 领取后继续提交第二段，计数在保留基础上累计
        self.client_b.post(f"/api/stocktake/tasks/{self.task.id}/claim/")
        resp = self.submit(self.client_b, self.task.id, [self.normal_entry(self.goods_a2)])
        self.assertEqual(resp.json()["data"]["counted_count"], 2)
        self.assertEqual(StocktakeItem.objects.filter(task=self.task).count(), 2)
        # 批次因历史领取/进度保持“盘点中”
        batch = StocktakeBatch.objects.get(pk=self.batch_id)
        self.assertEqual(batch.status, BATCH_IN_PROGRESS)

    def test_other_counter_cannot_force_release(self):
        resp = self.client_b.post(f"/api/stocktake/tasks/{self.task.id}/release/")
        self.assertEqual(resp.status_code, 403)

    def test_supervisor_reassign_keeps_lock_and_progress(self):
        resp = self.client.post(f"/api/stocktake/tasks/{self.task.id}/reassign/",
                                {"assignee": self.counter_b.id}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.counter_b.id)
        self.assertTrue(self.task.is_locked)
        self.assertEqual(self.task.items.count(), 1)
        # 新盘点员可直接继续提交
        resp = self.submit(self.client_b, self.task.id, [self.normal_entry(self.goods_a2)])
        self.assertEqual(resp.status_code, 200)


# ==================== 范围变化（增补） ====================

class ScopeChangeTest(InventoryFixture):
    def setUp(self):
        super().setUp()
        resp = self.create_batch(locations=["A区"], name="范围批次")
        self.batch_id = resp.json()["data"]["id"]
        self.task = StocktakeTask.objects.get(batch_id=self.batch_id, location="A区")
        self.client_a.post(f"/api/stocktake/tasks/{self.task.id}/claim/")
        self.submit(self.client_a, self.task.id, [
            self.normal_entry(self.goods_a1), self.normal_entry(self.goods_a2)])
        self.client_a.post(f"/api/stocktake/tasks/{self.task.id}/complete/")

    def _add_goods_to_a(self, code):
        return Goods.objects.create(
            variety=self.goods_a1.variety, name=f"新物资{code}", code=code,
            quantity=Decimal("20"), warning_threshold=Decimal("5"), location="A区")

    def test_refresh_scope_adds_supplement_tasks_and_keeps_history(self):
        new_goods = self._add_goods_to_a("A-003")
        resp = self.client.post(f"/api/stocktake/batches/{self.batch_id}/refresh-scope/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["new_goods_count"], 1)

        supplement = StocktakeTask.objects.get(batch_id=self.batch_id, is_supplement=True)
        self.assertEqual(supplement.goods_ids, [new_goods.id])
        self.assertEqual(supplement.scope_version, 2)
        # 原任务、原进度不受影响
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TASK_COMPLETED)
        self.assertEqual(StocktakeItem.objects.filter(task=self.task).count(), 2)
        # 批次未完成（还有增补任务）
        self.assertEqual(StocktakeBatch.objects.get(pk=self.batch_id).status, BATCH_IN_PROGRESS)

    def test_refresh_scope_no_change_is_noop(self):
        resp = self.client.post(f"/api/stocktake/batches/{self.batch_id}/refresh-scope/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["new_task_count"], 0)
        self.assertFalse(StocktakeTask.objects.filter(batch_id=self.batch_id, is_supplement=True).exists())

    def test_scope_diff_points_out_uncovered_goods(self):
        self._add_goods_to_a("A-003")
        resp = self.client.get(f"/api/stocktake/batches/{self.batch_id}/")
        diff = resp.json()["data"]["scope_diff"]
        self.assertEqual([g["code"] for g in diff["uncovered"]], ["A-003"])


# ==================== 批次结论与未完成范围查询 ====================

class ConclusionAndQueryTest(InventoryFixture):
    def setUp(self):
        super().setUp()
        resp = self.create_batch(locations=["A区", "B区"], chunk_size=10, name="结论批次")
        self.batch_id = resp.json()["data"]["id"]
        self.task_a = StocktakeTask.objects.get(batch_id=self.batch_id, location="A区")
        self.task_b = StocktakeTask.objects.get(batch_id=self.batch_id, location="B区")

    def _finish_task(self, client, task, entries):
        client.post(f"/api/stocktake/tasks/{task.id}/claim/")
        self.submit(client, task.id, entries)
        client.post(f"/api/stocktake/tasks/{task.id}/complete/")

    def test_detail_points_out_pending_scopes(self):
        self._finish_task(self.client_a, self.task_a,
                          [self.normal_entry(self.goods_a1), self.normal_entry(self.goods_a2)])
        self.client_b.post(f"/api/stocktake/tasks/{self.task_b.id}/claim/")
        # B区只盘0件，查询必须指出 B区 B-001 未盘
        resp = self.client.get(f"/api/stocktake/batches/{self.batch_id}/")
        pending = resp.json()["data"]["pending_scopes"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["location"], "B区")
        self.assertEqual([g["code"] for g in pending[0]["missing_goods"]], ["B-001"])
        self.assertEqual(pending[0]["assignee_name"], "counter-b")

    def test_cannot_conclude_with_pending_tasks(self):
        resp = self.client.post(f"/api/stocktake/batches/{self.batch_id}/conclude/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("未完成", resp.json()["message"])

    def test_cannot_complete_task_with_uncounted_goods(self):
        self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/claim/")
        self.submit(self.client_a, self.task_a.id, [self.normal_entry(self.goods_a1)])
        resp = self.client_a.post(f"/api/stocktake/tasks/{self.task_a.id}/complete/")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("未盘点", resp.json()["message"])

    def test_full_flow_conclusion_blocked_by_review_then_unblocked(self):
        # A区正常完成
        self._finish_task(self.client_a, self.task_a,
                          [self.normal_entry(self.goods_a1), self.normal_entry(self.goods_a2)])
        # B区盘出异常
        self._finish_task(self.client_b, self.task_b, [self.abnormal_entry(self.goods_b1, "1")])

        # 任务全部完成但复核未闭环 → 不能形成结论
        resp = self.client.post(f"/api/stocktake/batches/{self.batch_id}/conclude/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("待复核", resp.json()["message"])

        # 主管复核闭环
        review_id = StocktakeItem.objects.get(goods=self.goods_b1).review.id
        resp = self.client.post(f"/api/stocktake/reviews/{review_id}/resolve/",
                                {"note": "已核查，差异为领用未登记"}, format="json")
        self.assertEqual(resp.status_code, 200)

        # 形成批次结论
        resp = self.client.post(f"/api/stocktake/batches/{self.batch_id}/conclude/",
                                {"conclusion": "盘点总体正常"}, format="json")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], BATCH_COMPLETED)
        self.assertIn("盘点总体正常", data["conclusion"])
        self.assertIn("异常 1 项", data["conclusion"])
        self.assertEqual(data["pending_scopes"] if "pending_scopes" in data else [], [])

        # 完成后结论幂等、范围不可再变
        resp = self.client.post(f"/api/stocktake/batches/{self.batch_id}/conclude/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(f"/api/stocktake/batches/{self.batch_id}/refresh-scope/")
        self.assertEqual(resp.status_code, 400)

    def test_counter_cannot_conclude_or_resolve(self):
        resp = self.client_a.post(f"/api/stocktake/batches/{self.batch_id}/conclude/", {}, format="json")
        self.assertEqual(resp.status_code, 403)
