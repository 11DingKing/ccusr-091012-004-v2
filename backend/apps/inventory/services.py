"""
盘点业务服务

所有状态流转集中在本模块，视图层只做参数校验与响应包装。
关键点：
- 领取/释放/完成在事务内翻转 TaskGoods.is_locked，部分唯一索引保证不重复锁定；
- 分段提交以 (任务, 物资) 做 update_or_create，中断、重提均不丢进度、不重复计数；
- 范围变化只生成“增补任务”，历史任务与已盘进度不动。
"""
from collections import defaultdict
from datetime import datetime, timezone

from django.db import transaction, IntegrityError
from django.db.models import F

from apps.core.exceptions import BusinessException, NotFoundException, PermissionException

from .models import (
    StocktakeBatch, StocktakeTask, TaskGoods, StocktakeItem, StocktakeReview,
    RISK_ORDER,
    BATCH_COMPLETED,
    TASK_PENDING, TASK_IN_PROGRESS, TASK_COMPLETED,
    RESULT_ABNORMAL,
    REVIEW_PENDING, REVIEW_RESOLVED,
    DEFAULT_CHUNK_SIZE,
)


def _now():
    return datetime.now(timezone.utc)


def _location_label(location):
    return location or '未分区'


# ==================== 批次生成与拆分 ====================

def _split_tasks(batch, goods_list, scope_version, is_supplement=False):
    """按区域分组，再按 chunk_size 分块生成任务并挂载物资范围。"""
    grouped = defaultdict(list)
    for goods in goods_list:
        grouped[goods.location or ''].append(goods)

    tasks = []
    # 区域内序号独立，保持稳定顺序
    counters = defaultdict(int)
    for location in sorted(grouped.keys()):
        goods_in_loc = grouped[location]
        for start in range(0, len(goods_in_loc), batch.chunk_size):
            chunk = goods_in_loc[start:start + batch.chunk_size]
            counters[location] += 1
            label = _location_label(location)
            seq = f"{label}-{scope_version}-{counters[location]:02d}"
            risk = min(
                (StocktakeBatch.risk_level_of(g) for g in chunk),
                key=lambda r: RISK_ORDER[r],
            )
            task = StocktakeTask.objects.create(
                batch=batch,
                seq=seq,
                location=location,
                risk_level=risk,
                scope_version=scope_version,
                is_supplement=is_supplement,
                status=TASK_PENDING,
                total_count=len(chunk),
            )
            TaskGoods.objects.bulk_create([
                TaskGoods(task=task, goods=g, is_locked=False) for g in chunk
            ])
            tasks.append(task)
    return tasks


@transaction.atomic
def create_batch(*, name, locations, risk_levels, deadline, chunk_size, created_by):
    """主管生成盘点批次：圈定范围并拆分为可执行任务。"""
    batch = StocktakeBatch.objects.create(
        name=name,
        locations=locations or [],
        risk_levels=risk_levels or [],
        deadline=deadline,
        chunk_size=chunk_size or DEFAULT_CHUNK_SIZE,
        created_by=created_by,
    )
    goods_list = list(batch.scope_queryset())
    if not goods_list:
        # 空范围直接报错，避免“遗漏区域无人发现”
        raise BusinessException('所选区域/风险范围内没有可盘点物资')
    _split_tasks(batch, goods_list, scope_version=1)
    return batch


@transaction.atomic
def refresh_scope(batch):
    """范围变化处理：把当前在册但尚未纳入任何任务的物资拆成增补任务。

    已完成批次不允许变更范围；已有任务与盘点进度保持不动。
    """
    if batch.status == BATCH_COMPLETED:
        raise BusinessException('批次已形成结论，不能再变更范围')

    covered_ids = set(TaskGoods.objects.filter(task__batch=batch).values_list('goods_id', flat=True))
    new_goods = [g for g in batch.scope_queryset() if g.id not in covered_ids]
    if not new_goods:
        return {'new_task_count': 0, 'new_goods_count': 0, 'tasks': []}

    batch.scope_version = F('scope_version') + 1
    batch.save(update_fields=['scope_version', 'updated_at'])
    batch.refresh_from_db(fields=['scope_version'])
    tasks = _split_tasks(batch, new_goods, scope_version=batch.scope_version, is_supplement=True)
    return {
        'new_task_count': len(tasks),
        'new_goods_count': len(new_goods),
        'tasks': tasks,
    }


# ==================== 领取 / 中断 / 重新分配 ====================

