# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 盘点批次模块（apps.inventory）

解决多保管区各自盘点导致的重复锁定与区域遗漏问题：

- 主管按**区域、风险等级、截止日期**生成盘点批次，系统按区域自动拆分为可执行任务（`POST /api/stocktake/batches/`）。
- 盘点员**领取任务即锁定物资范围**：`iv_task_goods` 上对 `is_locked=True` 的物资建有部分唯一索引，跨批次重叠任务无法重复锁定同一物资（领取冲突返回 409）。
- 盘点员可**分段提交**（`POST /api/stocktake/tasks/<id>/submit/`），同一物资重复提交按更新处理、不重复计数；异常项自动进入复核队列。
- **任务中断**（release）释放锁并退回任务池、已盘进度保留；主管可**重新分配**（reassign），锁与进度不变。
- **范围变化**通过 `POST /api/stocktake/batches/<id>/refresh-scope/` 生成增补任务，历史任务与进度不动。
- 所有任务完成且异常复核清零后，主管才能**形成批次结论**（conclude）。
- 批次详情（`GET /api/stocktake/batches/<id>/`）返回 `pending_scopes`（按区域列出未盘物资、责任任务与盘点员）和 `scope_diff`（在册但未纳入批次的物资），直接指出遗漏范围。

风险规则：数量 ≤ 预警阈值为高风险；无存放位置为中风险；其余为低风险。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
