"""
盘点管理模型
"""
from django.db import models
from django.db.models import Q
from apps.authentication.models import User
from apps.warehouse.models import Goods


class StorageArea(models.Model):
    """保管区模型"""
    RISK_CHOICES = [
        ('low', '低风险'),
        ('medium', '中风险'),
        ('high', '高风险'),
    ]

    name = models.CharField('区域名称', max_length=50, unique=True)
    code = models.CharField('区域编码', max_length=20, unique=True)
    risk_level = models.CharField('风险等级', max_length=10, choices=RISK_CHOICES, default='medium')
    description = models.CharField('区域说明', max_length=200, blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'iv_storage_area'
        verbose_name = '保管区'
        verbose_name_plural = verbose_name
        ordering = ['code']

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def is_linked(self):
        """是否已关联货物或盘点任务"""
        return self.goods.exists() or self.inventory_tasks.exists()


class InventoryBatch(models.Model):
    """盘点批次模型（主管按区域、风险、截止日期生成）"""
    STATUS_CHOICES = [
        ('open', '进行中'),
        ('concluded', '已结论'),
    ]

    batch_no = models.CharField('批次号', max_length=30, unique=True)
    title = models.CharField('批次名称', max_length=100)
    risk_levels = models.JSONField('风险等级筛选', default=list, blank=True)
    deadline = models.DateField('截止日期')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='open')
    conclusion = models.JSONField('批次结论', null=True, blank=True)
    concluded_at = models.DateTimeField('结论形成时间', null=True, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='inventory_batches', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'iv_batch'
        verbose_name = '盘点批次'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.batch_no} - {self.title}"


class InventoryTask(models.Model):
    """盘点任务模型（批次按保管区拆分出的可执行单元）"""
    STATUS_CHOICES = [
        ('pending', '待领取'),
        ('claimed', '进行中'),
        ('done', '已完成'),
    ]

    task_no = models.CharField('任务号', max_length=40, unique=True)
    batch = models.ForeignKey(
        InventoryBatch, on_delete=models.CASCADE,
        related_name='tasks', verbose_name='所属批次'
    )
    area = models.ForeignKey(
        StorageArea, on_delete=models.PROTECT,
        related_name='inventory_tasks', verbose_name='保管区'
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    assignee = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inventory_tasks', verbose_name='盘点员'
    )
    claimed_at = models.DateTimeField('领取时间', null=True, blank=True)
    finished_at = models.DateTimeField('完成时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'iv_task'
        verbose_name = '盘点任务'
        verbose_name_plural = verbose_name
        ordering = ['task_no']

    def __str__(self):
        return f"{self.task_no} ({self.area.name})"


class InventoryTaskItem(models.Model):
    """任务明细模型（领取范围快照，进度以明细状态为准）"""
    STATUS_CHOICES = [
        ('pending', '待盘点'),
        ('counted', '已盘点'),
        ('abnormal', '异常待复核'),
        ('reviewed', '已复核'),
        ('removed', '已移出'),
    ]

    task = models.ForeignKey(
        InventoryTask, on_delete=models.CASCADE,
        related_name='items', verbose_name='所属任务'
    )
    goods = models.ForeignKey(
        Goods, on_delete=models.PROTECT,
        related_name='inventory_items', verbose_name='货物'
    )
    book_quantity = models.DecimalField('账面数量', max_digits=12, decimal_places=2)
    counted_quantity = models.DecimalField('盘点数量', max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    removed_reason = models.CharField('移出原因', max_length=100, blank=True)
    counted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='counted_items', verbose_name='盘点人'
    )
    counted_at = models.DateTimeField('盘点时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'iv_task_item'
        verbose_name = '任务明细'
        verbose_name_plural = verbose_name
        ordering = ['id']
        unique_together = ['task', 'goods']

    def __str__(self):
        return f"{self.task.task_no} - {self.goods.name}"


class InventorySubmission(models.Model):
    """分段提交单模型（按请求标识幂等，重复提交不重复计数）"""
    task = models.ForeignKey(
        InventoryTask, on_delete=models.CASCADE,
        related_name='submissions', verbose_name='所属任务'
    )
    request_id = models.CharField('请求标识', max_length=64)
    submitted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='inventory_submissions', verbose_name='提交人'
    )
    summary = models.JSONField('提交结果摘要', default=dict, blank=True)
    created_at = models.DateTimeField('提交时间', auto_now_add=True)

    class Meta:
        db_table = 'iv_submission'
        verbose_name = '分段提交单'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['task', 'request_id']

    def __str__(self):
        return f"{self.task.task_no} - {self.request_id}"


class InventoryRecord(models.Model):
    """盘点记录模型（每次提交的明细流水，仅作审计不参与计数）"""
    RESULT_CHOICES = [
        ('normal', '账实相符'),
        ('abnormal', '账实异常'),
    ]

    submission = models.ForeignKey(
        InventorySubmission, on_delete=models.CASCADE,
        related_name='records', verbose_name='所属提交单'
    )
    item = models.ForeignKey(
        InventoryTaskItem, on_delete=models.CASCADE,
        related_name='records', verbose_name='任务明细'
    )
    counted_quantity = models.DecimalField('盘点数量', max_digits=12, decimal_places=2)
    result = models.CharField('盘点结果', max_length=20, choices=RESULT_CHOICES)
    remark = models.CharField('备注', max_length=500, blank=True)
    created_at = models.DateTimeField('记录时间', auto_now_add=True)

    class Meta:
        db_table = 'iv_record'
        verbose_name = '盘点记录'
        verbose_name_plural = verbose_name
        ordering = ['id']

    def __str__(self):
        return f"{self.item} - {self.counted_quantity}"


class InventoryReview(models.Model):
    """异常复核单模型"""
    STATUS_CHOICES = [
        ('pending', '待复核'),
        ('confirmed', '确认差异'),
        ('recount', '退回重盘'),
    ]

    item = models.ForeignKey(
        InventoryTaskItem, on_delete=models.CASCADE,
        related_name='reviews', verbose_name='任务明细'
    )
    record = models.ForeignKey(
        InventoryRecord, on_delete=models.CASCADE,
        related_name='reviews', verbose_name='异常记录'
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    final_quantity = models.DecimalField('核定数量', max_digits=12, decimal_places=2, null=True, blank=True)
    review_note = models.CharField('复核意见', max_length=500, blank=True)
    reviewer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='inventory_reviews', verbose_name='复核人'
    )
    reviewed_at = models.DateTimeField('复核时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'iv_review'
        verbose_name = '异常复核单'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.item} - {self.get_status_display()}"


class GoodsLock(models.Model):
    """物资盘点锁（同一物资同一时间仅允许一个任务锁定，防止重复锁定）"""
    STATUS_CHOICES = [
        ('active', '锁定中'),
        ('released', '已释放'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='inventory_locks', verbose_name='货物'
    )
    task = models.ForeignKey(
        InventoryTask, on_delete=models.CASCADE,
        related_name='locks', verbose_name='锁定任务'
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='active')
    locked_at = models.DateTimeField('锁定时间', auto_now_add=True)
    released_at = models.DateTimeField('释放时间', null=True, blank=True)
    release_reason = models.CharField('释放原因', max_length=50, blank=True)

    class Meta:
        db_table = 'iv_goods_lock'
        verbose_name = '物资盘点锁'
        verbose_name_plural = verbose_name
        ordering = ['-locked_at']
        constraints = [
            models.UniqueConstraint(
                fields=['goods'],
                condition=Q(status='active'),
                name='uniq_active_goods_lock'
            )
        ]

    def __str__(self):
        return f"{self.goods.name} - {self.task.task_no}"