def _activate_locks(task):
    """把任务范围置为锁定态；任一物资已被其他进行中任务锁定则整体失败。"""
    try:
        TaskGoods.objects.filter(task=task).update(is_locked=True)
    except IntegrityError:
        raise BusinessException('范围内物资正被其他盘点任务锁定，无法重复领取', code=409)


@transaction.atomic
def claim_task(task, user):
    """盘点员领取任务，领取瞬间锁定该任务的物资范围。"""
    task = StocktakeTask.objects.select_for_update().select_related('assignee').get(pk=task.pk)
    if task.status == TASK_COMPLETED:
        raise BusinessException('任务已完成，不能领取')
    if task.is_locked:
        if task.assignee_id == user.id:
            return task  # 同一人重复领取，幂等返回
        raise BusinessException('任务已被其他盘点员领取', code=409)

    _activate_locks(task)
    task.status = TASK_IN_PROGRESS
    task.assignee = user
    task.claimed_at = _now()
    task.released_at = None
    task.claim_count = F('claim_count') + 1
    task.save(update_fields=[
        'status', 'assignee', 'claimed_at', 'released_at', 'claim_count', 'updated_at',
    ])
    task.refresh_from_db()
    task.batch.refresh_status()
    return task


@transaction.atomic
def release_task(task, user, *, force=False):
    """中断任务：释放锁、退回任务池，已盘条目与领取历史全部保留。"""
    task = StocktakeTask.objects.select_for_update().get(pk=task.pk)
    if task.status == TASK_COMPLETED:
        raise BusinessException('任务已完成，不能释放')
    if task.is_locked and task.assignee_id != user.id and not force:
        raise PermissionException('只能中断本人领取的任务')
    if not task.is_locked and task.status == TASK_PENDING:
        return task  # 本就在池中，幂等

    TaskGoods.objects.filter(task=task).update(is_locked=False)
    task.status = TASK_PENDING
    task.released_at = _now()
    task.assignee = None
    task.save(update_fields=['status', 'released_at', 'assignee', 'updated_at'])
    task.batch.refresh_status()
    return task


@transaction.atomic
def reassign_task(task, new_assignee):
    """主管重新分配：
    - 进行中任务：直接更换盘点员，锁与已盘进度都保持；
    - 待领取任务：以新盘点员名义激活领取。
    """
    task = StocktakeTask.objects.select_for_update().get(pk=task.pk)
    if task.status == TASK_COMPLETED:
        raise BusinessException('任务已完成，不能重新分配')

    if task.is_locked:
        task.assignee = new_assignee
        task.save(update_fields=['assignee', 'updated_at'])
    else:
        _activate_locks(task)
        task.status = TASK_IN_PROGRESS
        task.assignee = new_assignee
        task.claimed_at = _now()
        task.released_at = None
        task.claim_count = F('claim_count') + 1
        task.save(update_fields=[
            'status', 'assignee', 'claimed_at', 'released_at', 'claim_count', 'updated_at',
        ])
        task.refresh_from_db()
        task.batch.refresh_status()
    return task


# ==================== 分段提交与复核 ====================

def _sync_review(item):
    """根据条目最新结果维护复核队列：异常入队/重新打开，更正为正常则自动关闭。"""
    review = getattr(item, 'review', None) or StocktakeReview.objects.filter(item=item).first()
    if item.result == RESULT_ABNORMAL:
        if review is None:
            StocktakeReview.objects.create(item=item)
        elif review.status == REVIEW_RESOLVED:
            review.status = REVIEW_PENDING
            review.reviewer = None
            review.reviewed_at = None
            review.review_note = ''
            review.save(update_fields=['status', 'reviewer', 'reviewed_at', 'review_note'])
    elif review is not None and review.status == REVIEW_PENDING:
        review.status = REVIEW_RESOLVED
        review.reviewed_at = _now()
        review.review_note = '盘点员更正为正常，系统自动关闭复核'
        review.save(update_fields=['status', 'reviewed_at', 'review_note'])


