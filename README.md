# 科学实验溯源工作台（Experiment Provenance Workbench）

CQRS + Event Sourcing 全栈示例：命令追加 `event_store`，查询走投影表；Vue 前端查看 Run、事件时间线与血缘。

## How to Run

```bash
cd projects/03-experiment-provenance
docker compose up --build
```

> 镜像默认走 `docker.m.daocloud.io`（便于国内拉取）；前端 npm 使用 `npmmirror`。若你可直连 Docker Hub，可将 Dockerfile / compose 中的镜像前缀改回官方名。

首次启动会：

1. 拉起 PostgreSQL
2. 启动 FastAPI 后端并建表
3. `seed` 写入 2 条已完成 Run + 1 条进行中 Run
4. 构建并启动前端（nginx）

停止：

```bash
docker compose down
```

本地后端测试（可选，需 Python 3.11+）：

```bash
cd backend
pip install -r requirements.txt
pytest -q
```

## Services / 端口

| 服务 | 地址 |
|------|------|
| Frontend | http://localhost:3173 |
| Backend API | http://localhost:8173 |
| PostgreSQL | localhost:54373 |

容器内：

- `db`：Postgres `provenance/provenance`，库名 `provenance`
- `backend`：Uvicorn `:8000`
- `seed`：一次性灌数后退出
- `frontend`：nginx `:80`，`/api` 反代到 backend

## 账号

| 用户名 | 密码 | 角色 |
|--------|------|------|
| researcher | lab123456 | 可发命令（Start/Metric/Artifact/Complete/Abort） |
| auditor | audit123456 | 只读事件与投影 |

## Verification

1. 打开 http://localhost:3173 ，使用 `researcher` / `lab123456` 登录
2. 在 Run 列表看到 seed 数据（含进行中与已完成）
3. 点击「新建 Run」，填写 project/name、dataset sha、code commit，启动
4. 在详情页记录指标、挂载产物，再 Complete（或 Abort）
5. **重复指标名校验**：对同一指标名（如 `loss`）再记录一次 → 页面立即红色告警并禁用提交；绕过前端直接调 API 也会返回 **409**，事件时间线不新增事件，指标投影仍只保留一条原值（策略见下）
6. 打开「事件时间线」确认 version 递增的原始事件（被拒绝的重复记录不会出现）
7. 打开「血缘」确认 code_commit、dataset 指纹、artifacts、metrics
8. 健康检查：`GET http://localhost:8173/api/health`
9. 用 `auditor` 登录：可看列表/事件/血缘，命令按钮不可用；直接调 metrics/complete 等命令接口返回 **403**

终态或 `expected_version` 不匹配时，API 返回 **409**。

## 重复指标名策略

同一条 Run 内，**指标名唯一**。策略在后端写死（`app/cqrs.py` 的 `METRIC_NAME_POLICY`）：

- 当前策略：`reject` —— 已存在同名指标时，**拒绝第二次记录**。
  - 不追加任何事件（被拒绝的命令不进入 `event_store`，事件仍完整可追溯、version 不跳号）。
  - 返回 **409**，说明文字明确指出冲突指标名与策略，前端在提交前与提交后均给出红色拒绝提示。
  - 查询侧投影、血缘中每个指标名恰好一条，与策略一致。
- 代码保留 `overwrite`（新值覆盖）的语义位，但当前不启用；如需切换为覆盖策略，改常量并相应调整投影归并逻辑即可。

## 权限

- `researcher`：可发全部命令（Start/Metric/Artifact/Complete/Abort）。
- `auditor`：**只读**。可查看 Run、事件时间线、血缘；所有写命令接口由 `require_researcher` 拦截并返回 **403**，无法记录指标或改变 Run。

## 架构要点

- **命令**：`StartRun` / `RecordMetric` / `AttachArtifact` / `CompleteRun` / `AbortRun`
- **事件**：`RunStarted` / `MetricRecorded` / `ArtifactAttached` / `RunCompleted` / `RunAborted`
- **event_store**：`(aggregate_id, version)` 唯一；冲突 → 409
- **run_projections**：查询侧投影（状态、指标、产物等）
