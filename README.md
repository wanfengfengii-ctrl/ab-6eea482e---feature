# Timeline Projection Service

录音棚乐谱提示点（cue）投影服务：把以「四分音符刻度（tick）」表示的提示点，
按速度自动化曲线精确投影到纳秒时间轴与零基采样帧，保证灯光、字幕与录音不发生
跨段累计舍入漂移。

- 纯 Python 3.11 标准库实现，运行时零第三方依赖。
- 全部累计时长使用 `fractions.Fraction` 精确计算，**只在最后一步**对纳秒时刻和
  采样帧各自按「四舍六入、五取偶」（round-half-to-even）取整一次。
- 同刻度提示点必然得到相同投影；响应严格保持提示点的原始顺序。

## 接口

`POST /api/timelines/project`

```json
{
  "ticks_per_quarter": 480,
  "sample_rate": 48000,
  "tempo_points": [
    {"tick": 0,    "microseconds_per_quarter": 500000, "mode": "constant"},
    {"tick": 1920, "microseconds_per_quarter": 300000, "mode": "linear"},
    {"tick": 3840, "microseconds_per_quarter": 200000, "mode": "constant"}
  ],
  "cues": [
    {"id": "light-01",    "tick": 0},
    {"id": "subtitle-07", "tick": 2400}
  ]
}
```

字段约束：

| 字段 | 约束 |
| --- | --- |
| `ticks_per_quarter` | 正整数 |
| `sample_rate` | 正整数（Hz） |
| `tempo_points` | 1–500 个；`tick` 必须从 0 开始且严格递增；`microseconds_per_quarter` 为正整数（微秒/四分音符）；`mode` 为 `constant` 或 `linear` |
| `cues` | 1–2000 个；`id` 为非空字符串或整数且全请求唯一；`tick` 为非负整数 |

语义：

- `constant`：该点之后区间速度恒定。
- `linear`：该点到下一点之间，微秒/四分音符随 tick 线性变化（区间时长按
  梯形面积 `(v_a + v_b) / 2 × Δtick / tpq` 精确积分）。
- 最后一个速度点之后永远保持其速度，因此提示点无上界。

成功响应（200），保持 `cues` 原顺序：

```json
{
  "ticks_per_quarter": 480,
  "cues": [
    {"id": "light-01",    "tick": 0,    "time_nanoseconds": 0,         "sample_frame": 0},
    {"id": "subtitle-07", "tick": 2400, "time_nanoseconds": 2362500000, "sample_frame": 113400}
  ]
}
```

- `time_nanoseconds = round_half_even(累计微秒 × 1000)`
- `sample_frame = round_half_even(累计微秒 × sample_rate / 1_000_000)`（零基帧）

健康检查：`GET /health` → `200 {"status":"ok"}`（同时提供 `/healthz`、`/ready`）。

## 回放计划（playback_plan，可选）

排练时可能跳播（D.S.、反复）或重复某段乐谱。请求可带可选字段 `playback_plan`；
**省略时请求、响应、错误顺序与数值与以前完全一致**。给出后，提示点不再按绝对
乐谱时刻投影，而是沿实际播放路径投影到一条**始终连续、不倒退**的时钟，响应以
`occurrences`（而非 `cues`）返回：

```json
{
  "ticks_per_quarter": 480,
  "sample_rate": 48000,
  "tempo_points": [
    {"tick": 0, "microseconds_per_quarter": 500000, "mode": "constant"}
  ],
  "cues": [
    {"id": "a", "tick": 0},
    {"id": "b", "tick": 480}
  ],
  "playback_plan": [
    {"start_tick": 0,   "end_tick": 480, "repeats": 2},
    {"start_tick": 480, "end_tick": 960, "repeats": 1}
  ]
}
```

```json
{
  "ticks_per_quarter": 480,
  "occurrences": [
    {"id": "a", "tick": 0,   "plan_entry": 0, "repeat": 0, "time_nanoseconds": 0,          "sample_frame": 0},
    {"id": "a", "tick": 0,   "plan_entry": 0, "repeat": 1, "time_nanoseconds": 500000000,  "sample_frame": 24000},
    {"id": "b", "tick": 480, "plan_entry": 1, "repeat": 0, "time_nanoseconds": 1000000000, "sample_frame": 48000}
  ]
}
```

计划约束：

