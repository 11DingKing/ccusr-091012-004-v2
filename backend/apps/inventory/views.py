"""
盘点管理视图

路由划分：
- 主管：批次生成/范围增补/形成结论、任务转派、异常复核闭环；
- 盘点员：任务池查询、领取锁定、分段提交、完成；
- 批次详情始终给出未完成的具体范围（pending_scopes）与范围差异（scope_diff）。
"""
import logging

from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.response import success_response, error_response
from apps.core.exceptions import BusinessException

from . import services
from .models import (
    StocktakeBatch, StocktakeTask, StocktakeReview,
    REVIEW_PENDING,
)
from .serializers import (
    BatchCreateSerializer, BatchSerializer,
    TaskSerializer, TaskListSerializer, ItemSerializer, ReviewSerializer,
    SubmitItemsSerializer, ReassignSerializer, ConcludeSerializer, ReviewResolveSerializer,
)

logger = logging.getLogger('apps')


def _require_admin(user):
    if not user.is_admin:
        raise BusinessException('该操作仅主管可执行', code=403)


def _first_error(serializer):
    errors = serializer.errors
    value = list(errors.values())[0]
    if isinstance(value, list):
        value = value[0]
    if isinstance(value, dict):
        value = list(value.values())[0]
        if isinstance(value, list):
            value = value[0]
    return str(value)


def _page_params(request):
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = min(max(int(request.query_params.get('page_size', 10)), 1), 200)
    return page, page_size


# ==================== 盘点批次 ====================

class BatchListCreateView(APIView):
    """批次列表 / 主管生成批次"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = StocktakeBatch.objects.all().select_related('created_by')
        status = request.query_params.get('status')
        if status:
            qs = qs.filter(status=status)
        location = request.query_params.get('location')
        if location:
            qs = qs.filter(locations__contains=[location])

        page, page_size = _page_params(request)
        total = qs.count()
        batches = qs[(page - 1) * page_size:page * page_size]
        return success_response(data={
            'list': BatchSerializer(batches, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        _require_admin(request.user)
        serializer = BatchCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data
        batch = services.create_batch(
            name=data['name'],
            locations=data['locations'],
            risk_levels=data['risk_levels'],
            deadline=data['deadline'],
            chunk_size=data['chunk_size'],
            created_by=request.user,
        )
        logger.info("主管 %s 生成盘点批次 %s", request.user.username, batch.id)
        return success_response(data=BatchSerializer(batch).data, message='批次创建成功，已自动拆分盘点任务')


class BatchDetailView(APIView):
    """批次详情：进度、任务概览、未完成的具体范围、范围差异"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        batch = services.get_batch_or_404(pk)
        data = BatchSerializer(batch).data
        tasks = batch.tasks.select_related('assignee')
        data['tasks'] = TaskListSerializer(tasks, many=True).data
        # 批次查询必须能指出尚未完成的具体范围
        data['pending_scopes'] = batch.pending_scopes()
        data['scope_diff'] = batch.scope_diff()
        return success_response(data=data)


class BatchRefreshScopeView(APIView):
    """主管处理范围变化：生成增补任务，历史进度不动"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        _require_admin(request.user)
        batch = services.get_batch_or_404(pk)
        result = services.refresh_scope(batch)
        return success_response(
            data={
                'new_task_count': result['new_task_count'],
                'new_goods_count': result['new_goods_count'],
                'new_task_ids': [t.id for t in result['tasks']],
                'scope_version': batch.scope_version,
            },
            message='范围已同步' if result['new_task_count'] else '范围无变化',
        )


class BatchConcludeView(APIView):
    """主管在全部任务完成、复核清零后形成批次结论"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        _require_admin(request.user)
        batch = services.get_batch_or_404(pk)
        serializer = ConcludeSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        batch = services.conclude_batch(batch, request.user, serializer.validated_data['conclusion'])
        logger.info("盘点批次 %s 形成结论，操作人 %s", batch.id, request.user.username)
        return success_response(data=BatchSerializer(batch).data, message='批次结论已形成')


# ==================== 盘点任务 ====================

