# 请求诊断与错误归因能力（本地排查用）

在不改变原有成功接口语义的前提下，为服务补上一条贯穿
**接口调用 → 同步/异步处理 → 后台任务 → 异常分支** 的诊断链路：

- 每个请求都有稳定的 `request_id`（可由上游 `X-Request-ID` 透传），
  关键业务标识 `business_key` 可被后续环节与后台任务拿到；
- 基于 `contextvars` 传递，**并发请求互不串号**；任务取消/超时都有
  有界收尾，不会留下悬挂任务；
- 各类异常被**归一化为稳定、可区分的错误类别**，普通响应和日志都不
  泄露堆栈、下游地址、凭据等敏感信息；
- 诊断数据查询有**总开关 + 令牌权限**，并支持**采样与容量上限**；
  诊断写入失败或高并发都不会拖慢、改变主请求的业务结果。

## 目录结构

```
app/
  config.py            # 开关 / 采样率 / 容量 / 管理令牌（支持环境变量）
  context.py           # contextvars 上下文：request_id / business_key / sampled
  errors.py            # 错误类别、领域异常、归一化与脱敏错误体
  diagnostics.py       # 诊断记录、确定性采样、容量受限存储
  background.py        # 后台任务：继承上下文、异常归因、超时/取消收尾
  middleware.py        # ASGI 中间件：整条链路的串联点
  security.py          # 诊断查询令牌提取与常量时间比较
  logging_setup.py     # 带 req/biz/sampled/cat 字段的日志格式
  deps.py              # 路由依赖（每请求独立的 BackgroundJobs）
  routes_demo.py       # 演示业务接口（成功/各类失败/后台任务/并发自检）
  routes_diagnostics.py# 诊断数据查询接口（开关 + 权限）
  main.py              # 应用组装、异常处理器
tests/                 # 并发隔离 / 异常链路 / 权限 / 采样边界 / 取消 / 韧性
```

## 快速开始

```bash
uv sync --group dev          # 安装运行与测试依赖
uv run python main.py        # 启动服务（默认 127.0.0.1，端口随机）
uv run pytest                # 本地一键跑全部测试（实时打印诊断日志）
```

## 如何开启与配置

### 代码方式（适合本地调试/测试）

```python
from app.config import configure
from app.diagnostics import configure_store

cfg = configure(
    enabled=True,          # 总开关；False 时诊断接口返回 404 且不写记录
    sample_rate=1.0,       # 采样率 0.0~1.0，对同一 request_id 结果确定
    capacity=1000,         # 内存中最多保留的诊断记录数（环形淘汰最旧）
    admin_token="local-admin-token",  # 查询诊断数据所需令牌
)
configure_store(cfg)       # 让新配置立即生效（会清空旧记录）
```

### 环境变量方式

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DIAGNOSTICS_ENABLED` | `true` | 总开关（`1/true/yes/on` 为真） |
| `DIAGNOSTICS_SAMPLE_RATE` | `1.0` | 采样率，闭区间 `[0,1]`，越界启动即报错 |
| `DIAGNOSTICS_CAPACITY` | `1000` | 诊断记录容量上限（正整数） |
| `DIAGNOSTICS_ADMIN_TOKEN` | `local-admin-token` | 查询令牌；**置空则拒绝一切查询** |

> 采样对请求是确定性的：以 `request_id` 做稳定哈希分桶，因此
> `sample_rate=0` 一条不录、`=1` 全量录入，同一请求重放结果不变；
> 调参时可以观察到 0.5 附近约一半请求被采集。

## 错误如何分类

`app/errors.py` 把异常归一化为以下稳定类别（枚举值即对外契约）：

| category | HTTP 状态码 | 触发来源（示例） |
| --- | --- | --- |
| `validation` | 422 | Pydantic 校验失败、`ValidationFailure`、4xx 参数类错误 |
| `not_found` | 404 | `NotFoundFailure`、`HTTPException(404)` |
| `unauthorized` | 401/403 | 诊断接口缺令牌/令牌错误 |
| `dependency` | 502/503 | `DependencyFailure`、下游 5xx |
| `timeout` | 504 | `TimeoutFailure`、内置 `TimeoutError`/`asyncio.TimeoutError` |
| `cancelled` | 499 | 客户端断开、请求被取消（`CancelledError`） |
| `internal` | 500 | 一切未归类异常（兜底，绝不把原始信息外抛） |

错误响应统一为脱敏结构，成功响应保持原样：

```jsonc
// GET /            -> 200，字段与状态语义与原骨架完全一致
{ "Hello": "World" }

// GET /fail/dependency -> 502
{
  "error": {
    "category": "dependency",
    "message": "下游依赖服务异常",
    "request_id": "req_xxxxxxxx"
  }
}
```

每个响应（含成功）都会带 `X-Request-ID` 响应头；校验错误额外返回
不带输入回显的字段定位：

```json
{ "error": { "category": "validation", "...": "...",
             "fields": [ {"loc": ["body", "message"], "type": "string_too_short"} ] } }
