from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Category, Goods, Unit, Variety
from .models import (
    GoodsLock, InventoryBatch, InventoryRecord, InventoryReview,
    InventorySubmission, InventoryTask, InventoryTaskItem, StorageArea,
)

DEADLINE = (timezone.localdate() + timedelta(days=7)).isoformat()


class InventoryFixture(TestCase):
    """主管 + 两名盘点员 + 三个保管区（高/中/低风险）的基础数据"""

    def setUp(self):
        self.admin = User.objects.create_user("supervisor", "testpass123", role="admin")
        self.counter1 = User.objects.create_user("counter-one", "testpass123", role="user")
        self.counter2 = User.objects.create_user("counter-two", "testpass123", role="user")

        self.admin_client = APIClient()
        self.admin_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.admin)}")
        self.client1 = APIClient()
        self.client1.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.counter1)}")
        self.client2 = APIClient()
        self.client2.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.counter2)}")

        self.area_high = StorageArea.objects.create(name="证物一区", code="A1", risk_level="high")
        self.area_mid = StorageArea.objects.create(name="证物二区", code="A2", risk_level="medium")
        self.area_low = StorageArea.objects.create(name="耗材区", code="B1", risk_level="low")

        unit = Unit.objects.create(name="件", created_by=self.admin)
        category = Category.objects.create(name="受控器材", unit=unit, created_by=self.admin)
        variety = Variety.objects.create(name="记录终端", category=category, created_by=self.admin)

        def make_goods(code, area, qty):
            return Goods.objects.create(
                variety=variety, name=f"物资{code}", code=code,
                quantity=Decimal(qty), area=area,
            )

        self.goods_a1 = make_goods("G-A1", self.area_high, "10")
        self.goods_a2 = make_goods("G-A2", self.area_high, "5")
        self.goods_b1 = make_goods("G-B1", self.area_mid, "8")
        self.goods_c1 = make_goods("G-C1", self.area_low, "3")

    def create_batch(self, client=None, **overrides):
        client = client or self.admin_client
        payload = {"title": "月度盘点", "deadline": DEADLINE}
        payload.update(overrides)
        return client.post("/api/inventory/batches/", payload, format="json")

    def claim(self, client, batch_json, area_code):
        task = next(
            t for t in InventoryTask.objects.filter(batch_id=batch_json["batch"]["id"])
            if t.area.code == area_code
        )
        response = client.post(f"/api/inventory/tasks/{task.id}/claim/")
        return task, response

    def submit(self, client, task_id, request_id, entries):
        return client.post(
            f"/api/inventory/tasks/{task_id}/submit/",
            {"request_id": request_id, "items": entries},
            format="json",
        )

    @staticmethod
    def item_of(task, goods):
        return InventoryTaskItem.objects.get(task=task, goods=goods)


