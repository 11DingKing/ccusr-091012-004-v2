"""
盘点管理视图
"""
import logging
from django.db import IntegrityError
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.authentication.models import User
from apps.core.response import success_response, error_response
from .models import StorageArea, InventoryBatch, InventoryTask, InventoryReview
from .serializers import (
    StorageAreaSerializer, StorageAreaCreateSerializer,
    BatchCreateSerializer, InventoryBatchSerializer,
    InventoryTaskSerializer, InventoryTaskItemSerializer,
    InventorySubmissionSerializer, InventoryReviewSerializer,
    SubmitSerializer, ReassignSerializer, ReviewResolveSerializer,
)
from . import services

logger = logging.getLogger('apps')


def _first_error(errors):
    """取序列化器的第一条错误信息"""
    first = list(errors.values())[0]
    if isinstance(first, list):
        first = first[0]
    if isinstance(first, dict):
        first = list(first.values())[0]
        if isinstance(first, list):
            first = first[0]
    return str(first)


def _require_admin(request):
    """主管（管理员）权限校验"""
    if not request.user.is_admin:
        return error_response(message='仅主管可执行该操作', code=403)
    return None


def _goods_brief(goods):
    return {'id': goods.id, 'code': goods.code, 'name': goods.name}


def _build_unfinished(batch):
    """批次未完成的具体范围：未完成任务及其待盘/待复核明细"""
    unfinished = []
    tasks = batch.tasks.exclude(status='done').select_related('area', 'assignee').order_by('task_no')
    for task in tasks:
        items = task.items.filter(
            status__in=services.OPEN_ITEM_STATUSES
        ).select_related('goods').order_by('id')
        unfinished.append({
            'task_id': task.id,
            'task_no': task.task_no,
            'task_status': task.status,
            'task_status_display': task.get_status_display(),
            'area_id': task.area_id,
            'area_name': task.area.name,
            'assignee': task.assignee.username if task.assignee else None,
            'pending_count': items.count(),
            'pending_items': [
                {
                    'item_id': item.id,
                    'goods_id': item.goods_id,
                    'goods_code': item.goods.code,
                    'goods_name': item.goods.name,
                    'book_quantity': str(item.book_quantity),
                    'item_status': item.status,
                }
                for item in items
            ],
        })
    return unfinished


# ==================== 保管区管理 ====================

class AreaListView(APIView):
    """保管区列表视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = StorageArea.objects.all().order_by('code')

        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        start = (page - 1) * page_size
        end = start + page_size

        total = queryset.count()
        areas = queryset[start:end]
        serializer = StorageAreaSerializer(areas, many=True)

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size
        })

    def post(self, request):
        """创建保管区"""
        denied = _require_admin(request)
        if denied:
            return denied

        serializer = StorageAreaCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        area = StorageArea.objects.create(**serializer.validated_data)
        logger.info(f"User {request.user.username} created storage area {area.name}")
        return success_response(data=StorageAreaSerializer(area).data, message='创建成功')


class AreaAllView(APIView):
    """获取所有启用的保管区（用于下拉选择）"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        areas = StorageArea.objects.filter(is_active=True).order_by('code')
        return success_response(data=StorageAreaSerializer(areas, many=True).data)


class AreaDetailView(APIView):
    """保管区详情视图"""
    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        """更新保管区"""
        denied = _require_admin(request)
        if denied:
            return denied

        try:
            area = StorageArea.objects.get(pk=pk)
        except StorageArea.DoesNotExist:
            return error_response(message='保管区不存在', code=404)

        serializer = StorageAreaCreateSerializer(data=request.data, context={'instance': area})
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        for field, value in serializer.validated_data.items():
            setattr(area, field, value)
        area.save()
        logger.info(f"User {request.user.username} updated storage area {area.name}")
        return success_response(data=StorageAreaSerializer(area).data, message='更新成功')

    def delete(self, request, pk):
        """删除保管区"""
        denied = _require_admin(request)
        if denied:
            return denied

        try:
            area = StorageArea.objects.get(pk=pk)
        except StorageArea.DoesNotExist:
            return error_response(message='保管区不存在', code=404)

        if area.is_linked:
            return error_response(message='该保管区已关联货物或盘点任务，无法删除')

        name = area.name
        area.delete()
        logger.info(f"User {request.user.username} deleted storage area {name}")
        return success_response(message='删除成功')