@transaction.atomic
def submit_items(task, user, entries, *, force=False):
    """分段提交盘点结果。

    - 仅任务持有人可提交（主管 force 可代交）；
    - 同一 (任务, 物资) 走 update_or_create，重复提交不新增、不重复计数；
    - 异常条目自动进入复核队列。
    """
    task = StocktakeTask.objects.select_for_update().get(pk=task.pk)
    if task.status == TASK_COMPLETED:
        raise BusinessException('任务已完成，不能再提交盘点结果')
    if not force and task.assignee_id != user.id:
        raise PermissionException('只能提交本人领取任务的盘点结果')

    scope_ids = set(TaskGoods.objects.filter(task=task).values_list('goods_id', flat=True))
    created, updated, abnormal = 0, 0, 0
    for entry in entries:
        goods_id = entry['goods']
        if goods_id not in scope_ids:
            raise BusinessException(f'物资 {goods_id} 不在本任务盘点范围内')

        defaults = {
            'result': entry['result'],
            'actual_quantity': entry.get('actual_quantity'),
            'abnormal_type': entry.get('abnormal_type', '') if entry['result'] == RESULT_ABNORMAL else '',
            'remark': entry.get('remark', ''),
            'counted_by': user,
        }
        item, was_created = StocktakeItem.objects.update_or_create(
            task=task, goods_id=goods_id, defaults=defaults,
        )
        if was_created:
            created += 1
        else:
            StocktakeItem.objects.filter(pk=item.pk).update(revision=F('revision') + 1)
            item.refresh_from_db()
            updated += 1
        if item.result == RESULT_ABNORMAL:
            abnormal += 1
        _sync_review(item)

    task.save(update_fields=['updated_at'])
    return {
        'created': created,
        'updated': updated,
        'abnormal': abnormal,
        'counted_count': task.items.count(),
        'total_count': task.total_count,
    }


@transaction.atomic
def complete_task(task, user, *, force=False):
    """盘点员完成任务：范围全部盘完方可关闭，关闭后释放锁；异常复核不阻塞任务关闭。"""
    task = StocktakeTask.objects.select_for_update().get(pk=task.pk)
    if task.status == TASK_COMPLETED:
        return task  # 幂等
    if not force and task.assignee_id != user.id:
        raise PermissionException('只能完成本人领取的任务')

    scope_ids = set(TaskGoods.objects.filter(task=task).values_list('goods_id', flat=True))
    counted_ids = set(task.items.values_list('goods_id', flat=True))
    missing = sorted(scope_ids - counted_ids)
    if missing:
        raise BusinessException(f'仍有 {len(missing)} 项物资未盘点，不能完成任务', code=400)

    TaskGoods.objects.filter(task=task).update(is_locked=False)
    task.status = TASK_COMPLETED
    task.completed_at = _now()
    task.released_at = None
    task.save(update_fields=['status', 'completed_at', 'released_at', 'updated_at'])
    task.batch.refresh_status()
    return task


@transaction.atomic
def resolve_review(review, reviewer, note):
    """主管复核异常项并闭环。"""
    review = StocktakeReview.objects.select_for_update().get(pk=review.pk)
    review.status = REVIEW_RESOLVED
    review.reviewer = reviewer
    review.review_note = note or ''
    review.reviewed_at = _now()
    review.save(update_fields=['status', 'reviewer', 'review_note', 'reviewed_at'])
    return review


@transaction.atomic
def conclude_batch(batch, user, conclusion=''):
    """所有任务完成且复核清零后，形成批次结论。"""
    batch = StocktakeBatch.objects.select_for_update().get(pk=batch.pk)
    if batch.status == BATCH_COMPLETED:
        return batch
    if not batch.tasks.exists():
        raise BusinessException('批次没有盘点任务，无法形成结论')

    pending_tasks = batch.tasks.exclude(status=TASK_COMPLETED)
    if pending_tasks.exists():
        raise BusinessException('仍有盘点任务未完成，不能形成批次结论')

    pending_reviews = StocktakeReview.objects.filter(
        item__task__batch=batch, status=REVIEW_PENDING
    ).count()
    if pending_reviews:
        raise BusinessException(f'仍有 {pending_reviews} 项异常待复核，不能形成批次结论')

    total = batch.total_goods_count
    abnormal = batch.abnormal_goods_count
    auto_summary = f'本批次共盘点 {total} 项物资，异常 {abnormal} 项，均已复核闭环。'
    batch.status = BATCH_COMPLETED
    batch.conclusion = f'{conclusion}\n{auto_summary}'.strip() if conclusion else auto_summary
    batch.concluded_by = user
    batch.concluded_at = _now()
    batch.save(update_fields=['status', 'conclusion', 'concluded_by', 'concluded_at', 'updated_at'])
    return batch


def get_batch_or_404(batch_id):
    batch = StocktakeBatch.objects.filter(pk=batch_id).first()
    if batch is None:
        raise NotFoundException('盘点批次不存在')
    return batch


def get_task_or_404(task_id):
    task = StocktakeTask.objects.filter(pk=task_id).first()
    if task is None:
        raise NotFoundException('盘点任务不存在')
    return task


def get_review_or_404(review_id):
    review = StocktakeReview.objects.filter(pk=review_id).first()
    if review is None:
        raise NotFoundException('复核记录不存在')
    return review