class TaskListView(APIView):
    """任务池：可按批次/状态/区域筛选，mine=1 只看本人任务"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = StocktakeTask.objects.all().select_related('assignee', 'batch')
        batch_id = request.query_params.get('batch')
        if batch_id:
            qs = qs.filter(batch_id=batch_id)
        status = request.query_params.get('status')
        if status:
            qs = qs.filter(status=status)
        location = request.query_params.get('location')
        if location:
            qs = qs.filter(location=location)
        if request.query_params.get('mine') in ('1', 'true'):
            qs = qs.filter(assignee=request.user)

        page, page_size = _page_params(request)
        total = qs.count()
        tasks = qs[(page - 1) * page_size:page * page_size]
        return success_response(data={
            'list': TaskListSerializer(tasks, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })


class TaskDetailView(APIView):
    """任务详情：锁定范围、已盘条目与进度"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        task = services.get_task_or_404(pk)
        data = TaskSerializer(task).data
        data['goods_scope'] = [
            {
                'id': tg.goods_id,
                'code': tg.goods.code,
                'name': tg.goods.name,
                'location': tg.goods.location,
                'book_quantity': str(tg.goods.quantity),
                'is_locked': tg.is_locked,
            }
            for tg in task.task_goods.select_related('goods')
        ]
        data['items'] = ItemSerializer(task.items.select_related('goods', 'counted_by'), many=True).data
        return success_response(data=data)


class TaskClaimView(APIView):
    """盘点员领取任务并锁定范围"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        task = services.get_task_or_404(pk)
        task = services.claim_task(task, request.user)
        return success_response(data=TaskListSerializer(task).data, message='领取成功，盘点范围已锁定')


class TaskReleaseView(APIView):
    """盘点员中断任务：释放锁、退回任务池，进度保留；主管可强制中断"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        task = services.get_task_or_404(pk)
        force = bool(request.data.get('force')) and request.user.is_admin
        task = services.release_task(task, request.user, force=force)
        return success_response(data=TaskListSerializer(task).data, message='任务已中断，已盘进度保留')


class TaskReassignView(APIView):
    """主管重新分配任务（锁与进度保持）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        _require_admin(request.user)
        task = services.get_task_or_404(pk)
        serializer = ReassignSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        task = services.reassign_task(task, serializer.validated_data['assignee'])
        return success_response(data=TaskListSerializer(task).data, message='任务已重新分配')


class TaskSubmitView(APIView):
    """盘点员分段提交：重复提交幂等更新，异常项自动进入复核"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        task = services.get_task_or_404(pk)
        force = bool(request.data.get('force')) and request.user.is_admin
        serializer = SubmitItemsSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        entries = []
        for entry in serializer.validated_data['entries']:
            entries.append({
                'goods': entry['goods'].id,
                'result': entry['result'],
                'actual_quantity': entry.get('actual_quantity'),
                'abnormal_type': entry.get('abnormal_type', ''),
                'remark': entry.get('remark', ''),
            })
        summary = services.submit_items(task, request.user, entries, force=force)
        return success_response(data=summary, message='盘点结果已提交')


class TaskCompleteView(APIView):
    """盘点员完成任务（范围全部盘完，关闭并释放锁）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        task = services.get_task_or_404(pk)
        force = bool(request.data.get('force')) and request.user.is_admin
        task = services.complete_task(task, request.user, force=force)
        return success_response(data=TaskListSerializer(task).data, message='任务已完成')


# ==================== 异常复核 ====================

class ReviewListView(APIView):
    """复核队列"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = StocktakeReview.objects.all().select_related(
            'item__goods', 'item__task', 'reviewer'
        )
        status = request.query_params.get('status', REVIEW_PENDING)
        if status and status != 'all':
            qs = qs.filter(status=status)
        batch_id = request.query_params.get('batch')
        if batch_id:
            qs = qs.filter(item__task__batch_id=batch_id)

        page, page_size = _page_params(request)
        total = qs.count()
        reviews = qs[(page - 1) * page_size:page * page_size]
        data = ReviewSerializer(reviews, many=True).data
        return success_response(data={
            'list': data, 'total': total, 'page': page, 'page_size': page_size,
        })


class ReviewResolveView(APIView):
    """主管复核闭环异常项"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        _require_admin(request.user)
        review = services.get_review_or_404(pk)
        serializer = ReviewResolveSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        review = services.resolve_review(review, request.user, serializer.validated_data['note'])
        return success_response(data=ReviewSerializer(review).data, message='复核已闭环')