class StorageAreaAPITest(InventoryFixture):
    def test_area_crud_and_validation(self):
        created = self.admin_client.post(
            "/api/inventory/areas/",
            {"name": "暂存区", "code": "C1", "risk_level": "high"},
            format="json",
        )
        self.assertEqual(created.status_code, 200)
        duplicate = self.admin_client.post(
            "/api/inventory/areas/",
            {"name": "暂存区", "code": "C2", "risk_level": "low"},
            format="json",
        )
        self.assertEqual(duplicate.status_code, 400)

        listed = self.client1.get("/api/inventory/areas/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["data"]["total"], 4)

    def test_area_manage_requires_admin(self):
        denied = self.client1.post(
            "/api/inventory/areas/",
            {"name": "私设区", "code": "X1", "risk_level": "low"},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)

    def test_refuse_delete_linked_area(self):
        response = self.admin_client.delete(f"/api/inventory/areas/{self.area_high.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(StorageArea.objects.filter(pk=self.area_high.id).exists())


class BatchCreateTest(InventoryFixture):
    def test_split_tasks_by_area_and_snapshot_scope(self):
        response = self.create_batch()
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        batch = InventoryBatch.objects.get(pk=data["batch"]["id"])

        # 每个启用保管区拆出一个任务
        self.assertEqual(batch.tasks.count(), 3)
        self.assertEqual(
            set(batch.tasks.values_list("area__code", flat=True)),
            {"A1", "A2", "B1"},
        )
        # 任务明细快照账面数量
        task_a1 = batch.tasks.get(area=self.area_high)
        self.assertEqual(task_a1.items.count(), 2)
        item = task_a1.items.get(goods=self.goods_a1)
        self.assertEqual(item.book_quantity, Decimal("10.00"))
        self.assertEqual(item.status, "pending")
        # 批次号与任务号
        self.assertTrue(batch.batch_no.startswith("PD"))
        self.assertTrue(task_a1.task_no.startswith(batch.batch_no))

    def test_filter_by_risk_level(self):
        response = self.create_batch(risk_levels=["high"])
        self.assertEqual(response.status_code, 200)
        batch = InventoryBatch.objects.get(pk=response.json()["data"]["batch"]["id"])
        self.assertEqual(batch.tasks.count(), 1)
        self.assertEqual(batch.tasks.get().area, self.area_high)

    def test_filter_by_area_ids(self):
        response = self.create_batch(area_ids=[self.area_mid.id])
        self.assertEqual(response.status_code, 200)
        batch = InventoryBatch.objects.get(pk=response.json()["data"]["batch"]["id"])
        self.assertEqual(batch.tasks.count(), 1)
        self.assertEqual(batch.tasks.get().area, self.area_mid)

    def test_reject_past_deadline_and_empty_scope(self):
        past = self.create_batch(deadline="2020-01-01")
        self.assertEqual(past.status_code, 400)
        empty = self.create_batch(area_ids=[self.area_low.id], risk_levels=["high"])
        self.assertEqual(empty.status_code, 400)

    def test_batch_create_requires_admin(self):
        response = self.create_batch(client=self.client1)
        self.assertEqual(response.status_code, 403)

    def test_skip_locked_goods_and_report_uncovered(self):
        # 第一批：仅高风险区，领取后锁定 G-A1/G-A2
        first = self.create_batch(risk_levels=["high"]).json()["data"]
        task, _ = self.claim(self.client1, first, "A1")
        self.assertEqual(
            GoodsLock.objects.filter(task=task, status="active").count(), 2
        )

        # 第二批：全部区域，A1 区物资已被锁定 → 跳过且不重复锁定
        second = self.create_batch().json()["data"]
        self.assertEqual(
            {g["code"] for g in second["skipped_goods"]}, {"G-A1", "G-A2"}
        )
        self.assertEqual(GoodsLock.objects.filter(status="active").count(), 2)
        second_batch = InventoryBatch.objects.get(pk=second["batch"]["id"])
        second_a1 = second_batch.tasks.get(area=self.area_high)
        self.assertEqual(second_a1.items.count(), 0)
        # 空任务直接完成，不会卡住批次
        self.assertEqual(second_a1.status, "done")

    def test_uncovered_goods_without_area_are_reported(self):
        Goods.objects.create(
            variety=self.goods_a1.variety, name="无区物资", code="G-NA",
            quantity=Decimal("1"), area=None,
        )
        response = self.create_batch()
        data = response.json()["data"]
        self.assertEqual([g["code"] for g in data["uncovered_goods"]], ["G-NA"])


class TaskClaimTest(InventoryFixture):
    def test_claim_locks_scope(self):
        batch = self.create_batch().json()["data"]
        task, response = self.claim(self.client1, batch, "A1")
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "claimed")
        self.assertEqual(task.assignee, self.counter1)
        self.assertEqual(
            set(
                GoodsLock.objects.filter(task=task, status="active")
                .values_list("goods__code", flat=True)
            ),
            {"G-A1", "G-A2"},
        )

    def test_double_claim_rejected(self):
        batch = self.create_batch().json()["data"]
        task, first = self.claim(self.client1, batch, "A1")
        self.assertEqual(first.status_code, 200)
        second = self.client2.post(f"/api/inventory/tasks/{task.id}/claim/")
        self.assertEqual(second.status_code, 409)

    def test_claim_excludes_goods_locked_elsewhere(self):
        # 第一批任务领取后中断，锁释放
        first = self.create_batch(risk_levels=["high"]).json()["data"]
        task1, _ = self.claim(self.client1, first, "A1")
        self.client1.post(f"/api/inventory/tasks/{task1.id}/interrupt/")

        # 第二批抢先锁定同一物资
        second = self.create_batch(risk_levels=["high"]).json()["data"]
        task2, _ = self.claim(self.client2, second, "A1")

        # 第一批任务重新领取：冲突物资自动移出，不重复锁定
        response = self.client1.post(f"/api/inventory/tasks/{task1.id}/claim/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(
            {g["code"] for g in data["excluded_goods"]}, {"G-A1", "G-A2"}
        )
        task1.refresh_from_db()
        # 范围内物资全部被移出 → 任务直接完成，锁仍只归第二批任务
        self.assertEqual(task1.status, "done")
        self.assertEqual(
            set(
                GoodsLock.objects.filter(status="active")
                .values_list("task_id", flat=True)
            ),
            {task2.id},
        )
        self.assertEqual(
            task1.items.filter(status="removed").count(), 2
        )


class SegmentSubmitTest(InventoryFixture):
    def setUp(self):
        super().setUp()
        batch = self.create_batch().json()["data"]
        self.batch = InventoryBatch.objects.get(pk=batch["batch"]["id"])
        self.task, _ = self.claim(self.client1, batch, "A1")
        self.item1 = self.item_of(self.task, self.goods_a1)
        self.item2 = self.item_of(self.task, self.goods_a2)

    def test_segment_submit_updates_progress(self):
        response = self.submit(
            self.client1, self.task.id, "seg-1",
            [{"item_id": self.item1.id, "counted_quantity": "10"}],
        )
        self.assertEqual(response.status_code, 200)
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, "counted")
        self.assertEqual(self.item1.counted_quantity, Decimal("10.00"))

        # 任务未完成：还有一条待盘
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "claimed")

        # 提交剩余分段后任务完成、锁释放
        self.submit(
            self.client1, self.task.id, "seg-2",
            [{"item_id": self.item2.id, "counted_quantity": "5"}],
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "done")
        self.assertFalse(GoodsLock.objects.filter(task=self.task, status="active").exists())

    def test_duplicate_submit_is_idempotent(self):
        entries = [{"item_id": self.item1.id, "counted_quantity": "10"}]
        first = self.submit(self.client1, self.task.id, "seg-dup", entries)
        second = self.submit(self.client1, self.task.id, "seg-dup", entries)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["data"]["duplicate"])
        self.assertTrue(second.json()["data"]["duplicate"])
        self.assertEqual(
            first.json()["data"]["submission_id"],
            second.json()["data"]["submission_id"],
        )
        # 不重复生成记录、不重复计数
        self.assertEqual(InventoryRecord.objects.count(), 1)
        self.assertEqual(InventorySubmission.objects.count(), 1)
        self.assertEqual(
            InventoryTaskItem.objects.filter(status="counted").count(), 1
        )

    def test_submit_rejected_for_outsider_and_unclaimed(self):
        outsider = self.submit(
            self.client2, self.task.id, "seg-x",
            [{"item_id": self.item1.id, "counted_quantity": "10"}],
        )
        self.assertEqual(outsider.status_code, 403)

        pending_task = self.batch.tasks.get(area=self.area_mid)
        item = pending_task.items.get()
        unclaimed = self.submit(
            self.client1, pending_task.id, "seg-y",
            [{"item_id": item.id, "counted_quantity": "8"}],
        )
        self.assertEqual(unclaimed.status_code, 400)

    def test_submit_rejects_unknown_and_duplicate_items(self):
        unknown = self.submit(
            self.client1, self.task.id, "seg-bad",
            [{"item_id": 99999, "counted_quantity": "1"}],
        )
        self.assertEqual(unknown.status_code, 400)
        duplicated = self.submit(
            self.client1, self.task.id, "seg-bad2",
            [
                {"item_id": self.item1.id, "counted_quantity": "1"},
                {"item_id": self.item1.id, "counted_quantity": "2"},
            ],
        )
        self.assertEqual(duplicated.status_code, 400)


