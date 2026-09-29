# QwenPaw Agent API · SSE 事件谱（2026-09-29 实测录制）

> 来源：真实打 `POST http://127.0.0.1:8088/api/console/chat`（带 `X-Agent-Id: default`），
> 让它执行一次 `read_file` 工具调用，全程录下 SSE。
> 原始夹具：`tests/fixtures/sse-tool-flow.sse`（14,900 字节 / 57 个事件）
> 用途：**Step 2 引擎适配器的映射依据** + 单测夹具。

---

## 1. 事件总览（本次录制）

| 事件 `type` | 次数 | 含义 |
|---|---|---|
| `text` | 36 | 正文增量（**流式的主体**） |
| `data` | 9 | 结构化数据块（工具调用的参数/输出分片） |
| `message` | 4 | 一条完整消息（开始/结束快照） |
| `response` | 3 | 整个响应的开始/结束（**终态在这里**） |
| `plugin_call` | 2 | 工具调用（开始） |
| `plugin_call_output` | 2 | 工具执行结果 |
| `turn_usage` | 1 | 本轮 token 用量 + 上下文占用（**在最后**） |

每条 SSE 行的前缀固定是 `data: `，后跟一个 JSON 对象（**没有 `event:` 行**）。

---

## 2. 事件序列时序（实测顺序）

```
 0. response               ← 响应开始（status=created）
 1. response               ← status=in_progress
 2. message                ← 助手消息开始（content=[]）
 3-6. text ×4              ← 正文增量："I" / "'ll read that" / " file for" / " you.\n\n"
 7. text                   ← delta=false，整段快照（"I'll read that file for you."）
 8. message                ← 助手消息完成（status=completed，含完整 content）
 9. plugin_call            ← 工具调用开始（role=assistant，name=read_file）
10-15. data ×6             ← 工具参数分片（call_id / name / arguments）
16. plugin_call           ← 工具调用的完整快照
17. plugin_call_output    ← 工具执行结果（开始）
18-19. data ×2            ← 工具输出分片（call_id / name / output）
...
N. text ×N               ← 工具结果喂回模型后，继续输出正文
N+1. message              ← 完成
N+2. response             ← ★ 终态（status=completed，output 数组含全部消息）
N+3. turn_usage           ← ★ token 用量与上下文占用
```

**关键**：工具调用不是"单独一条流"，而是**穿插在同一个响应里** ——
文本 → 工具调用 → 工具结果 → 更多文本 → …… → 终态。

---

## 3. 关键事件的结构

### `text`（正文增量）★ 最常用

```json
{
  "type": "text", "delta": true, "index": 0, "status": null,
  "object": "content", "msg_id": "msg_...",
  "text": "I'll read that",
  "sequence_number": 4
}
```
- `delta: true` = 增量片段；`delta: false` = **整段快照**（去重用，别重复追加）
- 追加逻辑：只累积 `delta: true` 的 `text` 字段

### `plugin_call`（工具调用）

```json
{
  "id": "msg_...", "type": "plugin_call", "role": "assistant",
  "content": [{
    "type": "data", "delta": false, "index": null, "status": "completed",
    "object": "content", "msg_id": null,
    "data": {
      "call_id": "chatcmpl-tool-b207f3da7699c9fa",
      "name": "read_file",
      "arguments": "{\"file_path\": \"/tmp/probe-agent.txt\"}"
    }
  }],
  "status": "completed", "metadata": null, "name": "assistant",
  "object": "message", "sequence_number": 9
}
```
- **工具名**：`content[0].data.name`
- **参数**：`content[0].data.arguments`（**JSON 字符串**，要二次解析）
- **调用 id**：`content[0].data.call_id`（与 output 配对用）

### `plugin_call_output`（工具结果）

结构同上，`data` 里的键是 **`call_id` / `name` / `output`**（输出在 `output`，不是 `arguments`）。

### `data`（分片）

