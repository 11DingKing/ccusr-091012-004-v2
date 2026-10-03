"""
盘点管理序列化器
"""
from django.utils import timezone
from rest_framework import serializers
from .models import (
    StorageArea, InventoryBatch, InventoryTask, InventoryTaskItem,
    InventorySubmission, InventoryRecord, InventoryReview,
)


class StorageAreaSerializer(serializers.ModelSerializer):
    """保管区序列化器"""
    risk_level_display = serializers.CharField(source='get_risk_level_display', read_only=True)
    goods_count = serializers.SerializerMethodField()

    class Meta:
        model = StorageArea
        fields = [
            'id', 'name', 'code', 'risk_level', 'risk_level_display',
            'description', 'is_active', 'goods_count', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_goods_count(self, obj):
        return obj.goods.filter(is_active=True).count()


class StorageAreaCreateSerializer(serializers.Serializer):
    """保管区创建/更新序列化器"""
    name = serializers.CharField(min_length=1, max_length=50, required=True, error_messages={
        'required': '请输入区域名称',
        'blank': '区域名称不能为空',
        'max_length': '区域名称最多50个字',
    })
    code = serializers.CharField(min_length=1, max_length=20, required=True, error_messages={
        'required': '请输入区域编码',
        'blank': '区域编码不能为空',
        'max_length': '区域编码最多20个字',
    })
    risk_level = serializers.ChoiceField(
        choices=['low', 'medium', 'high'], default='medium',
        error_messages={'invalid_choice': '风险等级无效'}
    )
    description = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')

    def validate_name(self, value):
        instance = self.context.get('instance')
        queryset = StorageArea.objects.filter(name=value)
        if instance:
            queryset = queryset.exclude(pk=instance.pk)
        if queryset.exists():
            raise serializers.ValidationError('区域名称已存在')
        return value

    def validate_code(self, value):
        instance = self.context.get('instance')
        queryset = StorageArea.objects.filter(code=value)
        if instance:
            queryset = queryset.exclude(pk=instance.pk)
        if queryset.exists():
            raise serializers.ValidationError('区域编码已存在')
        return value


class BatchCreateSerializer(serializers.Serializer):
    """批次创建序列化器（按区域、风险、截止日期生成）"""
    title = serializers.CharField(min_length=1, max_length=100, required=True, error_messages={
        'required': '请输入批次名称',
        'blank': '批次名称不能为空',
        'max_length': '批次名称最多100个字',
    })
    area_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True, default=list,
        error_messages={'not_a_list': '区域列表格式错误'}
    )
    risk_levels = serializers.ListField(
        child=serializers.ChoiceField(choices=['low', 'medium', 'high']),
        required=False, allow_empty=True, default=list,
        error_messages={'not_a_list': '风险等级列表格式错误'}
    )
    deadline = serializers.DateField(required=True, error_messages={
        'required': '请选择截止日期',
        'invalid': '截止日期格式错误',
    })

    def validate_area_ids(self, value):
        existing = set(StorageArea.objects.filter(id__in=value, is_active=True).values_list('id', flat=True))
        missing = [area_id for area_id in value if area_id not in existing]
        if missing:
            raise serializers.ValidationError(f'保管区不存在或已停用: {missing}')
        return value

    def validate_deadline(self, value):
        if value < timezone.localdate():
            raise serializers.ValidationError('截止日期不能早于今天')
        return value