# ==================== 盘点批次 ====================

class BatchListView(APIView):
    """盘点批次列表视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = InventoryBatch.objects.all().order_by('-created_at')
        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)

        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        start = (page - 1) * page_size
        end = start + page_size

        total = queryset.count()
        batches = queryset[start:end]
        serializer = InventoryBatchSerializer(batches, many=True)

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size
        })

    def post(self, request):
        """生成盘点批次：按区域、风险和截止日期拆分任务"""
        denied = _require_admin(request)
        if denied:
            return denied

        serializer = BatchCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        batch, skipped_goods, uncovered_goods = services.create_batch(
            user=request.user,
            title=data['title'],
            area_ids=data['area_ids'],
            risk_levels=data['risk_levels'],
            deadline=data['deadline'],
        )

        logger.info(
            f"User {request.user.username} created inventory batch {batch.batch_no} "
            f"with {batch.tasks.count()} tasks"
        )

        return success_response(data={
            'batch': InventoryBatchSerializer(batch).data,
            'skipped_goods': [_goods_brief(g) for g in skipped_goods],
            'uncovered_goods': [_goods_brief(g) for g in uncovered_goods],
        }, message='批次已生成并拆分任务')


class BatchDetailView(APIView):
    """盘点批次详情视图（含未完成的具体范围）"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            batch = InventoryBatch.objects.get(pk=pk)
        except InventoryBatch.DoesNotExist:
            return error_response(message='盘点批次不存在', code=404)

        tasks = batch.tasks.select_related('area', 'assignee').order_by('task_no')
        return success_response(data={
            'batch': InventoryBatchSerializer(batch).data,
            'tasks': InventoryTaskSerializer(tasks, many=True).data,
            'unfinished': _build_unfinished(batch),
        })


# ==================== 盘点任务 ====================

class TaskListView(APIView):
    """盘点任务列表视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = InventoryTask.objects.select_related('batch', 'area', 'assignee').order_by('-created_at')

        batch_id = request.query_params.get('batch')
        if batch_id:
            queryset = queryset.filter(batch_id=batch_id)
        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)
        if request.query_params.get('mine') == 'true':
            queryset = queryset.filter(assignee=request.user)

        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        start = (page - 1) * page_size
        end = start + page_size

        total = queryset.count()
        tasks = queryset[start:end]
        serializer = InventoryTaskSerializer(tasks, many=True)

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size
        })


class TaskDetailView(APIView):
    """盘点任务详情视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            task = InventoryTask.objects.select_related('batch', 'area', 'assignee').get(pk=pk)
        except InventoryTask.DoesNotExist:
            return error_response(message='盘点任务不存在', code=404)

        items = task.items.select_related('goods', 'counted_by').order_by('id')
        submissions = task.submissions.select_related('submitted_by').order_by('-created_at')
        return success_response(data={
            'task': InventoryTaskSerializer(task).data,
            'items': InventoryTaskItemSerializer(items, many=True).data,
            'submissions': InventorySubmissionSerializer(submissions, many=True).data,
        })


class TaskClaimView(APIView):
    """领取任务视图（领取时锁定范围）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            task = InventoryTask.objects.select_related('batch').get(pk=pk)
        except InventoryTask.DoesNotExist:
            return error_response(message='盘点任务不存在', code=404)

        try:
            task, excluded_goods = services.claim_task(task, request.user)
        except IntegrityError:
            return error_response(message='范围内物资正被其他任务锁定，请同步范围后重试', code=409)

        logger.info(f"User {request.user.username} claimed task {task.task_no}")
        return success_response(data={
            'task': InventoryTaskSerializer(task).data,
            'excluded_goods': [_goods_brief(g) for g in excluded_goods],
        }, message='领取成功，范围已锁定')


class TaskInterruptView(APIView):
    """中断任务视图（释放锁，保留进度）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            task = InventoryTask.objects.get(pk=pk)
        except InventoryTask.DoesNotExist:
            return error_response(message='盘点任务不存在', code=404)

        services.interrupt_task(task, request.user)
        logger.info(f"User {request.user.username} interrupted task {task.task_no}")
        return success_response(data={
            'task': InventoryTaskSerializer(task).data,
        }, message='任务已中断，进度已保留')


