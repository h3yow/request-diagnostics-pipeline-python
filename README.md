# 请求诊断与错误归因（Request Diagnostics Pipeline）

为 FastAPI 服务增加一套贯穿 **同步接口 / 异步处理 / 后台任务 / 异常分支** 的请求诊断与错误归因能力，用于本地排查问题。设计目标：

- 同一条请求的 `request_id` / `business_id` 在整条调用链（含后台任务、异常分支）一致，并发请求互不串号；
- 各种异常归一化为**稳定、可区分**的错误类别；
- 诊断数据的查询有明确开关与权限控制，支持采样与容量上限；
- 诊断链路任何故障都**不会拖慢或改变**业务结果；
- 敏感信息不出现在普通响应和日志里。

## 1. 开启与配置

全部通过环境变量配置（前缀 `DIAG_`），默认**关闭**，关闭时中间件对业务完全透明（不采样、不落库、查询路由不注册）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DIAG_ENABLED` | `0` | `1/true/yes/on` 开启诊断能力（含查询路由） |
| `DIAG_SAMPLE_RATE` | `1.0` | 采样率，`[0.0, 1.0]`，越界自动收敛 |
| `DIAG_CAPACITY` | `1000` | 内存中最多保留的诊断记录条数，超出淘汰最旧 |
| `DIAG_QUERY_TOKEN` | 空 | 查询诊断数据所需的令牌；**为空时一律拒绝（fail-closed）** |

启动：

```bash
DIAG_ENABLED=1 \
DIAG_SAMPLE_RATE=0.5 \
DIAG_CAPACITY=2000 \
DIAG_QUERY_TOKEN=local-token \
uv run python main.py
# 或：uv run uvicorn main:app --reload
```

诊断记录只保存在**单进程内存**里（有界 `deque`），定位本地问题用；不落盘、不外发。

## 2. 请求标识与链路串联

- 入站可带 `X-Request-Id`（受限字符集 `[A-Za-z0-9._-]{1,128}`，非法则自动生成 hex id），`X-Business-Id` 或 `?business_id=` 传业务标识（会剔除 `\r\n\t`，防止日志注入）。
- 所有响应都带 `X-Request-Id` 响应头；错误响应体内也回带 `request_id`，便于把客户端报错与服务端记录对上。
- 上下文用 `contextvars` 承载，asyncio 每个任务持有独立副本：
  - 并发请求天然隔离，测试覆盖 100/200 并发；
  - 后台任务通过 `diagnostics.spawn_background()` 调度，在创建时**复制**当前请求的 id / 业务 id / 采样决策，请求结束后任务仍持有自己的身份，且不影响兄弟任务。
- 请求或后台任务被取消、超时、抛异常时都会正确收尾：分类 → 结构化日志 →（采样命中时）落库；`CancelledError` 在请求路径上继续向上抛以保持 ASGI 服务器语义。

代码里取上下文：

```python
from diagnostics.context import require_context
ctx = require_context()
with ctx.span("upstream.call", kind="http"):
    ...