| 字段 | 约束 |
| --- | --- |
| `playback_plan` | 1–64 个条目；条目按请求顺序播放，允许重排、重叠或完全重复 |
| `start_tick` / `end_tick` | 非负整数，`end_tick` 严格大于 `start_tick`；区间左闭右开 `[start_tick, end_tick)` |
| `repeats` | 1–16 的整数 |
| 展开上限 | 展开后的提示出现次数（各条目 `repeats` × 落在区间内的 cue 数之和）不得超过 5000 |

语义：

- 排序为「条目顺序 → 零基轮次（repeat）→ 提示在原请求中的顺序」。落在区间
  `[start_tick, end_tick)` 内的提示才出现；恰在 `end_tick` 的提示属于下一段。
- 每个区间时长与片内偏移沿用既有速度曲线（含 linear 渐变），全部用 `Fraction`
  精确计算。**未舍入的累计时钟会跨无提示片段、回跳和重复继续传递**；纳秒与帧
  只在最终输出时各自按半偶规则取整一次，因此输出时钟严格单调不减。
- 合法的回跳（如先播 `[480,960)` 再播 `[0,480)`）只是在输出时钟后再接一段
  该区间的时长，不会令时钟倒退。
- `playback_plan` 启用时响应不含 `cues`；省略时响应不含 `occurrences`。

## 错误处理

任何字段级或结构级错误都使**整个请求失败（HTTP 400），响应绝不夹带部分投影
结果**。错误码为稳定字符串，并通过 `path` 给出对应位置（JSON Pointer 风格）：

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "request validation failed; no projections were produced",
    "errors": [
      {"code": "TEMPO_GAP",          "path": "/tempo_points/0/tick",                           "message": "..."},
      {"code": "DUPLICATE_TEMPO_TICK","path": "/tempo_points/2/tick",                          "message": "..."},
      {"code": "TEMPO_NOT_ORDERED",  "path": "/tempo_points/3/tick",                           "message": "..."},
      {"code": "NON_POSITIVE_TEMPO", "path": "/tempo_points/1/microseconds_per_quarter",       "message": "..."},
      {"code": "INVALID_MODE",       "path": "/tempo_points/1/mode",                           "message": "..."},
      {"code": "CUE_OUT_OF_RANGE",   "path": "/cues/4/tick",                                   "message": "..."},
      {"code": "DUPLICATE_CUE_ID",   "path": "/cues/9/id",                                     "message": "... /cues/2 ..."}
    ]
  }
}
```

其他错误码：`MISSING_FIELD`、`INVALID_TYPE`（JSON 布尔值不接受为整数）、
`INVALID_COUNT`、`UNKNOWN_FIELD`，以及传输层的 `MALFORMED_JSON`、
`EMPTY_BODY`、`BODY_TOO_LARGE`、`NOT_FOUND`。回放计划另有
`PLAN_TICK_OUT_OF_RANGE`（start/end 为负，指针指向对应字段）、
`INVALID_INTERVAL`（`end_tick <= start_tick`，指针指向 `end_tick`）、
`REPEATS_OUT_OF_RANGE`（repeats 超出 1–16）和 `PLAN_TOO_LARGE`
（展开超过 5000 个出现，指针指向 `/playback_plan`）。同一请求产生的错误集合与
顺序是确定性的，可用于稳定比对。计划字段非法、区间无效或展开超限时整个请求
失败，且不返回任何 `occurrences`。

## 本地运行（无需 Docker）

```bash
python3 -m app.server            # 默认 0.0.0.0:8080，可用 PORT / HOST 覆盖
python3 -m unittest discover -s tests -t .
```

## Docker 与一键校验

```bash
# 宿主机端口可配置（默认 8080）
HOST_PORT=9090 docker compose up -d app

# 一次性 verify 服务：等待 app 健康后，依次执行
# 构建（字节码编译）、全部单元测试、恒定段/渐变段/错误请求，以及回放计划
# （重复、回跳、计划非法）的新旧 API 冒烟，
# 随后自行退出，退出码即结果（0 成功）。
docker compose up --abort-on-container-exit --exit-code-from verify verify
```

Compose 服务：

- `app`：长驻 API 服务，带容器级 HEALTHCHECK，发布 `${HOST_PORT:-8080}:8080`。
- `verify`：依赖 `app` 的 `service_healthy` 条件，`restart: "no"`，结束即退出。
