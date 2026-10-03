"""
盘点管理URL配置
"""
from django.urls import path
from .views import (
    BatchListCreateView, BatchDetailView, BatchRefreshScopeView, BatchConcludeView,
    TaskListView, TaskDetailView, TaskClaimView, TaskReleaseView,
    TaskReassignView, TaskSubmitView, TaskCompleteView,
    ReviewListView, ReviewResolveView,
)

urlpatterns = [
    # 盘点批次（主管）
    path('stocktake/batches/', BatchListCreateView.as_view(), name='stocktake-batch-list'),
    path('stocktake/batches/<int:pk>/', BatchDetailView.as_view(), name='stocktake-batch-detail'),
    path('stocktake/batches/<int:pk>/refresh-scope/', BatchRefreshScopeView.as_view(),
         name='stocktake-batch-refresh-scope'),
    path('stocktake/batches/<int:pk>/conclude/', BatchConcludeView.as_view(),
         name='stocktake-batch-conclude'),

    # 盘点任务（盘点员）
    path('stocktake/tasks/', TaskListView.as_view(), name='stocktake-task-list'),
    path('stocktake/tasks/<int:pk>/', TaskDetailView.as_view(), name='stocktake-task-detail'),
    path('stocktake/tasks/<int:pk>/claim/', TaskClaimView.as_view(), name='stocktake-task-claim'),
    path('stocktake/tasks/<int:pk>/release/', TaskReleaseView.as_view(), name='stocktake-task-release'),
    path('stocktake/tasks/<int:pk>/reassign/', TaskReassignView.as_view(), name='stocktake-task-reassign'),
    path('stocktake/tasks/<int:pk>/submit/', TaskSubmitView.as_view(), name='stocktake-task-submit'),
    path('stocktake/tasks/<int:pk>/complete/', TaskCompleteView.as_view(), name='stocktake-task-complete'),

    # 异常复核（主管）
    path('stocktake/reviews/', ReviewListView.as_view(), name='stocktake-review-list'),
    path('stocktake/reviews/<int:pk>/resolve/', ReviewResolveView.as_view(), name='stocktake-review-resolve'),
]