```

发起后台任务：

```python
from diagnostics.background import spawn_background
spawn_background(coro, name="report", store=store)
```

## 3. 错误如何分类

异常统一归一化到以下稳定类别（字符串值即对外契约，不会随内部实现变化）：

| 类别 `error.category` | HTTP | 典型来源 |
| --- | --- | --- |
| `validation_error` | 422 | `RequestValidationError`、`ValueError/KeyError/TypeError`、业务主动抛 `ValidationError` |
| `permission_denied` | 403 | 401/403、`PermissionDeniedError` |
| `not_found` | 404 | 404、`NotFoundError` |
| `timeout_error` | 504 | 内置 `TimeoutError` / `asyncio.TimeoutError` |
| `cancelled` | 499 | `asyncio.CancelledError`（客户端断开 / 任务取消） |
| `dependency_error` | 502 | `ConnectionError`/`OSError`、502/503 等下游错误、`DependencyError` |
| `internal_error` | 500 | 其他未归类异常 |

对外错误体结构稳定，且**只含通用文案与 request_id**；原始异常信息只留在服务端诊断记录内部、不返回客户端：

```json
{
  "error": {
    "category": "dependency_error",
    "message": "downstream dependency error",
    "request_id": "c8bd53feb7ee47e7bbf9818f0b4e5545"
  }
}
```

唯一允许返回的明细是参数校验字段（`details.validation`，仅含字段名/类型/短提示）。框架异常处理器转成的响应，中间件也会按状态码反推类别，保证归类不丢。

主动抛错：

```python
from diagnostics.errors import ValidationError, DependencyError, TimeoutError
raise ValidationError(details={"field": "item_id"})
```

## 4. 查询权限如何判定

- 诊断总开关关闭时，`/diagnostics/*` 路由根本不注册（返回 404，表面不存在）。
- 开启后，每个诊断查询都必须携带令牌：请求头 `X-Diagnostics-Token: <token>`，或 `Authorization: Bearer <token>`。
- 使用 `hmac.compare_digest` 恒定时间比较；服务端未配置令牌（空）时**拒绝一切访问**；拒绝原因不会回显令牌内容。
- 诊断查询自身不写入诊断记录，避免污染查询结果。

接口：

```bash
# 最近记录（limit 上限 200）
curl -s -H "X-Diagnostics-Token: local-token" \
  "http://127.0.0.1:8000/diagnostics/requests?limit=50"

# 单个请求的聚合视图（request 记录 + related 后台任务记录）
curl -s -H "X-Diagnostics-Token: local-token" \
  "http://127.0.0.1:8000/diagnostics/requests/<request_id>"
```

记录字段：`request_id / business_id / sampled / method / path / status_code /
duration_ms / error_category / error_message / spans / phase(request|background)
/ task`，同一 `request_id` 的后台记录出现在 `related` 中。

## 5. 采样与容量

- 采样决策 = `sha256("diag-sampler-v1:" + request_id)` 高 32 位映射到 `[0,1)`，
  当且仅当 `bucket < rate` 时记录：
  - `rate=0` 全不采、`rate=1` 全采；
  - **同一 request_id 决策永远一致**；提高采样率只会新增被采的 id，不会让原来采到的变成不采（单调）。
- 容量：有界 `deque(maxlen=capacity)`，满了淘汰最旧记录并维护计数；并发写有锁保护。
- 落库是 **best-effort**：存储异常被捕获并记 debug 日志，不抛出、不影响响应；
  采样未命中时不产生写库开销。日志同样 best-effort，任何日志异常都不影响请求。

## 6. 敏感信息保护

- `diagnostics.redact` 递归处理：命中敏感键名（`password/token/secret/api_key/
  authorization/cookie/...`，大小写与中划线/下划线不敏感）整体替换为
  `***REDACTED***`；文本中的 `Authorization: Bearer ...`、`token=...` 等模式掩码；
  未知对象不用 `repr()`（防止其 `__repr__` 泄密），只输出类型标记。
- 结构化 JSON 日志（logger 名 `diagnostics`）每行带 `request_id / business_id /
  sampled / error_category / event`，所有字段先脱敏；环境变量无法开启"打印敏感信息"。
- 单测日志在每次请求/后台任务收尾时打印 `request_id`、`error_category`、
  `sampled`，失败时可直接据此定位（`pytest -s` 可见）。

## 7. 本地验证步骤

```bash
# 安装依赖（已含 pytest）
uv sync

# 全量测试（约 2 秒，含真实 uvicorn 子进程的端到端用例）
uv run pytest

# 只看某类场景
uv run pytest tests/test_middleware.py    # 链路 / 并发隔离 / 取消 / 归一化
uv run pytest tests/test_sampling.py      # 采样边界
uv run pytest tests/test_integration.py   # 开关 / 权限 / 真实服务器端到端
```

手动体验（服务起在 8000）：

```bash
DIAG_ENABLED=1 DIAG_SAMPLE_RATE=1 DIAG_QUERY_TOKEN=local-token uv run python main.py

curl -s http://127.0.0.1:8000/                                   # 原接口，行为不变
curl -s http://127.0.0.1:8000/items/-5                           # 422 validation_error
curl -s http://127.0.0.1:8000/dependencies/fail                  # 502 dependency_error
curl -s http://127.0.0.1:8000/dependencies/timeout               # 504 timeout_error
curl -s -X POST http://127.0.0.1:8000/jobs -H 'content-type: application/json' \
  -d '{"mode":"fail"}'                                           # 后台任务失败收尾
curl -s http://127.0.0.1:8000/diagnostics/requests               # 403（无令牌）
curl -s -H "X-Diagnostics-Token: local-token" \
  http://127.0.0.1:8000/diagnostics/requests                     # 查看记录
```

## 8. 代码结构

```
diagnostics/
  context.py         # 请求上下文（ContextVar）、Span
  errors.py          # 错误类别、异常层级、分类/归一化、稳定响应体
  redaction.py       # 敏感信息脱敏
  settings.py        # 环境变量配置
  sampling.py        # 确定性采样
  storage.py         # 有界、best-effort 内存存储（同 id 多记录聚合）
  auth.py            # 查询令牌校验（恒定时间、fail-closed）
  logging_setup.py   # 结构化 JSON 日志
  background.py      # 上下文传播的后台任务（成功/异常/取消收尾）
  pipeline.py        # 组装脱敏记录 + 安全落库
  middleware.py      # 纯 ASGI 中间件：id/采样/计时/归一化/落库
  endpoints.py       # /diagnostics 查询路由 + 异常处理器
tests/               # 65 个用例：并发隔离、异常链路、权限拒绝、采样边界等
main.py              # 演示服务（保留原始 GET / 契约）
```
