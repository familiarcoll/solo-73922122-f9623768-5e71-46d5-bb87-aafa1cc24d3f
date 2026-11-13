# 医学影像切片分组服务（Slice Split Service）

把标注切片按**受试者防泄漏**地划分为 `train` / `validation` / `test` 三个集合。

- 同一受试者（subject）的全部切片样本只能进入**同一个集合**；
- 各病种（disease）在三个集合中的样本数**精确等于**请求配额；
- 可对指定受试者**冻结**归组（求解时强制进入指定集合）；
- 求解基于 OR-Tools CP-SAT，服务框架为 FastAPI + SQLite；
- 候选保存求解时刻的**完整输入快照**；求解后任何修改都会使旧候选失效，
  发布返回 `409`，即使把值改回去也无法恢复（版本号单调递增、永不回退）；
- **重新求解的候选替换是严格原子且不可复活的**：新求解受理后，旧候选在同一
  事务里被标记 `superseded`，旧 worker 线程迟到的成功/暂停/取消/异常收尾都带
  归属与状态保护，写入全部空转，旧候选持续 `superseded`、发布返回 `409`；
- **控制信号按任务隔离**：每个候选在 `solve_controls` 中有独立的一行，旧线程
  收尾只能清除自己的信号，不可能消费或清除新任务的暂停/恢复/取消信号；
- 求解在后台线程进行，CP-SAT 计算期间**不持有数据库写锁**，期间可继续
  导入/补样、修改病种、修改冻结；
- 两个 API 进程共享同一个 SQLite（WAL 模式），任务可跨进程认领、暂停、继续、取消；
- 发布版本不可变，查询与 CSV 下载都对应发布时的同一份完整快照；重启后保留。

## 目录结构

```
app/
  config.py      业务常量与数据库路径
  database.py    SQLite 持久化、版本号、事务
  schemas.py     Pydantic 模型与配额校验
  solver.py      OR-Tools CP-SAT 模型（支持暂停/继续/取消的分片求解）
  manager.py     后台求解管理、跨进程认领与控制信号
  main.py        FastAPI 全部 HTTP 接口
scripts/
  generate_fixture.py     生成虚构切片 CSV（无真实患者数据）
  demo.py                 单进程完整流程演示
  demo_dual_process.py    双进程重新求解/控制交错演示（可控制旧求解返回时机）
tests/                    pytest 端到端测试（含竞态回归与真实双进程集成）
data/                     虚构样例数据
Dockerfile
docker-compose.yml
```

## 快速开始（Docker Compose，推荐）

```bash
# 1. 生成虚构切片数据（也可直接使用仓库自带的 data/slices_seed.csv）
python scripts/generate_fixture.py

# 2. 构建并启动两个共享数据库的 API 进程
docker compose up --build -d

# 3. 打开交互式文档
#    http://localhost:8001/docs   （进程 1）
#    http://localhost:8002/docs   （进程 2）
```

两个容器挂载同一个命名卷 `slice-data`，数据库文件为 `/data/slices.db`；
演示闸门使用另一个共享卷 `slice-gates`（容器内 `/gates`）。

### 一键演示双进程重新求解竞态

```bash
docker compose up --build -d
docker compose run --rm demo
```

`demo` 服务运行 `scripts/demo_dual_process.py`，自动完成：进程1提交旧求解 A
（成功返回被闸门拦住）→ 进程2受理新求解 B、A 立即 `superseded` → 发布 A 返回
`409` → 跨进程暂停 B → 放行 A 的闸门验证迟到成功不复活、也不清掉 B 的暂停
信号 → 恢复并发布 B → 跨进程取消新任务 C。脚本只依赖标准库，也可在容器外
对已启动的两个端口运行：

```bash
SLICE_SOLVE_GATE_DIR=/path/to/gates \
  python scripts/demo_dual_process.py http://127.0.0.1:8001 http://127.0.0.1:8002
```

## 本地运行（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/generate_fixture.py

