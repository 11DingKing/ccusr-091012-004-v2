"""
盘点管理模型

核心概念：
- StocktakeBatch（盘点批次）：主管按区域、风险等级、截止日期生成，创建时快照物资范围。
- StocktakeTask（盘点任务）：批次范围按区域 + 分块拆分出的可执行单元，盘点员领取后锁定其物资范围。
- TaskGoods（任务物资）：任务持有的物资范围；is_locked=True 表示该物资正被进行中任务锁定。
- StocktakeItem（盘点条目）：任务内每件物资的盘点结果，(任务, 物资) 唯一，支持分段提交、幂等重提。
- StocktakeReview（复核记录）：异常条目的复核闭环，未清零前批次不能形成结论。

并发安全：
- TaskGoods(goods) 在 is_locked=True 上有部分唯一索引，同一物资任意时刻只被一个进行中任务锁定，
  从根本上杜绝多个保管区盘点任务重叠导致的重复锁定。
- 领取 / 释放 / 完成 / 提交均包裹在事务与行锁中。
"""
from django.db import models
from django.db.models import Q

from apps.authentication.models import User
from apps.warehouse.models import Goods


# 风险等级
RISK_LOW = 'low'
RISK_MEDIUM = 'medium'
RISK_HIGH = 'high'
RISK_CHOICES = [
    (RISK_LOW, '低风险'),
    (RISK_MEDIUM, '中风险'),
    (RISK_HIGH, '高风险'),
]
# 任务风险取范围内最高等级时使用（数值越小风险越高）
RISK_ORDER = {RISK_HIGH: 0, RISK_MEDIUM: 1, RISK_LOW: 2}

# 批次状态
BATCH_DRAFT = 'draft'         # 任务已生成，尚无任务被领取
BATCH_IN_PROGRESS = 'in_progress'
BATCH_COMPLETED = 'completed'
BATCH_STATUS_CHOICES = [
    (BATCH_DRAFT, '待开始'),
    (BATCH_IN_PROGRESS, '盘点中'),
    (BATCH_COMPLETED, '已完成'),
]

# 任务状态
TASK_PENDING = 'pending'      # 待领取（范围不持锁）
TASK_IN_PROGRESS = 'in_progress'
TASK_COMPLETED = 'completed'
TASK_STATUS_CHOICES = [
    (TASK_PENDING, '待领取'),
    (TASK_IN_PROGRESS, '盘点中'),
    (TASK_COMPLETED, '已完成'),
]

# 盘点结果
RESULT_NORMAL = 'normal'
RESULT_ABNORMAL = 'abnormal'
RESULT_CHOICES = [
    (RESULT_NORMAL, '正常'),
    (RESULT_ABNORMAL, '异常'),
]

# 复核状态
REVIEW_PENDING = 'pending'
REVIEW_RESOLVED = 'resolved'
REVIEW_STATUS_CHOICES = [
    (REVIEW_PENDING, '待复核'),
    (REVIEW_RESOLVED, '已复核'),
]

# 异常类型
ABNORMAL_QUANTITY = 'quantity'   # 数量不符
ABNORMAL_STATUS = 'status'       # 状态异常（损坏/封存等）
ABNORMAL_MISSING = 'missing'     # 物资缺失
ABNORMAL_OTHER = 'other'
ABNORMAL_TYPE_CHOICES = [
    (ABNORMAL_QUANTITY, '数量不符'),
    (ABNORMAL_STATUS, '状态异常'),
    (ABNORMAL_MISSING, '物资缺失'),
    (ABNORMAL_OTHER, '其他异常'),
]

# 默认每任务物资上限
DEFAULT_CHUNK_SIZE = 50