class InventoryTaskItemSerializer(serializers.ModelSerializer):
    """任务明细序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    counted_by_name = serializers.CharField(source='counted_by.username', read_only=True)

    class Meta:
        model = InventoryTaskItem
        fields = [
            'id', 'task', 'goods', 'goods_name', 'goods_code',
            'book_quantity', 'counted_quantity', 'status', 'status_display',
            'removed_reason', 'counted_by', 'counted_by_name', 'counted_at'
        ]


class InventoryTaskSerializer(serializers.ModelSerializer):
    """盘点任务序列化器"""
    batch_no = serializers.CharField(source='batch.batch_no', read_only=True)
    area_name = serializers.CharField(source='area.name', read_only=True)
    area_code = serializers.CharField(source='area.code', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    assignee_name = serializers.CharField(source='assignee.username', read_only=True)
    total_items = serializers.SerializerMethodField()
    done_items = serializers.SerializerMethodField()

    class Meta:
        model = InventoryTask
        fields = [
            'id', 'task_no', 'batch', 'batch_no', 'area', 'area_name', 'area_code',
            'status', 'status_display', 'assignee', 'assignee_name',
            'total_items', 'done_items',
            'claimed_at', 'finished_at', 'created_at'
        ]

    def _items(self, obj):
        return obj.items.exclude(status='removed')

    def get_total_items(self, obj):
        return self._items(obj).count()

    def get_done_items(self, obj):
        return self._items(obj).filter(status__in=['counted', 'reviewed']).count()


class InventoryBatchSerializer(serializers.ModelSerializer):
    """盘点批次序列化器"""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    areas = serializers.SerializerMethodField()
    task_total = serializers.SerializerMethodField()
    task_done = serializers.SerializerMethodField()
    total_items = serializers.SerializerMethodField()
    done_items = serializers.SerializerMethodField()
    abnormal_open = serializers.SerializerMethodField()

    class Meta:
        model = InventoryBatch
        fields = [
            'id', 'batch_no', 'title', 'risk_levels', 'deadline',
            'status', 'status_display', 'areas',
            'task_total', 'task_done', 'total_items', 'done_items', 'abnormal_open',
            'conclusion', 'concluded_at',
            'created_by', 'created_by_name', 'created_at'
        ]

    def _items(self, obj):
        return InventoryTaskItem.objects.filter(task__batch=obj).exclude(status='removed')

    def get_areas(self, obj):
        return [
            {'id': task.area_id, 'name': task.area.name, 'code': task.area.code}
            for task in obj.tasks.select_related('area').order_by('task_no')
        ]

    def get_task_total(self, obj):
        return obj.tasks.count()

    def get_task_done(self, obj):
        return obj.tasks.filter(status='done').count()

    def get_total_items(self, obj):
        return self._items(obj).count()

    def get_done_items(self, obj):
        return self._items(obj).filter(status__in=['counted', 'reviewed']).count()

    def get_abnormal_open(self, obj):
        return self._items(obj).filter(status='abnormal').count()


class InventoryRecordSerializer(serializers.ModelSerializer):
    """盘点记录序列化器"""
    goods_name = serializers.CharField(source='item.goods.name', read_only=True)
    goods_code = serializers.CharField(source='item.goods.code', read_only=True)
    result_display = serializers.CharField(source='get_result_display', read_only=True)
    submitted_by_name = serializers.CharField(source='submission.submitted_by.username', read_only=True)

    class Meta:
        model = InventoryRecord
        fields = [
            'id', 'submission', 'item', 'goods_name', 'goods_code',
            'counted_quantity', 'result', 'result_display', 'remark',
            'submitted_by_name', 'created_at'
        ]


class InventorySubmissionSerializer(serializers.ModelSerializer):
    """分段提交单序列化器"""
    submitted_by_name = serializers.CharField(source='submitted_by.username', read_only=True)
    records = InventoryRecordSerializer(many=True, read_only=True)

    class Meta:
        model = InventorySubmission
        fields = [
            'id', 'task', 'request_id', 'submitted_by', 'submitted_by_name',
            'summary', 'records', 'created_at'
        ]


class InventoryReviewSerializer(serializers.ModelSerializer):
    """异常复核单序列化器"""
    goods_name = serializers.CharField(source='item.goods.name', read_only=True)
    goods_code = serializers.CharField(source='item.goods.code', read_only=True)
    task_no = serializers.CharField(source='item.task.task_no', read_only=True)
    area_name = serializers.CharField(source='item.task.area.name', read_only=True)
    book_quantity = serializers.DecimalField(source='item.book_quantity', max_digits=12, decimal_places=2, read_only=True)
    counted_quantity = serializers.DecimalField(source='record.counted_quantity', max_digits=12, decimal_places=2, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    reviewer_name = serializers.CharField(source='reviewer.username', read_only=True)

    class Meta:
        model = InventoryReview
        fields = [
            'id', 'item', 'record', 'goods_name', 'goods_code',
            'task_no', 'area_name', 'book_quantity', 'counted_quantity',
            'status', 'status_display', 'final_quantity', 'review_note',
            'reviewer', 'reviewer_name', 'reviewed_at', 'created_at'
        ]


class SubmitEntrySerializer(serializers.Serializer):
    """分段提交明细"""
    item_id = serializers.IntegerField(required=True, error_messages={'required': '缺少明细ID'})
    counted_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=True,
        error_messages={'required': '请输入盘点数量'}
    )
    remark = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class SubmitSerializer(serializers.Serializer):
    """分段提交序列化器（request_id 保证幂等）"""
    request_id = serializers.CharField(min_length=1, max_length=64, required=True, error_messages={
        'required': '缺少请求标识',
        'blank': '请求标识不能为空',
    })
    items = SubmitEntrySerializer(many=True, allow_empty=False, error_messages={
        'required': '请提交盘点明细',
        'empty': '盘点明细不能为空',
    })

    def validate_items(self, value):
        item_ids = [entry['item_id'] for entry in value]
        if len(item_ids) != len(set(item_ids)):
            raise serializers.ValidationError('提交中存在重复明细')
        return value


class ReassignSerializer(serializers.Serializer):
    """重新分配序列化器"""
    user_id = serializers.IntegerField(required=True, error_messages={'required': '请选择盘点员'})


class ReviewResolveSerializer(serializers.Serializer):
    """复核处理序列化器"""
    action = serializers.ChoiceField(choices=['confirm', 'recount'], required=True, error_messages={
        'required': '请选择处理方式',
        'invalid_choice': '处理方式无效',
    })
    final_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )
    note = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')
