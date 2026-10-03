"""
盘点业务服务

进度一律由任务明细状态推导，提交记录仅作审计流水，
因此中断、重新分配、范围变化和重复提交都不会重复计数。
"""
import uuid
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.exceptions import BusinessException
from apps.warehouse.models import Goods
from .models import (
    StorageArea, InventoryBatch, InventoryTask, InventoryTaskItem,
    InventorySubmission, InventoryRecord, InventoryReview, GoodsLock,
)

# 明细的未完成状态（其余状态 counted/reviewed 为终态，removed 不参与进度）
OPEN_ITEM_STATUSES = ['pending', 'abnormal']
# 明细的完成状态
DONE_ITEM_STATUSES = ['counted', 'reviewed']


def _locked_goods_ids(exclude_task=None):
    """当前被其他任务锁定的物资ID集合"""
    queryset = GoodsLock.objects.filter(status='active')
    if exclude_task is not None:
        queryset = queryset.exclude(task=exclude_task)
    return set(queryset.values_list('goods_id', flat=True))


def _release_locks(task, reason):
    """释放任务持有的全部锁"""
    GoodsLock.objects.filter(task=task, status='active').update(
        status='released', released_at=timezone.now(), release_reason=reason
    )


def maybe_finish_task(task):
    """任务明细全部进入终态时完成任务并释放锁，返回是否完成"""
    if task.status == 'done':
        return False
    has_open = task.items.filter(status__in=OPEN_ITEM_STATUSES).exists()
    if has_open:
        return False
    task.status = 'done'
    task.finished_at = timezone.now()
    task.save(update_fields=['status', 'finished_at', 'updated_at'])
    _release_locks(task, reason='completed')
    maybe_conclude_batch(task.batch)
    return True


def maybe_conclude_batch(batch):
    """全部任务完成后形成批次结论，返回是否形成结论"""
    if batch.status != 'open':
        return False
    if batch.tasks.exclude(status='done').exists():
        return False
    areas = []
    total = normal = abnormal = 0
    for task in batch.tasks.select_related('area').order_by('task_no'):
        items = task.items.exclude(status='removed')
        task_total = items.count()
        task_normal = items.filter(status='counted').count()
        task_abnormal = items.filter(status='reviewed').count()
        total += task_total
        normal += task_normal
        abnormal += task_abnormal
        areas.append({
            'task_no': task.task_no,
            'area_id': task.area_id,
            'area_name': task.area.name,
            'total_items': task_total,
            'normal_items': task_normal,
            'abnormal_items': task_abnormal,
        })
    batch.status = 'concluded'
    batch.concluded_at = timezone.now()
    batch.conclusion = {
        'total_items': total,
        'normal_items': normal,
        'abnormal_items': abnormal,
        'areas': areas,
    }
    batch.save(update_fields=['status', 'concluded_at', 'conclusion', 'updated_at'])
    return True


@transaction.atomic
def create_batch(*, user, title, area_ids, risk_levels, deadline):
    """
    主管生成盘点批次：按区域与风险筛选保管区，逐区拆分任务并快照范围。
    已被其他批次锁定的物资自动跳过，避免同一物资重复锁定。
    返回 (batch, skipped_goods, uncovered_goods)。
    """
    areas = StorageArea.objects.filter(is_active=True)
    if area_ids:
        areas = areas.filter(id__in=area_ids)
    if risk_levels:
        areas = areas.filter(risk_level__in=risk_levels)
    areas = list(areas.order_by('code'))
    if not areas:
        raise BusinessException('没有符合条件的保管区，无法生成批次')

    batch = InventoryBatch.objects.create(
        batch_no=f"TMP-{uuid.uuid4().hex[:20]}",
        title=title,
        risk_levels=risk_levels or [],
        deadline=deadline,
        created_by=user,
    )
    batch.batch_no = f"PD{timezone.localdate():%Y%m%d}-{batch.pk:04d}"
    batch.save(update_fields=['batch_no'])

    locked_ids = _locked_goods_ids()
    skipped_goods = []
    for seq, area in enumerate(areas, 1):
        task = InventoryTask.objects.create(
            task_no=f"{batch.batch_no}-T{seq:02d}",
            batch=batch,
            area=area,
        )
        items = []
        for goods in Goods.objects.filter(area=area, is_active=True).order_by('id'):
            if goods.id in locked_ids:
                skipped_goods.append(goods)
                continue
            items.append(InventoryTaskItem(
                task=task, goods=goods, book_quantity=goods.quantity
            ))
        InventoryTaskItem.objects.bulk_create(items)
        # 范围内无物资的任务直接完成，空区域不会卡住批次
        maybe_finish_task(task)

    # 无保管区或保管区已停用的在库物资无法被任何批次覆盖，显式提示避免遗漏
    uncovered_goods = list(
        Goods.objects.filter(is_active=True)
        .filter(Q(area__isnull=True) | Q(area__is_active=False))
        .exclude(id__in=locked_ids)
        .order_by('id')
    )
    batch.refresh_from_db()
    return batch, skipped_goods, uncovered_goods