class StocktakeBatch(models.Model):
    """盘点批次：主管按区域、风险、截止日期生成"""

    name = models.CharField('批次名称', max_length=100)
    # 范围条件（locations 为空列表表示全部区域；risk_levels 为空表示不限风险）
    locations = models.JSONField('盘点区域', default=list)
    risk_levels = models.JSONField('风险等级', default=list)
    deadline = models.DateTimeField('截止日期')
    chunk_size = models.PositiveIntegerField('任务分块大小', default=DEFAULT_CHUNK_SIZE)

    status = models.CharField('批次状态', max_length=20, choices=BATCH_STATUS_CHOICES, default=BATCH_DRAFT)
    scope_version = models.PositiveIntegerField('范围版本', default=1)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stocktake_batches', verbose_name='创建主管'
    )
    conclusion = models.TextField('批次结论', blank=True, default='')
    concluded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='concluded_batches', verbose_name='结论确认人'
    )
    concluded_at = models.DateTimeField('结论形成时间', null=True, blank=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'iv_stocktake_batch'
        verbose_name = '盘点批次'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    # ---------- 范围计算 ----------

    def scope_queryset(self):
        """按区域/风险匹配当前在册（启用）物资，按区域排序便于拆分。

        风险规则：数量低于或等于预警阈值为高风险；无存放位置为中风险；其余为低风险。
        """
        qs = Goods.objects.filter(is_active=True)
        if self.locations:
            qs = qs.filter(location__in=list(self.locations))
        risk_set = set(self.risk_levels or [])
        if risk_set:
            condition = Q()
            if RISK_HIGH in risk_set:
                condition |= Q(quantity__lte=models.F('warning_threshold'))
            if RISK_MEDIUM in risk_set:
                condition |= Q(location='') & ~Q(quantity__lte=models.F('warning_threshold'))
            if RISK_LOW in risk_set:
                condition |= ~Q(location='') & ~Q(quantity__lte=models.F('warning_threshold'))
            qs = qs.filter(condition)
        return qs.order_by('location', 'id')

    @staticmethod
    def risk_level_of(goods):
        """评估单件物资的风险等级"""
        if goods.quantity <= goods.warning_threshold:
            return RISK_HIGH
        if not goods.location:
            return RISK_MEDIUM
        return RISK_LOW

    # ---------- 状态与进度 ----------

    def refresh_status(self, save=True):
        """根据任务进展重算批次状态（已完成批次不再回退）。

        只要有任务被领取过或产生了盘点条目，即视为盘点中——中断释放不会抹掉进度。
        """
        if self.status == BATCH_COMPLETED:
            return self.status
        has_started = (
            self.tasks.exclude(status=TASK_PENDING).exists()
            or self.tasks.filter(claim_count__gt=0).exists()
            or StocktakeItem.objects.filter(task__batch=self).exists()
        )
        self.status = BATCH_IN_PROGRESS if has_started else BATCH_DRAFT
        if save:
            self.save(update_fields=['status', 'updated_at'])
        return self.status

    @property
    def total_goods_count(self):
        """批次范围应覆盖的物资总数（含各次范围增补）。"""
        return self.tasks.aggregate(total=models.Sum('total_count'))['total'] or 0

    @property
    def counted_goods_count(self):
        """已盘点条目数（按物资去重，不重复计数）。"""
        return StocktakeItem.objects.filter(task__batch=self).values('goods_id').distinct().count()

    @property
    def abnormal_goods_count(self):
        return StocktakeItem.objects.filter(
            task__batch=self, result=RESULT_ABNORMAL
        ).values('goods_id').distinct().count()

    @property
    def pending_review_count(self):
        return StocktakeReview.objects.filter(
            item__task__batch=self, status=REVIEW_PENDING
        ).count()

    @property
    def all_tasks_completed(self):
        return self.tasks.exists() and not self.tasks.exclude(status=TASK_COMPLETED).exists()

    @property
    def can_conclude(self):
        """所有任务完成且无待复核项，才能形成批次结论。"""
        return self.all_tasks_completed and self.pending_review_count == 0

    def pending_scopes(self):
        """尚未完成的具体范围：按区域聚合未盘物资及阻塞任务。

        批次查询据此指出“哪个区域的哪件物资、卡在哪一个任务上”。
        """
        counted_ids = set(
            StocktakeItem.objects.filter(task__batch=self).values_list('goods_id', flat=True)
        )
        result = {}
        for task in self.tasks.exclude(status=TASK_COMPLETED).prefetch_related('task_goods__goods'):
            missing = [
                {
                    'id': tg.goods_id,
                    'code': tg.goods.code,
                    'name': tg.goods.name,
                }
                for tg in task.task_goods.all()
                if tg.goods_id not in counted_ids
            ]
            location = task.location or '(未分区)'
            bucket = result.setdefault(location, {
                'location': location,
                'task_id': task.id,
                'task_seq': task.seq,
                'task_status': task.status,
                'assignee_id': task.assignee_id,
                'assignee_name': task.assignee.username if task.assignee_id else None,
                'missing_goods': [],
                'missing_count': 0,
            })
            bucket['missing_goods'].extend(missing)
            bucket['missing_count'] += len(missing)
        return list(result.values())

    def scope_diff(self):
        """当前在册物资与批次已覆盖范围的差异（发现遗漏区域与新增物资）。"""
        current_ids = set(self.scope_queryset().values_list('id', flat=True))
        covered_ids = set(TaskGoods.objects.filter(task__batch=self).values_list('goods_id', flat=True))
        missing = Goods.objects.filter(id__in=current_ids - covered_ids)
        extra = Goods.objects.filter(id__in=covered_ids - current_ids)
        return {
            'uncovered': list(missing.values('id', 'code', 'name', 'location')),
            'out_of_scope': list(extra.values('id', 'code', 'name', 'location')),
        }


class StocktakeTask(models.Model):
    """盘点任务：批次范围拆分出的可执行单元。"""

    batch = models.ForeignKey(
        StocktakeBatch, on_delete=models.CASCADE,
        related_name='tasks', verbose_name='盘点批次'
    )
    seq = models.CharField('任务编号', max_length=30)
    location = models.CharField('盘点区域', max_length=100, blank=True, default='')
    risk_level = models.CharField('风险等级', max_length=10, choices=RISK_CHOICES, default=RISK_LOW)
    scope_version = models.PositiveIntegerField('所属范围版本', default=1)
    is_supplement = models.BooleanField('是否范围增补任务', default=False)

    status = models.CharField('任务状态', max_length=20, choices=TASK_STATUS_CHOICES, default=TASK_PENDING)
    assignee = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stocktake_tasks', verbose_name='盘点员'
    )
    claimed_at = models.DateTimeField('最近领取时间', null=True, blank=True)
    released_at = models.DateTimeField('最近释放时间', null=True, blank=True)
    completed_at = models.DateTimeField('完成时间', null=True, blank=True)
    claim_count = models.PositiveIntegerField('累计领取次数', default=0)
    total_count = models.PositiveIntegerField('应盘数量', default=0)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'iv_stocktake_task'
        verbose_name = '盘点任务'
        verbose_name_plural = verbose_name
        ordering = ['batch_id', 'seq']

    def __str__(self):
        return f"{self.batch.name}-{self.seq}"

    @property
    def goods_ids(self):
        return list(self.task_goods.values_list('goods_id', flat=True))

    @property
    def counted_count(self):
        return self.items.count()

    @property
    def is_locked(self):
        """范围是否处于锁定态：盘点中且未释放。"""
        return self.status == TASK_IN_PROGRESS and self.released_at is None

    def attach_goods(self, goods_list):
        TaskGoods.objects.bulk_create(
            [TaskGoods(task=self, goods=g) for g in goods_list], ignore_conflicts=True
        )
        self.total_count = self.task_goods.count()
        self.save(update_fields=['total_count', 'updated_at'])