class ReviewFlowTest(InventoryFixture):
    def setUp(self):
        super().setUp()
        batch = self.create_batch().json()["data"]
        self.task, _ = self.claim(self.client1, batch, "A1")
        self.item1 = self.item_of(self.task, self.goods_a1)
        self.item2 = self.item_of(self.task, self.goods_a2)
        # 一件正常、一件异常
        self.submit(
            self.client1, self.task.id, "seg-1",
            [
                {"item_id": self.item1.id, "counted_quantity": "10"},
                {"item_id": self.item2.id, "counted_quantity": "4", "remark": "少一件"},
            ],
        )
        self.review = InventoryReview.objects.get(item=self.item2)

    def test_abnormal_item_enters_review(self):
        self.item2.refresh_from_db()
        self.assertEqual(self.item2.status, "abnormal")
        self.assertEqual(self.review.status, "pending")
        # 异常未复核，任务不能完成
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "claimed")

    def test_abnormal_item_locked_until_reviewed(self):
        response = self.submit(
            self.client1, self.task.id, "seg-2",
            [{"item_id": self.item2.id, "counted_quantity": "5"}],
        )
        self.assertEqual(response.status_code, 400)

    def test_confirm_review_finishes_task(self):
        response = self.admin_client.post(
            f"/api/inventory/reviews/{self.review.id}/resolve/",
            {"action": "confirm", "final_quantity": "4", "note": "确认损耗"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.review.refresh_from_db()
        self.item2.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.review.status, "confirmed")
        self.assertEqual(self.review.final_quantity, Decimal("4.00"))
        self.assertEqual(self.item2.status, "reviewed")
        self.assertEqual(self.task.status, "done")

        # 重复处理复核单被拒绝
        again = self.admin_client.post(
            f"/api/inventory/reviews/{self.review.id}/resolve/",
            {"action": "confirm"},
            format="json",
        )
        self.assertEqual(again.status_code, 409)

    def test_recount_returns_item_to_pending(self):
        response = self.admin_client.post(
            f"/api/inventory/reviews/{self.review.id}/resolve/",
            {"action": "recount", "note": "重新清点"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.item2.refresh_from_db()
        self.assertEqual(self.item2.status, "pending")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "claimed")

        # 重盘后按账面提交，任务完成
        self.submit(
            self.client1, self.task.id, "seg-3",
            [{"item_id": self.item2.id, "counted_quantity": "5"}],
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "done")

    def test_review_requires_admin(self):
        denied = self.client1.post(
            f"/api/inventory/reviews/{self.review.id}/resolve/",
            {"action": "confirm"},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)
        listing = self.client1.get("/api/inventory/reviews/")
        self.assertEqual(listing.status_code, 403)


class BatchConclusionTest(InventoryFixture):
    def test_conclusion_only_after_all_tasks_done(self):
        batch_json = self.create_batch(risk_levels=["high", "medium"]).json()["data"]
        batch = InventoryBatch.objects.get(pk=batch_json["batch"]["id"])
        self.assertIsNone(batch.conclusion)

        # 完成高风险区任务
        task_a1, _ = self.claim(self.client1, batch_json, "A1")
        for item in task_a1.items.all():
            self.submit(
                self.client1, task_a1.id, f"a1-{item.id}",
                [{"item_id": item.id, "counted_quantity": str(item.book_quantity)}],
            )
        batch.refresh_from_db()
        self.assertEqual(batch.status, "open")
        self.assertIsNone(batch.conclusion)

        # 完成中风险区任务（含一件异常复核）后批次形成结论
        task_a2, _ = self.claim(self.client2, batch_json, "A2")
        item = task_a2.items.get()
        self.submit(
            self.client2, task_a2.id, "a2-1",
            [{"item_id": item.id, "counted_quantity": "6"}],
        )
        batch.refresh_from_db()
        self.assertEqual(batch.status, "open")
        review = InventoryReview.objects.get(item=item)
        self.admin_client.post(
            f"/api/inventory/reviews/{review.id}/resolve/",
            {"action": "confirm"},
            format="json",
        )
        batch.refresh_from_db()
        self.assertEqual(batch.status, "concluded")
        self.assertIsNotNone(batch.concluded_at)
        conclusion = batch.conclusion
        self.assertEqual(conclusion["total_items"], 3)
        self.assertEqual(conclusion["normal_items"], 2)
        self.assertEqual(conclusion["abnormal_items"], 1)
        self.assertEqual(len(conclusion["areas"]), 2)

    def test_concluded_batch_tasks_cannot_be_claimed(self):
        batch_json = self.create_batch(area_ids=[self.area_low.id]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "B1")
        item = task.items.get()
        self.submit(
            self.client1, task.id, "b1-1",
            [{"item_id": item.id, "counted_quantity": "3"}],
        )
        batch = InventoryBatch.objects.get(pk=batch_json["batch"]["id"])
        self.assertEqual(batch.status, "concluded")


class InterruptReassignTest(InventoryFixture):
    def test_interrupt_preserves_progress_and_releases_locks(self):
        batch_json = self.create_batch(risk_levels=["high"]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "A1")
        item1 = self.item_of(task, self.goods_a1)
        item2 = self.item_of(task, self.goods_a2)
        self.submit(
            self.client1, task.id, "seg-1",
            [{"item_id": item1.id, "counted_quantity": "10"}],
        )

        # 中断：锁释放、进度保留、任务回到待领取
        response = self.client1.post(f"/api/inventory/tasks/{task.id}/interrupt/")
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertIsNone(task.assignee)
        self.assertFalse(GoodsLock.objects.filter(status="active").exists())
        item1.refresh_from_db()
        self.assertEqual(item1.status, "counted")

        # 另一盘点员接手，从中断处继续，不重复计数
        _, reclaim = self.claim(self.client2, {"batch": {"id": batch_json["batch"]["id"]}}, "A1")
        self.assertEqual(reclaim.status_code, 200)
        self.submit(
            self.client2, task.id, "seg-2",
            [{"item_id": item2.id, "counted_quantity": "5"}],
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        self.assertEqual(InventoryRecord.objects.filter(item=item1).count(), 1)
        self.assertEqual(InventoryRecord.objects.count(), 2)

    def test_interrupt_permission(self):
        batch_json = self.create_batch(risk_levels=["high"]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "A1")
        denied = self.client2.post(f"/api/inventory/tasks/{task.id}/interrupt/")
        self.assertEqual(denied.status_code, 403)
        allowed = self.admin_client.post(f"/api/inventory/tasks/{task.id}/interrupt/")
        self.assertEqual(allowed.status_code, 200)

    def test_reassign_keeps_scope_and_progress(self):
        batch_json = self.create_batch(risk_levels=["high"]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "A1")
        item1 = self.item_of(task, self.goods_a1)
        item2 = self.item_of(task, self.goods_a2)
        self.submit(
            self.client1, task.id, "seg-1",
            [{"item_id": item1.id, "counted_quantity": "10"}],
        )

        response = self.admin_client.post(
            f"/api/inventory/tasks/{task.id}/reassign/",
            {"user_id": self.counter2.id},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.assignee, self.counter2)
        # 范围锁定不变
        self.assertEqual(GoodsLock.objects.filter(task=task, status="active").count(), 2)

        # 原盘点员不能再提交，新盘点员继续
        old_submit = self.submit(
            self.client1, task.id, "seg-2",
            [{"item_id": item2.id, "counted_quantity": "5"}],
        )
        self.assertEqual(old_submit.status_code, 403)
        new_submit = self.submit(
            self.client2, task.id, "seg-2",
            [{"item_id": item2.id, "counted_quantity": "5"}],
        )
        self.assertEqual(new_submit.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "done")

    def test_reassign_requires_admin(self):
        batch_json = self.create_batch(risk_levels=["high"]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "A1")
        denied = self.client2.post(
            f"/api/inventory/tasks/{task.id}/reassign/",
            {"user_id": self.counter2.id},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)


class ScopeSyncTest(InventoryFixture):
    def test_sync_adds_new_goods_and_removes_outgoing(self):
        batch_json = self.create_batch(risk_levels=["high"]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "A1")
        item1 = self.item_of(task, self.goods_a1)
        self.submit(
            self.client1, task.id, "seg-1",
            [{"item_id": item1.id, "counted_quantity": "10"}],
        )

        # 范围变化：新物资划入、旧物资划出
        new_goods = Goods.objects.create(
            variety=self.goods_a1.variety, name="新划入物资", code="G-NEW",
            quantity=Decimal("7"), area=self.area_high,
        )
        self.goods_a2.area = self.area_mid
        self.goods_a2.save()

        response = self.client1.post(f"/api/inventory/tasks/{task.id}/sync-scope/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual([i["goods"] for i in data["added"]], [new_goods.id])
        self.assertEqual([i["goods"] for i in data["removed"]], [self.goods_a2.id])

        # 已盘进度保留，移出项不参与进度，新物资待盘且已加锁
        item1.refresh_from_db()
        self.assertEqual(item1.status, "counted")
        removed = self.item_of(task, self.goods_a2)
        self.assertEqual(removed.status, "removed")
        self.assertEqual(
            set(
                GoodsLock.objects.filter(task=task, status="active")
                .values_list("goods__code", flat=True)
            ),
            {"G-A1", "G-NEW"},
        )

        # 盘点新物资后任务完成
        new_item = self.item_of(task, new_goods)
        self.submit(
            self.client1, task.id, "seg-2",
            [{"item_id": new_item.id, "counted_quantity": "7"}],
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        # 移出项的历史记录保留用于审计
        self.assertTrue(InventoryTaskItem.objects.filter(pk=removed.pk).exists())

    def test_sync_restores_returning_goods_as_pending(self):
        batch_json = self.create_batch(risk_levels=["high"]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "A1")
        self.goods_a1.area = self.area_mid
        self.goods_a1.save()
        self.client1.post(f"/api/inventory/tasks/{task.id}/sync-scope/")
        self.assertEqual(self.item_of(task, self.goods_a1).status, "removed")

        # 物资划回：恢复为待盘点，账面数量按最新快照
        self.goods_a1.area = self.area_high
        self.goods_a1.quantity = Decimal("12")
        self.goods_a1.save()
        self.client1.post(f"/api/inventory/tasks/{task.id}/sync-scope/")
        restored = self.item_of(task, self.goods_a1)
        self.assertEqual(restored.status, "pending")
        self.assertEqual(restored.book_quantity, Decimal("12.00"))
        self.assertIsNone(restored.counted_quantity)

    def test_sync_rejected_when_done(self):
        batch_json = self.create_batch(area_ids=[self.area_low.id]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "B1")
        item = task.items.get()
        self.submit(
            self.client1, task.id, "seg-1",
            [{"item_id": item.id, "counted_quantity": "3"}],
        )
        task.refresh_from_db()
        self.assertEqual(task.status, "done")
        response = self.client1.post(f"/api/inventory/tasks/{task.id}/sync-scope/")
        self.assertEqual(response.status_code, 400)


class BatchQueryTest(InventoryFixture):
    def test_batch_detail_points_out_unfinished_scope(self):
        batch_json = self.create_batch(risk_levels=["high", "medium"]).json()["data"]
        batch_id = batch_json["batch"]["id"]

        task_a1, _ = self.claim(self.client1, batch_json, "A1")
        item1 = self.item_of(task_a1, self.goods_a1)
        self.submit(
            self.client1, task_a1.id, "seg-1",
            [{"item_id": item1.id, "counted_quantity": "10"}],
        )

        detail = self.admin_client.get(f"/api/inventory/batches/{batch_id}/")
        self.assertEqual(detail.status_code, 200)
        data = detail.json()["data"]
        unfinished = data["unfinished"]

        # 两个任务均未完成：A1 剩 G-A2，A2 整区未领取
        self.assertEqual(len(unfinished), 2)
        by_area = {entry["area_name"]: entry for entry in unfinished}
        a1 = by_area["证物一区"]
        self.assertEqual(a1["task_status"], "claimed")
        self.assertEqual(a1["assignee"], "counter-one")
        self.assertEqual([i["goods_code"] for i in a1["pending_items"]], ["G-A2"])
        a2 = by_area["证物二区"]
        self.assertEqual(a2["task_status"], "pending")
        self.assertIsNone(a2["assignee"])
        self.assertEqual([i["goods_code"] for i in a2["pending_items"]], ["G-B1"])

        # 批次进度汇总
        batch_data = data["batch"]
        self.assertEqual(batch_data["total_items"], 3)
        self.assertEqual(batch_data["done_items"], 1)

    def test_unfinished_empty_after_conclusion(self):
        batch_json = self.create_batch(area_ids=[self.area_low.id]).json()["data"]
        task, _ = self.claim(self.client1, batch_json, "B1")
        item = task.items.get()
        self.submit(
            self.client1, task.id, "seg-1",
            [{"item_id": item.id, "counted_quantity": "3"}],
        )
        detail = self.admin_client.get(f"/api/inventory/batches/{batch_json['batch']['id']}/")
        data = detail.json()["data"]
        self.assertEqual(data["unfinished"], [])
        self.assertEqual(data["batch"]["status"], "concluded")
        self.assertEqual(data["batch"]["conclusion"]["total_items"], 1)

    def test_task_list_filters(self):
        batch_json = self.create_batch().json()["data"]
        task, _ = self.claim(self.client1, batch_json, "A1")

        mine = self.client1.get("/api/inventory/tasks/?mine=true")
        self.assertEqual(mine.json()["data"]["total"], 1)
        others = self.client2.get("/api/inventory/tasks/?mine=true")
        self.assertEqual(others.json()["data"]["total"], 0)
        by_batch = self.client1.get(f"/api/inventory/tasks/?batch={batch_json['batch']['id']}")
        self.assertEqual(by_batch.json()["data"]["total"], 3)
        pending = self.client1.get("/api/inventory/tasks/?status=pending")
        self.assertEqual(pending.json()["data"]["total"], 2)


class InventoryAuthTest(InventoryFixture):
    def test_requires_authentication(self):
        anonymous = APIClient()
        for url in [
            "/api/inventory/areas/",
            "/api/inventory/batches/",
            "/api/inventory/tasks/",
            "/api/inventory/reviews/",
        ]:
            self.assertEqual(anonymous.get(url).status_code, 401)