```

**敏感信息处理**：对外错误体只含固定中文提示；诊断记录里只保存异常
类型名（如 `DependencyFailure`），不保存异常原文——下游地址、账号、
密钥不会出现在普通响应、诊断接口返回或日志中。

## 权限如何判定

- 诊断接口前缀 `/diagnostics`，支持两种令牌携带方式：
  - `Authorization: Bearer <token>`
  - `X-Diagnostics-Token: <token>`
- 使用 `hmac.compare_digest` 做**常量时间比较**，避免时序侧信道；
- 服务端令牌为空、调用方未带令牌或令牌错误，一律拒绝；
- 诊断总开关关闭时，即使带了正确令牌也返回 **404**，不暴露接口存在；
- 查询结果受 `capacity` 与 `limit`（1~200）双重限制。

```bash
TOKEN="local-admin-token"
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:<port>/diagnostics/records?limit=20"
curl -s -H "X-Diagnostics-Token: $TOKEN" "http://127.0.0.1:<port>/diagnostics/records/req_xxx"
```

记录字段：请求/业务标识、路径方法、状态码、错误类别、耗时、是否采样、
是否取消/超时、后台任务总数/失败数及每个后台任务的归因结果。

## 后台任务与取消语义

- 路由通过依赖注入拿到 `BackgroundJobs`，`jobs.schedule(fn, ...)`
  支持同步函数（线程池执行）与协程；任务在**登记时复制当前请求的
  上下文快照**，因此路由从请求体解析出的 `business_key` 也能继承；
- 每个任务有独立超时（默认 5s，可传 `timeout=`），单个任务超时只记
  `timeout` 失败，不影响其它任务与业务响应；
- 请求被取消时，中间件调用有界收尾（默认最多再等 2s）取消所有未完成
  任务，记录 `cancelled=true` 后按 ASGI 语义重新抛出取消。

## 本地验证步骤

```bash
# 1) 跑测试（会实时打印 [req=... cat=... sampled=...] 日志）
uv run pytest

# 2) 启动服务
uv run python main.py    # 记下输出端口，或自行改为固定端口

# 3) 正常成功路径（响应头里有 X-Request-ID）
curl -i http://127.0.0.1:<port>/
curl -s http://127.0.0.1:<port>/items/5

# 4) 各类错误归一化（注意 category 与状态码互不混淆）
curl -s http://127.0.0.1:<port>/items/-3          # validation / 422
curl -s http://127.0.0.1:<port>/fail/dependency   # dependency / 502
curl -s http://127.0.0.1:<port>/fail/timeout      # timeout    / 504
curl -s http://127.0.0.1:<port>/fail/internal     # internal   / 500

# 5) 触发后台任务并查它的诊断记录
curl -s -X POST http://127.0.0.1:<port>/background \
  -H 'Content-Type: application/json' \
  -d '{"message":"go","business_key":"order-7"}'
curl -s -H "Authorization: Bearer local-admin-token" \
  "http://127.0.0.1:<port>/diagnostics/records?limit=5" | python -m json.tool

# 6) 无令牌 / 错误令牌 -> 403
curl -i http://127.0.0.1:<port>/diagnostics/records
curl -i -H "X-Diagnostics-Token: wrong" http://127.0.0.1:<port>/diagnostics/records

# 7) 关闭诊断（接口 404，且不再写记录）
DIAGNOSTICS_ENABLED=false DIAGNOSTICS_ADMIN_TOKEN=local-admin-token \
  uv run python main.py
```

## 测试覆盖

- `tests/test_isolation.py`：80 路并发请求的标识/业务标识不串号、
  上游请求 ID 合法性校验、记录与请求一一对应；
- `tests/test_error_chain.py`：各类错误的类别与状态码、成功响应兼容、
  响应与诊断记录均不含敏感信息；
- `tests/test_permission.py`：无令牌/错令牌拒绝、两种令牌头、开关
  关闭 404 且不写记录、空令牌拒绝一切；
- `tests/test_sampling.py`：采样率 0/1 边界、0.5 的近似比例与确定性、
  容量环形淘汰、未采样不影响业务；
- `tests/test_background.py`：后台上下文继承、失败归因、超时与取消
  收尾、后台失败不改变业务响应；
- `tests/test_cancellation.py`：ASGI 层客户端断开的取消收尾与超时
  归一化；
- `tests/test_resilience.py`：诊断存储写入故障时主请求结果不变、
  200 并发下结果正确。

## 设计取舍备忘

- 诊断存储为**进程内内存结构**，面向本地/单实例排查；多实例部署时
  应在 `DiagnosticStore` 接口后接集中式存储（写入仍需异步、有界）。
- 诊断写入在请求收尾阶段进行且全程 try/except 兜底，路径极短
  （锁内只做 deque/dict 操作），避免成为主请求的性能与正确性负担。