@transaction.atomic
def claim_task(task, user):
    """
    领取任务并锁定范围。领取时再次核对锁定冲突：
    被其他任务抢先锁定的物资自动移出本任务范围（保留历史记录），
    保证任务总能继续推进且同一物资不会被重复锁定。
    返回 (task, excluded_goods)。
    """
    if task.batch.status != 'open':
        raise BusinessException('批次已结论，任务不可领取')
    updated = InventoryTask.objects.filter(pk=task.pk, status='pending').update(
        status='claimed', assignee=user, claimed_at=timezone.now(), updated_at=timezone.now()
    )
    if not updated:
        raise BusinessException('任务已被领取或已完成', code=409)
    task.refresh_from_db()

    conflict_ids = _locked_goods_ids(exclude_task=task)
    excluded_goods = []
    for item in task.items.exclude(status='removed').select_related('goods'):
        if item.goods_id in conflict_ids:
            item.status = 'removed'
            item.removed_reason = '物资已被其他盘点任务锁定'
            item.save(update_fields=['status', 'removed_reason', 'updated_at'])
            excluded_goods.append(item.goods)

    locks = [
        GoodsLock(task=task, goods_id=goods_id)
        for goods_id in task.items.exclude(status='removed').values_list('goods_id', flat=True)
    ]
    GoodsLock.objects.bulk_create(locks)
    maybe_finish_task(task)
    task.refresh_from_db()
    return task, excluded_goods


@transaction.atomic
def interrupt_task(task, user):
    """中断任务：释放锁并退回待领取，已提交的盘点进度完整保留"""
    if task.status != 'claimed':
        raise BusinessException('仅进行中的任务可以中断')
    if task.assignee_id != user.id and not user.is_admin:
        raise BusinessException('仅盘点员本人或主管可以中断任务', code=403)
    task.status = 'pending'
    task.assignee = None
    task.claimed_at = None
    task.save(update_fields=['status', 'assignee', 'claimed_at', 'updated_at'])
    _release_locks(task, reason='interrupt')


@transaction.atomic
def reassign_task(task, new_assignee):
    """重新分配：更换盘点员，范围锁定与盘点进度保持不变"""
    if task.status != 'claimed':
        raise BusinessException('任务未领取，无法重新分配')
    task.assignee = new_assignee
    task.save(update_fields=['assignee', 'updated_at'])


