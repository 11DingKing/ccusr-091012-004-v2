# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 盘点管理

主管按区域、风险和截止日期生成盘点批次（`POST /api/inventory/batches/`），系统按保管区拆分任务并快照范围，已被其他批次锁定的物资自动跳过、无区物资显式提示，避免重复锁定与区域遗漏。盘点员领取任务时锁定范围（`POST /api/inventory/tasks/{id}/claim/`），分段提交盘点结果（`POST /api/inventory/tasks/{id}/submit/`，以 `request_id` 幂等，重复提交不重复计数）；账实不符的明细自动进入复核（`POST /api/inventory/reviews/{id}/resolve/` 确认差异或退回重盘）。全部任务完成后批次自动形成结论。任务中断（`interrupt/`）释放锁并保留进度，重新分配（`reassign/`）与范围同步（`sync-scope/`）不影响已盘进度；批次详情（`GET /api/inventory/batches/{id}/`）的 `unfinished` 字段列出尚未完成的具体区域与物资。

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