class TaskReassignView(APIView):
    """重新分配任务视图"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_admin(request)
        if denied:
            return denied

        try:
            task = InventoryTask.objects.get(pk=pk)
        except InventoryTask.DoesNotExist:
            return error_response(message='盘点任务不存在', code=404)

        serializer = ReassignSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        try:
            new_assignee = User.objects.get(pk=serializer.validated_data['user_id'], is_active=True)
        except User.DoesNotExist:
            return error_response(message='盘点员不存在或已停用')

        services.reassign_task(task, new_assignee)
        logger.info(
            f"User {request.user.username} reassigned task {task.task_no} "
            f"to {new_assignee.username}"
        )
        return success_response(data={
            'task': InventoryTaskSerializer(task).data,
        }, message='任务已重新分配')


class TaskSyncScopeView(APIView):
    """范围同步视图（范围变化时保留进度、不重复计数）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            task = InventoryTask.objects.get(pk=pk)
        except InventoryTask.DoesNotExist:
            return error_response(message='盘点任务不存在', code=404)

        if not (request.user.is_admin or task.assignee_id == request.user.id or task.status == 'pending'):
            return error_response(message='仅主管或当前盘点员可同步范围', code=403)

        try:
            added_items, removed_items = services.sync_task_scope(task)
        except IntegrityError:
            return error_response(message='新增物资正被其他任务锁定，请稍后重试', code=409)

        logger.info(
            f"User {request.user.username} synced scope of task {task.task_no}: "
            f"+{len(added_items)} -{len(removed_items)}"
        )
        return success_response(data={
            'added': InventoryTaskItemSerializer(added_items, many=True).data,
            'removed': InventoryTaskItemSerializer(removed_items, many=True).data,
        }, message=f'范围已同步：新增 {len(added_items)} 项，移出 {len(removed_items)} 项')


class TaskSubmitView(APIView):
    """分段提交视图（幂等，重复提交不重复计数）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            task = InventoryTask.objects.get(pk=pk)
        except InventoryTask.DoesNotExist:
            return error_response(message='盘点任务不存在', code=404)

        serializer = SubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        submission, duplicated = services.submit_segment(
            task, request.user, data['request_id'], data['items']
        )

        task.refresh_from_db()
        batch = task.batch
        batch.refresh_from_db()
        if not duplicated:
            logger.info(
                f"User {request.user.username} submitted segment {data['request_id']} "
                f"for task {task.task_no}"
            )
        return success_response(data={
            'submission_id': submission.id,
            'duplicate': duplicated,
            'results': submission.summary.get('results', []),
            'task_status': task.status,
            'batch_status': batch.status,
        }, message='重复提交，已返回原结果' if duplicated else '提交成功')


# ==================== 异常复核 ====================

class ReviewListView(APIView):
    """异常复核单列表视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        denied = _require_admin(request)
        if denied:
            return denied

        queryset = InventoryReview.objects.select_related(
            'item__goods', 'item__task__area', 'record', 'reviewer'
        ).order_by('-created_at')
        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)

        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        start = (page - 1) * page_size
        end = start + page_size

        total = queryset.count()
        reviews = queryset[start:end]
        serializer = InventoryReviewSerializer(reviews, many=True)

        return success_response(data={
            'list': serializer.data,
            'total': total,
            'page': page,
            'page_size': page_size
        })


class ReviewResolveView(APIView):
    """复核处理视图"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_admin(request)
        if denied:
            return denied

        try:
            review = InventoryReview.objects.select_related('item__task').get(pk=pk)
        except InventoryReview.DoesNotExist:
            return error_response(message='复核单不存在', code=404)

        serializer = ReviewResolveSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        services.resolve_review(
            review, request.user,
            action=data['action'],
            final_quantity=data.get('final_quantity'),
            note=data.get('note', ''),
        )

        logger.info(
            f"User {request.user.username} resolved review {review.id} "
            f"with action {data['action']}"
        )
        return success_response(data=InventoryReviewSerializer(review).data, message='复核完成')
