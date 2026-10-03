"""
盘点管理序列化器
"""
from rest_framework import serializers
from django.db.models import Count

from apps.authentication.models import User
from apps.warehouse.models import Goods

from .models import (
    StocktakeBatch, StocktakeTask, TaskGoods, StocktakeItem, StocktakeReview,
    RISK_CHOICES,
    RESULT_CHOICES, ABNORMAL_TYPE_CHOICES,
    DEFAULT_CHUNK_SIZE,
)


class BatchCreateSerializer(serializers.Serializer):
    """主管生成盘点批次入参"""

    name = serializers.CharField(min_length=1, max_length=100, required=True,
                                 error_messages={'blank': '批次名称不能为空', 'required': '请输入批次名称'})
    locations = serializers.ListField(
        child=serializers.CharField(max_length=100), required=False, default=list,
        help_text='盘点区域列表，留空表示全部区域',
    )
    risk_levels = serializers.ListField(
        child=serializers.ChoiceField(choices=[c[0] for c in RISK_CHOICES]),
        required=False, default=list, allow_empty=True,
        help_text='风险等级列表，留空表示不限风险',
    )
    deadline = serializers.DateTimeField(required=True, error_messages={'required': '请选择盘点截止日期'})
    chunk_size = serializers.IntegerField(required=False, default=DEFAULT_CHUNK_SIZE, min_value=1, max_value=500)

    def validate_locations(self, value):
        return [v.strip() for v in value if v and v.strip()]


class ReviewResolveSerializer(serializers.Serializer):
    """复核闭环入参"""

    note = serializers.CharField(required=False, default='', allow_blank=True, max_length=1000)


class SubmitEntrySerializer(serializers.Serializer):
    """单件物资盘点结果"""

    goods = serializers.PrimaryKeyRelatedField(queryset=Goods.objects.all())
    result = serializers.ChoiceField(choices=RESULT_CHOICES)
    actual_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=0,
    )
    abnormal_type = serializers.ChoiceField(choices=ABNORMAL_TYPE_CHOICES, required=False, default='')
    remark = serializers.CharField(required=False, default='', allow_blank=True, max_length=1000)

    def validate(self, attrs):
        if attrs['result'] == 'abnormal' and not attrs.get('abnormal_type'):
            raise serializers.ValidationError({'abnormal_type': '异常项必须选择异常类型'})
        return attrs


class SubmitItemsSerializer(serializers.Serializer):
    """分段提交入参（可多次调用，每次提交一个分段；同一物资重复提交按更新处理）"""

    entries = SubmitEntrySerializer(many=True, allow_empty=False)


class ReassignSerializer(serializers.Serializer):
    """任务重新分配入参"""

    assignee = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        required=True, error_messages={'required': '请指定盘点员'},
    )


class ConcludeSerializer(serializers.Serializer):
    """形成批次结论入参"""

    conclusion = serializers.CharField(required=False, default='', allow_blank=True, max_length=2000)


class GoodsBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Goods
        fields = ['id', 'code', 'name', 'location', 'quantity', 'warning_threshold']


class TaskGoodsSerializer(serializers.ModelSerializer):
    goods = GoodsBriefSerializer(read_only=True)

    class Meta:
        model = TaskGoods
        fields = ['id', 'goods', 'is_locked']


class ReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.CharField(source='reviewer.username', read_only=True, default=None)

    class Meta:
        model = StocktakeReview
        fields = [
            'id', 'item', 'status', 'reviewer', 'reviewer_name',
            'review_note', 'reviewed_at', 'created_at',
        ]


class ItemSerializer(serializers.ModelSerializer):
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    location = serializers.CharField(source='goods.location', read_only=True)
    counted_by_name = serializers.CharField(source='counted_by.username', read_only=True, default=None)
    review = ReviewSerializer(read_only=True)

    class Meta:
        model = StocktakeItem
        fields = [
            'id', 'task', 'goods', 'goods_code', 'goods_name', 'location',
            'result', 'actual_quantity', 'abnormal_type', 'remark',
            'counted_by', 'counted_by_name', 'counted_at', 'created_at', 'revision',
            'review',
        ]


class TaskSerializer(serializers.ModelSerializer):
    assignee_name = serializers.CharField(source='assignee.username', read_only=True, default=None)
    is_locked = serializers.BooleanField(read_only=True)
    counted_count = serializers.IntegerField(read_only=True)
    goods_scope = serializers.SerializerMethodField()

    class Meta:
        model = StocktakeTask
        fields = [
            'id', 'batch', 'seq', 'location', 'risk_level',
            'scope_version', 'is_supplement',
            'status', 'assignee', 'assignee_name', 'is_locked',
            'claimed_at', 'released_at', 'completed_at', 'claim_count',
            'total_count', 'counted_count', 'created_at',
            'goods_scope',
        ]

    def get_goods_scope(self, obj):
        # 仅返回物资ID；任务详情接口会展开为含编码/名称/账面数量的明细
        return list(obj.task_goods.values_list('goods_id', flat=True))


class TaskListSerializer(TaskSerializer):
    """列表场景别名，行为同基类"""
    pass


class BatchSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.username', read_only=True, default=None)
    total_count = serializers.IntegerField(source='total_goods_count', read_only=True)
    counted_count = serializers.SerializerMethodField()
    abnormal_count = serializers.SerializerMethodField()
    pending_review_count = serializers.IntegerField(read_only=True)
    all_tasks_completed = serializers.BooleanField(read_only=True)
    can_conclude = serializers.BooleanField(read_only=True)
    task_stats = serializers.SerializerMethodField()

    class Meta:
        model = StocktakeBatch
        fields = [
            'id', 'name', 'locations', 'risk_levels', 'deadline', 'chunk_size',
            'status', 'scope_version',
            'created_by', 'created_by_name', 'created_at', 'updated_at',
            'conclusion', 'concluded_by', 'concluded_at',
            'total_count', 'counted_count', 'abnormal_count',
            'pending_review_count', 'all_tasks_completed', 'can_conclude',
            'task_stats',
        ]

    def get_counted_count(self, obj):
        return obj.counted_goods_count

    def get_abnormal_count(self, obj):
        return obj.abnormal_goods_count

    def get_task_stats(self, obj):
        stats = {'pending': 0, 'in_progress': 0, 'completed': 0}
        for row in obj.tasks.values('status').annotate(n=Count('id')):
            stats[row['status']] = row['n']
        return stats
