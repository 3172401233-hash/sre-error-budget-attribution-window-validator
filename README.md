# sre-error-budget-attribution-window-validator

验证错误预算归因、窗口边界和共享故障。

## 说明

本工具**只消费离线事件**（本地构造的请求/失败/窗口等数据），不访问任何线上系统、
网络或监控平台。用于离线验证：当多个服务共享的依赖发生故障时，错误预算是否被正确归因，
以及窗口边界与上游重试是否导致同一失败被重复计入。

## 输入

- `Request`：请求事件（request_id、service、timestamp、dependency）
- `Failure`：失败事件（failure_id、request_id、service、timestamp、dependency、root_cause_id、retry_of）
- `SLOWindow`：SLO 窗口（service、[start, end)、slo_target）
- `ServiceOwnership`：服务所有权（service、owner）
- `SharedRootCause`：共享根因（root_cause_id、dependency、services）

## 归因规则

1. **重试去重**：沿 `retry_of` 链解析到根失败，同一根失败只计一次（重试放大折叠）；
   链断裂或成环的失败进入无法判定列表。
2. **窗口归属**：窗口为左闭右开 `[start, end)`；失败按根失败时间戳落入唯一窗口，
   跨窗口的重试不会在下一个窗口重复计数。
3. **请求缺失**：失败引用的请求不存在，或请求服务与失败服务不一致时，记入无法判定部分。
4. **共享依赖**：同一 `root_cause_id` 的跨服务失败分组，每个受影响服务各自消耗一次预算，
   多出的计数记为共享重复。

## 输出（`AttributionReport`）

- `results`：各服务各窗口的唯一失败数、预算允许/消耗/剩余、是否耗尽、归属 owner
- `duplicate_count` / `retry_duplicates` / `shared_duplicates`：重复计数明细
- `undetermined`：无法判定的失败及原因
- `shared_root_causes`：共享根因分组明细

## 运行测试

```bash
python -m unittest
```

兼容 Python 3.8.2+，仅使用标准库。