工具参数/输出会**先以 `data` 事件分片流式到达**（`call_id` / `name` / `arguments|output` 陆续出现），
最后再来一条完整的 `plugin_call` / `plugin_call_output` 快照。
→ **适配器只需认快照事件**（`plugin_call` / `plugin_call_output`），分片 `data` 可忽略（避免重复渲染）。

### `response`（终态）

```json
{
  "id": "response_...", "output": [ ...全部消息... ],
  "status": "completed",            // ← completed / failed / cancelled
  "created_at": "...", "completed_at": "...",
  "object": "response", "session_id": "probe-tool-flow",
  "sequence_number": 57,
  "usage": { "input_tokens": 13767, "output_tokens": 21 }
}
```
**这是结束标志**，也是拿最终全文最可靠的地方（`output` 里每条消息带完整 content）。

### `turn_usage`（用量，最后一条）

```json
{
  "type": "turn_usage", "session_id": "probe-tool-flow",
  "usage": {
    "provider_id": "opencode", "model_name": "deepseek-v4.1-flash",
    "prompt_tokens": 13767, "completion_tokens": 21, "total_tokens": 13788,
    "cache_read_tokens": 1024, "cache_observed": true, "cache_hit_rate": 7.43,
    "context_size": 1000000, "compact_threshold": 0.8
  },
  "context_usage": { "estimated_tokens": 27, "max_input_length": 1000000, "context_usage_ratio": 0.0027 }
}
```
**这是网关侧记 token / 算成本的直接来源**（比从 `response.usage` 拿更全：带 model_name 和缓存字段）。

---

## 4. 适配器映射表（Step 2 实现依据）

| QwenPaw 事件 | 网关要做什么 |
|---|---|
| `text` with `delta:true` | 累积 `text` → 作为正文增量推给前端（`{type:"text", text}`） |
| `text` with `delta:false` | **忽略**（快照，避免重复） |
| `plugin_call` | 新增一条"轨迹步骤"：`{name, arguments(解析后), call_id}`，状态=运行中 |
| `plugin_call_output` | 用 `call_id` 匹配上一步 → 标记完成 + 记 `output` |
| `data` | **忽略**（分片，快照已覆盖） |
| `message` | 可用于"消息边界"（本次实现可忽略，靠 text 累积） |
| `response` (`status:completed`) | 结束流；用它校验最终全文 |
| `response` (`status:failed`) | 结束流 + 报错 |
| `turn_usage` | **记 token / 成本 / model_name**（网关计量） |
| 任意事件含 `status:cancelled` | 用户点了停止 → 正常结束 |

---

## 5. 已确认的其他事实（Step -1 结论）

| 项 | 结论 |
|---|---|
| **`role: "system"` 是否被接受** | ✅ **接受且生效** —— 实测塞一条 `{"role":"system","content":[{"type":"text","text":"从现在起你只用英文回答"}]}`，模型随后**真的用英文回答**。→ `system_suffix` 用**方案 ④**（网关每轮注入一条 system 消息），最干净。 |
| **请求里带 `model` 是否生效** | ❌ **被忽略**（见计划 ⑲）→ 甲案：所有智能体共用同一模型 |
| **`/healthz` 是否含版本** | ❌ 只回 `{"status":"ok"}`；**版本号要读 `GET /`** 的 `version` 字段 |
| **本机免鉴权** | ✅ `allow_no_auth_hosts` 含 `127.0.0.1`/`::1`，网关内网直连不需要 token |
| **X-Agent-Id 选 profile** | ✅ 生效（实测带 `X-Agent-Id: default` 走的就是 default 的会话与记忆） |

---

## 6. 复现方式

```bash
# 录一段含工具调用的流（本机免鉴权）
curl -s --max-time 150 -X POST http://127.0.0.1:8088/api/console/chat \
  -H 'Content-Type: application/json' -H 'X-Agent-Id: default' \
  -d '{"input":[{"role":"user","content":[{"type":"text","text":"用 read_file 读 /tmp/probe-agent.txt 并告诉我内容"}]}],
       "stream":true,"session_id":"probe-tool-flow"}' \
  -o tests/fixtures/sse-tool-flow.sse
```