# 进程 1 / 进程 2，指向同一个数据库文件
SLICE_DB_PATH=./slices.db uvicorn app.main:app --port 8001
SLICE_DB_PATH=./slices.db uvicorn app.main:app --port 8002
```

本地复现双进程竞态演示时，给两个进程再加上同一个闸门目录：

```bash
mkdir -p /tmp/slice-gates
SLICE_DB_PATH=./slices.db SLICE_SOLVE_GATE_DIR=/tmp/slice-gates \
  uvicorn app.main:app --port 8001
SLICE_DB_PATH=./slices.db SLICE_SOLVE_GATE_DIR=/tmp/slice-gates \
  uvicorn app.main:app --port 8002
SLICE_SOLVE_GATE_DIR=/tmp/slice-gates \
  python scripts/demo_dual_process.py http://127.0.0.1:8001 http://127.0.0.1:8002
```

## 通过 /docs 完成完整流程

下面用 `curl` 给出等价步骤（在 Swagger UI 里点击对应接口即可）。

### 1. 导入样本（样本ID、受试者ID、病种）

CSV 表头固定为 `sample_id,subject_id,disease`：

```bash
curl -X POST "http://localhost:8001/api/samples/import-csv?mode=replace" \
  -F "file=@data/slices_seed.csv;type=text/csv"
```

也可用 JSON：`POST /api/samples/import?mode=replace|append`。
约束：最多 200 个样本、20 名受试者、4 种病种（超限返回 422）。

### 2. 冻结受试者归组（可选）

```bash
curl -X PUT http://localhost:8001/api/subjects/sub-003/freeze \
  -H 'Content-Type: application/json' -d '{"group":"test"}'
# 解冻：DELETE /api/subjects/{subject_id}/freeze
```

### 3. 提交求解（精确配额）

每个病种的 `train+validation+test` 必须**恰好等于该病种现有样本总数**：

```bash
curl -X POST http://localhost:8001/api/solve \
  -H 'Content-Type: application/json' -d '{
    "quota": {
      "glioma":     {"train": 7, "validation": 5, "test": 4},
      "meningioma": {"train": 8, "validation": 4, "test": 3},
      "pituitary":  {"train": 6, "validation": 4, "test": 2},
      "normal":     {"train": 9, "validation": 4, "test": 4}
    },
    "time_limit_seconds": 30,
    "min_hold_seconds": 0
  }'
