"""
盘点管理URL配置
"""
from django.urls import path
from .views import (
    AreaListView, AreaAllView, AreaDetailView,
    BatchListView, BatchDetailView,
    TaskListView, TaskDetailView, TaskClaimView, TaskInterruptView,
    TaskReassignView, TaskSyncScopeView, TaskSubmitView,
    ReviewListView, ReviewResolveView,
)

urlpatterns = [
    # 保管区管理
    path('inventory/areas/', AreaListView.as_view(), name='inventory-area-list'),
    path('inventory/areas/all/', AreaAllView.as_view(), name='inventory-area-all'),
    path('inventory/areas/<int:pk>/', AreaDetailView.as_view(), name='inventory-area-detail'),

    # 盘点批次
    path('inventory/batches/', BatchListView.as_view(), name='inventory-batch-list'),
    path('inventory/batches/<int:pk>/', BatchDetailView.as_view(), name='inventory-batch-detail'),

    # 盘点任务
    path('inventory/tasks/', TaskListView.as_view(), name='inventory-task-list'),
    path('inventory/tasks/<int:pk>/', TaskDetailView.as_view(), name='inventory-task-detail'),
    path('inventory/tasks/<int:pk>/claim/', TaskClaimView.as_view(), name='inventory-task-claim'),
    path('inventory/tasks/<int:pk>/interrupt/', TaskInterruptView.as_view(), name='inventory-task-interrupt'),
    path('inventory/tasks/<int:pk>/reassign/', TaskReassignView.as_view(), name='inventory-task-reassign'),
    path('inventory/tasks/<int:pk>/sync-scope/', TaskSyncScopeView.as_view(), name='inventory-task-sync-scope'),
    path('inventory/tasks/<int:pk>/submit/', TaskSubmitView.as_view(), name='inventory-task-submit'),

    # 异常复核
    path('inventory/reviews/', ReviewListView.as_view(), name='inventory-review-list'),
    path('inventory/reviews/<int:pk>/resolve/', ReviewResolveView.as_view(), name='inventory-review-resolve'),
]