class TaskGoods(models.Model):
    """任务-物资关联：任务持有的范围；is_locked=True 即该物资被进行中任务锁定。"""

    task = models.ForeignKey(
        StocktakeTask, on_delete=models.CASCADE,
        related_name='task_goods', verbose_name='盘点任务'
    )
    goods = models.ForeignKey(
        Goods, on_delete=models.PROTECT,
        related_name='stocktake_locks', verbose_name='物资'
    )
    is_locked = models.BooleanField('是否被进行中任务锁定', default=False)
    created_at = models.DateTimeField('加入时间', auto_now_add=True)

    class Meta:
        db_table = 'iv_task_goods'
        verbose_name = '任务物资范围'
        verbose_name_plural = verbose_name
        constraints = [
            # 核心防重锁：同一物资在任意批次中只能被一个进行中任务锁定
            models.UniqueConstraint(
                fields=['goods'],
                condition=Q(is_locked=True),
                name='iv_task_goods_active_lock',
            ),
        ]

    def __str__(self):
        return f"任务{self.task_id}-物资{self.goods_id}"


class StocktakeItem(models.Model):
    """盘点条目：每件物资的盘点结果。(任务, 物资) 唯一，重复提交走更新而非新增。"""

    task = models.ForeignKey(
        StocktakeTask, on_delete=models.CASCADE,
        related_name='items', verbose_name='盘点任务'
    )
    goods = models.ForeignKey(
        Goods, on_delete=models.PROTECT,
        related_name='stocktake_items', verbose_name='物资'
    )
    result = models.CharField('盘点结果', max_length=10, choices=RESULT_CHOICES, default=RESULT_NORMAL)
    actual_quantity = models.DecimalField(
        '实盘数量', max_digits=12, decimal_places=2, null=True, blank=True
    )
    abnormal_type = models.CharField(
        '异常类型', max_length=20, choices=ABNORMAL_TYPE_CHOICES, blank=True, default=''
    )
    remark = models.TextField('备注', blank=True, default='')

    counted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stocktake_items', verbose_name='盘点人'
    )
    counted_at = models.DateTimeField('最近盘点时间', auto_now=True)
    created_at = models.DateTimeField('首次提交时间', auto_now_add=True)
    revision = models.PositiveIntegerField('提交版本', default=1)

    class Meta:
        db_table = 'iv_stocktake_item'
        verbose_name = '盘点条目'
        verbose_name_plural = verbose_name
        # 重复提交不重复计数：同一任务同一物资只有一行
        unique_together = [('task', 'goods')]

    def __str__(self):
        return f"{self.task_id}-{self.goods_id}:{self.result}"


class StocktakeReview(models.Model):
    """异常复核记录：异常项进入复核，闭环后批次方可形成结论。"""

    item = models.OneToOneField(
        StocktakeItem, on_delete=models.CASCADE,
        related_name='review', verbose_name='盘点条目'
    )
    status = models.CharField('复核状态', max_length=20, choices=REVIEW_STATUS_CHOICES, default=REVIEW_PENDING)
    reviewer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stocktake_reviews', verbose_name='复核人'
    )
    review_note = models.TextField('复核意见', blank=True, default='')
    reviewed_at = models.DateTimeField('复核时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'iv_stocktake_review'
        verbose_name = '异常复核'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"复核-{self.item_id}:{self.status}"