```

返回候选任务 `id`，后台异步求解。轮询 `GET /api/solve/{id}` 查看状态：
`solving → solved | infeasible | paused | cancelled | failed`。

### 4. 可控暂停 / 继续 / 取消（跨进程）

`min_hold_seconds` 让求解即使已找到可行解也保持运行，便于演示暂停：

```bash
# 在任意一个进程上发信号都可以（信号写入共享数据库）
curl -X POST http://localhost:8002/api/solve/1/pause
curl -X POST http://localhost:8002/api/solve/1/resume
curl -X POST http://localhost:8002/api/solve/1/cancel
```

### 5. 发布

```bash
curl -X POST http://localhost:8001/api/solve/1/publish
```

- 候选已求解成功（`solved`）且输入版本等于当前版本 → 200，生成不可变发布版本；
- 求解后数据被修改过 → `409`（版本号不匹配，改回原值也不能恢复，需重新求解）；
- `infeasible` 候选无法发布，已发布结果保持不变。

### 6. 查询与 CSV 下载（同一不可变版本）

```bash
curl http://localhost:8001/api/published                      # 当前发布版本
curl http://localhost:8001/api/published?published_id=3      # 历史版本
curl http://localhost:8001/api/published/history             # 发布历史
curl -OJ http://localhost:8001/api/published/csv             # 下载当前版本 CSV
curl "http://localhost:8001/api/published/csv?published_id=3" # 历史版本 CSV
```

CSV 列：`sample_id,subject_id,disease,group,solution_id,input_version`，
数据全部取自发布时保存的快照，与当前库内数据无关。

## 关键设计说明

### 失效语义（为什么"改回去"也不能恢复）

`metadata.data_version` 是单调递增整数。任何有效修改（导入、补样、改病种、
改/清冻结、删除）都在同一事务里让它 +1。候选行记录求解时的版本号与完整输入快照；
发布时比对当前版本，不一致即 409。把值改回原值只会让版本号继续 +1，不会回退，
因此旧候选永久失效。

### 求解不持写锁

`POST /api/solve` 只在提交时做一次短暂的读事务（取快照 + 写任务行）。
随后 CP-SAT 在后台线程基于内存快照计算，不再访问数据库，仅每 0.25s 回写一次
心跳（独立短事务）。因此求解进行中，其它请求的写操作不会被阻塞。

### 跨进程

- SQLite 启用 `WAL + busy_timeout=30s`，两进程并发读写互不阻塞；
- 任务行用 `worker_id` 原子认领（`UPDATE ... WHERE worker_id IS NULL RETURNING`）；
- 暂停/继续/取消信号写入 `solve_controls` 表，worker 在每个求解分片轮询；
- 心跳超过 30s 的任务会被另一进程重新认领，单进程崩溃不会卡死任务。

### 候选替换为什么不可"复活"（重新求解竞态）

调整配额、重新划分训练集时会重新提交求解。修复前存在两个缺陷：
控制信号存在一个全局单行表里，任何任务收尾都会把它清空；worker 写终态时
只按任务 id 更新、没有归属检查。于是新候选创建后，旧线程迟到的成功会把
已 `superseded` 的旧候选改回 `solved`（复活、可被误发布），还会顺手清掉
新任务的暂停信号。修复后的保证：

1. **替换原子化**：`POST /api/solve` 在同一 `BEGIN IMMEDIATE` 事务里把全部旧
   候选置为 `superseded`、只删除这些旧任务自己的控制行，再插入新候选；
2. **写入带守卫**：worker 的每一次状态变更都走 `guarded_transition`，
   SQL 条件为 `id=? AND worker_id=? AND status IN (...)`。任务一旦
   `superseded` 或被别的进程接管，迟到的 `solved/paused/cancelled/failed`
   写入影响行数为 0，终态不可被覆盖；
3. **信号按任务隔离**：`solve_controls` 以 `solution_id` 为主键，每个任务只
   读/写/删自己的行，旧线程的任何收尾都碰不到新任务的控制信号；
4. **心跳同样带归属**：被取代或接管的任务不会再被旧 worker 刷新心跳。

因此：旧候选持续 `superseded`、对它的发布与 pause/resume/cancel 一律 `409`；
新候选可正常暂停、恢复、取消、发布；任意操作交错，失效旧候选都不会复活。
旧版单行控制表会在启动时自动迁移为按任务隔离的新表。

### 演示用"求解闸门"（控制旧求解的返回时机）

设置环境变量 `SLICE_SOLVE_GATE_DIR` 后，worker 在已经找到可行解、即将成功
返回之前，会阻塞等待该目录下出现 `release-<solution_id>` 文件；等待期间
仍刷新心跳并响应暂停/取消。不设置该变量时闸门完全关闭，行为与普通
CP-SAT 一致。Compose 的两个 API 都把 `/gates` 挂载为共享卷，因此可以从
外部精确控制任意任务（包括旧任务）何时成功返回，确定性复现交错时序。

### 无解

配额本身通过校验（总和相等）但与冻结/受试者整组约束矛盾时，CP-SAT 证明无解，
候选状态为 `infeasible`，不产生分配、不可发布，已发布版本完全不受影响。

## 运行测试

```bash
pip install -r requirements.txt
pytest -q
```

覆盖：上限校验、配额校验、精确约束、受试者整组、冻结、无解不影响已发布、
修改即失效（含改回原值）、暂停/继续/取消、求解中并发修改、CSV 一致性、
发布历史不可变、跨"重启"持久化（独立子进程），以及本修复的专项：

- `tests/test_supersede_race.py`：用可控制返回时机的桩求解器，确定性验证旧
  求解迟到的成功/暂停/取消/异常均不能复活 `superseded` 候选，新任务的暂停
  信号在交错操作下不被消费（5 个用例）；
- `tests/test_dual_process.py`：启动两个真实 uvicorn 进程共享一个 SQLite，
  经闸门控制旧求解返回时机，验证跨进程替换、`409`、跨进程暂停/恢复/取消/发布。