@transaction.atomic
def submit_segment(task, user, request_id, entries):
    """
    分段提交盘点结果。同一 request_id 重复提交直接返回原结果，
    不重复生成记录、不重复计数。异常明细自动生成复核单。
    返回 (submission, duplicated)。
    """
    if task.status != 'claimed':
        raise BusinessException('任务未领取，不能提交盘点结果')
    if task.assignee_id != user.id:
        raise BusinessException('仅当前盘点员可以提交该任务', code=403)

    existing = InventorySubmission.objects.filter(task=task, request_id=request_id).first()
    if existing:
        return existing, True

    submission = InventorySubmission.objects.create(
        task=task, request_id=request_id, submitted_by=user
    )
    now = timezone.now()
    results = []
    for entry in entries:
        item = task.items.exclude(status='removed').filter(pk=entry['item_id']).first()
        if item is None:
            raise BusinessException(f"明细 {entry['item_id']} 不在任务范围内")
        if item.status == 'abnormal':
            raise BusinessException(f"货物 {item.goods.name} 待复核，请先完成复核")
        if item.status == 'reviewed':
            raise BusinessException(f"货物 {item.goods.name} 已复核定案，不能重复提交")

        counted = entry['counted_quantity']
        normal = counted == item.book_quantity
        record = InventoryRecord.objects.create(
            submission=submission,
            item=item,
            counted_quantity=counted,
            result='normal' if normal else 'abnormal',
            remark=entry.get('remark', ''),
        )
        item.counted_quantity = counted
        item.counted_by = user
        item.counted_at = now
        item.status = 'counted' if normal else 'abnormal'
        item.save(update_fields=['counted_quantity', 'counted_by', 'counted_at', 'status', 'updated_at'])
        if not normal:
            InventoryReview.objects.create(item=item, record=record)
        results.append({
            'item_id': item.id,
            'goods_id': item.goods_id,
            'goods_name': item.goods.name,
            'book_quantity': str(item.book_quantity),
            'counted_quantity': str(counted),
            'result': record.result,
        })

    submission.summary = {'results': results}
    submission.save(update_fields=['summary'])
    maybe_finish_task(task)
    return submission, False


@transaction.atomic
def resolve_review(review, user, action, final_quantity=None, note=''):
    """复核异常明细：确认差异则定案，退回重盘则明细回到待盘点"""
    if review.status != 'pending':
        raise BusinessException('该复核单已处理', code=409)
    item = review.item
    review.reviewer = user
    review.reviewed_at = timezone.now()
    review.review_note = note
    if action == 'confirm':
        review.status = 'confirmed'
        review.final_quantity = final_quantity if final_quantity is not None else item.counted_quantity
        item.status = 'reviewed'
    else:
        review.status = 'recount'
        item.status = 'pending'
    item.save(update_fields=['status', 'updated_at'])
    review.save()
    maybe_finish_task(item.task)


@transaction.atomic
def sync_task_scope(task):
    """
    范围变化同步：新划入区域的物资补入任务，已划出区域、停用
    或被其他任务锁定的物资移出范围（历史记录保留，不参与进度）。
    返回 (added_items, removed_items)。
    """
    if task.status == 'done':
        raise BusinessException('任务已完成，范围不可调整')

    locked_elsewhere = _locked_goods_ids(exclude_task=task)
    desired = {
        goods.id: goods
        for goods in Goods.objects.filter(area=task.area, is_active=True)
        if goods.id not in locked_elsewhere
    }
    current_items = {item.goods_id: item for item in task.items.exclude(status='removed')}
    removed_items = []
    now = timezone.now()

    # 移出：不在目标范围内的现有明细
    for goods_id, item in current_items.items():
        if goods_id not in desired:
            item.status = 'removed'
            item.removed_reason = '范围调整移出'
            item.save(update_fields=['status', 'removed_reason', 'updated_at'])
            removed_items.append(item)
    if removed_items:
        GoodsLock.objects.filter(
            task=task, status='active',
            goods_id__in=[item.goods_id for item in removed_items]
        ).update(status='released', released_at=now, release_reason='scope_change')

    # 补入：新进入范围的物资；曾被移出的恢复为待盘点
    added_items = []
    all_items = {item.goods_id: item for item in task.items.all()}
    for goods_id, goods in desired.items():
        if goods_id in current_items:
            continue
        existing = all_items.get(goods_id)
        if existing is not None:
            existing.status = 'pending'
            existing.book_quantity = goods.quantity
            existing.counted_quantity = None
            existing.counted_by = None
            existing.counted_at = None
            existing.removed_reason = ''
            existing.save(update_fields=[
                'status', 'book_quantity', 'counted_quantity',
                'counted_by', 'counted_at', 'removed_reason', 'updated_at'
            ])
            added_items.append(existing)
        else:
            added_items.append(InventoryTaskItem.objects.create(
                task=task, goods=goods, book_quantity=goods.quantity
            ))
    if task.status == 'claimed' and added_items:
        GoodsLock.objects.bulk_create([
            GoodsLock(task=task, goods_id=item.goods_id) for item in added_items
        ])

    maybe_finish_task(task)
    return added_items, removed_items
