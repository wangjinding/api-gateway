# CHANGELOG

## 2026-09-24 · 模型接口加入上下文参数（context_window / max_output_tokens）

老板要求：查两家文档有没有写上下文长度，有就改网关、接口加入上下文参数，并纳入每日同步。

**数据来源（逐一核查，结论不同）**
- **Agnes 有**：官方**每个文本模型详情页**的「限制与价格」表写明 `上下文窗口` / `最大输出`（定价页没有这两项）。
  `agnes-3.0-flash` = 512K / 65,536、`agnes-2.5-flash` = 512K / 65.5K、`agnes-2.5-pro` 与 `agnes-2.5-pro-beta` = 1M / 65,536。图片/视频模型页无此表 → 留 0。
- **OpenCode 没有**：官方文档（Zen 页、`v2/docs/console/go/`）**都不写**上下文长度；`/zen/go/v1/models` 实测只回 `id/object/created/owned_by`（单模型详情 404、`include` 参数被忽略）。
  唯一机器可读来源是 **models.dev**（OpenCode 团队维护的模型目录），其 `opencode-go` / `opencode` 两供应商每条模型都带 `limit.context` / `limit.output`。
  - **口径（老板拍板）**：只用于**更新网关已有模型**，**不拿它当增删依据**（models.dev 收录比官方 Go 文档多，含 `omen-alpha` 等文档未收录的）；同名模型跨源不一致时取**保守下限**（`minimax-m3` Go 1M / Zen 512K → 512,000；`qwen3.6-plus` → 262,144）——宁可早压缩，不能超窗口。
  - models.dev 拉取失败 → 返回 `None`，**只跳过上下文同步**，不产生漂移、不写盘。

**改动**
- `app/config.py`：`ModelInfo` 新增 `context_window` / `max_output_tokens`（默认 0 = 未标注），解析 + 序列化一并带上。
- `app/main.py`：`GET /models` 与 `/v1/models` 输出这两个字段；**别名继承目标模型的值**。
- `app/settings.py`：「供应商与模型」面板往返不丢字段（默认 0 → 未配置的模型不会被补出 `context_window: 0` 污染 YAML）；页面提交了就以此为准，没提交则沿用文件原文。
- `data/gateway.yaml`：37 个模型落盘（33 OpenCode + 4 Agnes 文本），**文本级插入保住全部手写注释**（注释行 162 → 236）。
- `scripts/sync_models.py`：新增 `fetch_models_dev()`、`fetch_agnes_context()`（逐模型页解析「限制与价格」表），`diff()` 产出 `ctx_drift`，`apply_plan` 走 `_set_context`（块内改值、保留行尾注释、幂等），`verify()` 回读校验，报告显示漂移；`_DRIFT_KEYS` 与 `plan_is_empty` 认 `ctx_set`。
- 测试 **201 passed / 0 failed**（新增 3 个），`ci_check.sh` 全绿。

**上线**：`supervisorctl restart api-gateway:api-gateway_00`（pid 3492804）→ 实测 `/v1/models` 45 个模型、**38 个带 `context_window`**（含 `wjd` 别名继承 = 1,000,000 / 384,000），图片/视频模型正确为 0；health 200。代码已推 CNB（`08a4aee`，远端 = 本地）。

## 2026-09-24 · 修「QwenPaw 缓存命中率恒 0」：流式 usage 丢掉 `prompt_tokens_details`

**现象**：老板在 QwenPaw 上用本网关（`wangjinding` 自定义供应商）调 `deepseek-v4.1-flash`
（别名 `wjd`），网页显示的**缓存命中率一直是 0**；同一模型直连 OpenCode 是 94~99%。

**排查（全部实测，逐层证伪）**
1. ~~QwenPaw 组装 messages 有问题~~ → **证伪**：6 份连续请求逐条 diff，前面所有消息
   逐字节一致、仅末尾追加 2 条（msgs 362→372 单调递增），tools 26 个逐字节相同 ——
   **教科书级的递增前缀**。
2. ~~网关发给上游的 payload 有问题~~ → **证伪**：出站 payload 的 model / stream /
   stream_options 全同，session 稳定。
3. ~~上游没给缓存 / 字段名非标准~~ → **证伪**：拿网关**真实出站 payload** 原样直打上游，
   `cached_tokens = 126464 / 126848`（pt=126897 / 127648，≈99% 命中）。
4. **真因** → 网关**流式响应**把上游 usage 重建成只含 `prompt_tokens` /
   `completion_tokens` / `total_tokens` 的版本，**`prompt_tokens_details.cached_tokens`
   在这一步被丢弃**，末尾又用这份残缺 usage 覆盖上游原始值发给客户端 —— QwenPaw 从未
   收到缓存字段，只能记 0。（同一个 bug 也导致网关自己的控制台看不到缓存命中。）

**修复**
- 新增 `_merge_usage(dst, src)`：**整数字段累加、非整数字段（`prompt_tokens_details`
  这类整份快照）覆盖**，实现 usage 全字段透传；流式累加改用它。
- `_UsageScanner`（三线直传的记账扫描器）顺带收集 `cached_tokens`，`counts()` 多返回
  该键（消费方按名取值，不改既有行为）—— 网关控制台的缓存口径同步修正。
- 非流式路径本就原样透传 `result["usage"]`，无需改动。

**回归**：`test_merge_usage_keeps_prompt_tokens_details`、
`test_usage_scanner_collects_cached_tokens`；测试 **198 passed / 0 failed**。

**上线验证**：流式请求第 2 次 usage 已带 `prompt_tokens_details.cached_tokens = 7168`
（修复前该字段整个不存在）。

**方法论留档**（下次遇到「某个量归零」）：网关的**流式与非流式是两条互不相干的代码
路径**（QwenPaw 走流式 `_stream_response`），插探针必须插对分支；排查顺序应是
**入站 payload → 出站 payload → 上游原始响应 → 最后才查网关有没有把它改坏**
（这次真凶恰好就在最后一环）。

## 2026-09-24 · 删除 deepseek-flash / omen-alpha + 立「上生产必推 CNB」规则

**老板三条指令**：① 那两个受保护的模型「删了」；② 报告不用改发飞书；③ 记住并写进文件与记忆——**只要项目重启上线生产的代码都要推送到 CNB**。

### ① 删除 `opencode/deepseek-flash` / `opencode/omen-alpha`
- 此前在 `PROTECTED` 名单里护着（上游在服务、官方文档表未收录，怕收录滞后删错）；老板拍板以上游文档为准 → **名单清空**（机制保留：以后要临时护住某个 id 在此登记即可；被别名 / media 引用的 id 本来就有独立保护）。
- `--apply` 落盘删除，opencode 网关 35 → **33 = 文档 33**（Agnes 10 = 10，无漂移）；两个模型块原文归档到 `/root/.trash/2026-09-24-opencode-models-doc-sync/removed-model-blocks.yaml`。
- 线上确证：`hot_reload` 生效，`/models` 已不含这两个 id（总数 47 → 45），Agnes 的免费 / 限时免费标注不受影响。
- 顺手修一个删块 bug（本次实测踩到）：`_find_model_blocks()` 用「下一个 `- id` 行」当块尾，会把**紧贴下一个模型的分组标题注释**（本文件惯例：`# ── …` 写在组里第一个模型上方；同步自己写的「官方文档新增」标记同理）一起吞掉 —— 删 `deepseek-flash` 时连带删了 `# ── Anthropic Messages 线（文档主推这条）`。现改为块尾只收口到「块头注释」之前（`_block_end` + `_BLOCK_HEADER_RE`），标题已恢复，回归用例 `test_remove_block_keeps_next_group_header_comment` 钉住。
- 测试 **196 passed / 0 failed**。

### ② 近两日积压提交全部推送 CNB（9 个 commit）
本地领先远端 9 个（含模型同步、Agnes 免费标注、间距修复、本轮删除），一次性带 token 推送完成：
`ecb99e6 → afd2f69 → 16d973c`，**远端 `master` tip = 本地 HEAD**，`.git/config` 无 token。

### ③ 立「上生产必须同步推 CNB」规则（老板硬性要求）
- `README.md` 新增段落：凡**重启上线生产**（`supervisorctl restart api-gateway:api-gateway_00`）或 **`npm run build` 覆盖 `frontend/dist`** 的代码改动，验证通过后**必须**推 CNB；顺序＝改代码 → 测试/CI → 上线 → 确证线上生效 → commit → push；纯注释/配置靠热重载生效的也照推。唯一原则：**线上跑着的代码 = 仓库里的代码**。
- 新增 `scripts/push_cnb.sh`：一次性带 token 内嵌 URL 推送，推完自动核对「远端 = 本地」并复查 `.git/config` 无 token；无 token 退出 2、工作区不干净退出 3；**脚本内不内置 token**（凭据只在 `MEMORY.md`）。

## 2026-09-24 · Agnes 免费 / 限时免费标注 + 免费标签间距对齐

**老板需求**：① Agnes 的模型也要标「免费」或「限时免费」；② 标注和模型名的间隔不一样，优化一下。

### 交付
| 部分 | 内容 |
| --- | --- |
| 判定口径 | Agnes 定价页「### 当前优惠」小节：`当前（均）免费` → 免费；`当前限时免费` → 限时免费。按 bullet 切块 + 严格措辞，**只认反引号里的完整 id**（`agnes-video-2.5` 是 `agnes-video-2.5-flash` 的子串），`输入图片前 5 张免费` 这类计费额度不算模型免费 |
| 配置 | `free: true`（免费）+ 新增 `free_limited: true`（限时免费）。落盘 7 条：`3.0-flash` / `2.5-flash` / 3 个 `image-*-flash` / `video-v2.0` = 免费，`video-2.5-flash` = 限时免费；只 +8 行，手写注释一行未动 |
| 每日同步 | `scripts/sync_models.py` 新增 `_agnes_free_map()` / `_set_agnes_free()`：免费标注纳入漂移比对与 `--apply` 落盘（`free` + `free_limited`），官方改口径时**整行撤掉**标注；回读校验新增 Agnes 免费位检查 |
| 后端 | `ModelInfo.free_limited` 字段 + `/v1/models` 带出 `free_limited`（别名随目标继承） |
| 前端 | 标签文案 `免费` / `限时免费`（中英各 2 个新键，i18n 755/755 对称）；间距修正：`.st-row .name-free { margin-left: 0 }` |
| 测试 | 新增 `test_agnes_free_map_rules`、`test_agnes_free_apply_and_revoke_round_trip`（加标注 / 限时 / 撤标注 / 幂等 / 相邻注释不被碰）；**195 passed / 0 failed** |

### 间距问题的实测根因
`.st-row` 是 flex（`gap: 8px`），而 `.name-free` 又给了 `margin-left: 6px` —— 标签左侧 8+6=**14px**、
右侧只有 **8px**，跟其它行「模型名 → 供应商名」的 8px 节奏对不上。用同源最小页实测：
修前 `14 / 8`，修后 `8 / 8`（且与无标签行的 8px 一致）。表格单元格是行内排布、没有 flex gap，
所以那边保留 `margin-left: 6px`，只对测速行归零。

### 上线
- 配置：`--apply` 落盘（备份 `data/gateway.yaml.bak`，权限 600，回读校验通过），`hot_reload` 自动生效。
- 后端：`supervisorctl restart api-gateway:api-gateway_00`（pid 3465113，health 200）。
- 前端：`npm run build` 覆盖 `frontend/dist`，线上 `/web` 已换 `index-CkVhRx-u.js` / `Models-RN5bN2Hd.js`（旧 chunk 404），入口包里能查到「限时免费」。
- 实测 `/v1/models` 载荷：Agnes 6 个免费 + `agnes-video-2.5-flash` 限时免费 + 3 个付费档无标注；`space-bunny-free` 免费标注不受影响。

## 2026-09-24 · 模型清单每日同步（全量落盘）+ 视频默认模型口径 + 修「空 plan 也写盘」

**老板需求**：网关的模型清单要和上游官方文档**同步**——文档新增的加进去、下线的删掉、
参数变了就更新；每天自动跑。

### 交付
| 部分 | 内容 |
| --- | --- |
| `scripts/sync_models.py` | 抓 OpenCode Go 文档 + Agnes 定价页，与 `data/gateway.yaml` 比对：默认只报告（退出码 0/1/2），`--apply` 真落盘 |
| 落盘方式 | **文本级**增删改（`data/gateway.yaml` 里逐条实测的手写注释不被冲掉）+ 复用网关 `write_config`（备份 / 原子替换 / 600）+ 落盘后**回读校验**（模型集合、定价、协议线、别名目标、无关 provider 是否被碰），不过就中止、不写盘 |
| 边界 | 协议线**只加不砍**；`PROTECTED` 名单与被**别名 / media 引用**的 id **永不自动删**，只在报告里点名（`deepseek-flash` / `omen-alpha` 上游在服务、文档表未收） |
| 定时 | QwenPaw cron 每天 08:00（Asia/Shanghai）跑 `--apply`，简报投控制台，报告追加 `logs/model-sync.log` |
| 首次同步 | Agnes +6（`agnes-2.5-pro` / `2.5-pro-beta` / `image-2.0-flash` / `image-2.1-flash` / `video-2.5` / `video-v2.0`）、`agnes-3.0-flash` 价目 0.35/1.00 → **0/0**（文档现价免费）、补 2 条价目；OpenCode 无漂移 |

### 视频默认模型 `media.video_model_name`：不自动换
文档对三个视频模型**没有**「哪个最新 / 最好」的机器可读标记（`agnes-video-v2.0` 当前免费、
`agnes-video-2.5` 按输出分辨率计费、`agnes-video-2.5-flash` 限时免费），自动挑一个切上去
等于直接改线上视频行为。所以脚本只做两件事：**保证它不被自动删**（`media` 引用的 id 受保护）
+ **报告里列文档视频清单当候选**（✓ 文档里还在 / ✗ 已下线）。换默认由人点模型名、改一行配置
（脚本 docstring、README、`data/gateway.yaml` 注释、cron 提示词四处都写明，防后手擅自切）。

### 修 bug：空 plan 也走写盘分支
`plan_is_empty()` 只看外层结构，而 `plan["add"] = {"opencode": {}, "agnes": {}}` 这种两层
dict 即使一个模型都没变也是**真值** → 每天无漂移也重写一次配置（并覆盖上一份备份），
报告还误报「已写盘：…N 字节」。改为下钻到 provider 桶判断，并加回归用例
`test_sync_plan_is_empty_drills_into_provider_buckets`。单测 192 → **193 passed / 0 failed**。

## 2026-09-21 · 用另一把 Zen API Key 再验免费档（结论不变：客户端锁定，写不进去）

**老板需求**：用新给的 `oc_sk_…` 这把 key 去调免费档试试。

### 实测（三组对照，同一台机器、同一时段）
| 端点 / 模型 | 结果 |
| --- | --- |
| `GET https://opencode.ai/zen/v1/models`（新 key） | **200，80 个模型**（含 9 个 `*-free`）→ key 本身有效 |
| Zen 端点调 8 个 `*-free`（`mimo-v2.5-free`、`mimo-v2.6-flash-free`、`ling-3.0-flash-fin-free`、`muse-spark-1.2/1.3-contributor-free`、`nemotron-3-ultra-free`、`nemotron-3.5-lightning-free`、`jev-1.13-free`） | **全部 `403 FreeTierError: OpenCode's free tier can only be used from within OpenCode`** |
| Zen 端点调付费模型（`deepseek-v4.1-flash` / `kimi-k3` / `minimax-m3`） | **全部 `402 Insufficient account funds`**（Zen 账户没余额） |
| Go 端点（同一把新 key）调 `space-bunny-free` / `deepseek-v4.1-flash` | **都 200**（这把 key 同样在 Go 计划内） |

### 结论（与上一轮一致，这次换 key 复核过）
1. **免费档（`*-free`）是服务端「客户端锁定」**：换 key 无效，只认 OpenCode 自己的客户端 ——
   不是权限配置问题。**本网关永远调不到**，所以**一个都没写入**（按老板「可以用才写入」的条件）。
2. **在线免费清单只有 9 个**（models.dev 那 29 个里大部分已下线，实测 `400 Model is unavailable`）；
   其中**唯一能用的 `space-bunny-free` 之所以能用，是因为它在 Go 计划里**（已经收录，`free: true`）。
3. Zen 账户**余额为 0**，付费 Zen 模型一律 402 → 现在加 Zen 供应商也没意义。
4. **没有尝试**伪装 OpenCode 客户端的 UA/签名去绕过 —— 那是官方明写的使用限制，绕过不合适。

## 2026-09-21 · 免费标志跟随模型名 + 轮换 Agnes Key + 复测 Zen 免费档（结论：一个都用不了）

**老板需求**：① 免费标志直接放在模型名称后面；② Agnes 供应商 API Key 换成新的那把；
③ 把 models.dev 里那批 `*-free` 用 Go 的 key 测一遍，能用就都写进网关。

### ① 免费标志位置（前端）
`frontend/src/views/Models.vue`：绿色「免费」标签从「能力」列挪到**「模型」列、紧跟模型 id 后面**；
左上「全部文本模型」测速行同样跟在名字后面（原先那枚闪电图标撤掉，避免同一件事标两遍）；
「能力」列恢复成只标「视觉 / 参数固定」，免费模型那格显示「—」。

### ② Agnes Key 轮换
`data/.env` 的 `AGNES_API_KEY` 换新（**值不入库**，只写在服务器上 600 权限的 `.env` 里；
`data/.env` 在 `.gitignore` 内）。重启后端后真实调用验证通过（见「验证」）。

### ③ Zen 免费档复测（29 个 `*-free`）—— 结论：一个都不能用，**未写入任何模型**
用 Go 的 key（`OPENCODE_API_KEY`）对 models.dev `opencode` 目录里 29 个 `*-free` 逐个打瘦请求
（`max_tokens=1`，带 `x-opencode-session` 与专属 UA）：
- 打 **Go 端点** `/zen/go/v1/chat/completions`：**29/29 全部 `400 Model is unavailable.`**；
- 打 **Zen 端点** `/zen/v1/chat/completions`：同样全部失败 —— 一部分 `400 Model is unavailable.`，
  另一部分是 **`403 FreeTierError: OpenCode's free tier can only be used from within OpenCode`**。

**结论**：这批免费模型被**客户端锁定**（只允许 OpenCode 自己调用），不是 key 权限不够、
也不是端点写错 —— 第三方网关（含本网关）**无论如何都调不到**。按老板「可以用才写入」的条件，
**一个都没写入**。**以后不要再重复测这批**（结论已定；`space-bunny-free` 是 Go 计划里唯一真能用的免费模型）。

## 2026-09-21 · 刷新 OpenCode Go 模型清单（35 个，标注免费模型）

**老板需求（原话）**：把 ai 网关的 OpenCode Go 的模型更新一下，**免费的也要标注出来**，弄好后重启上线。

### 数据来源
1. 上游活列表 `GET https://opencode.ai/zen/go/v1/models`（真实 key，35 条）；
2. 官方文档价格表 / 模型-端点表（`opencode.ai/docs/go`，给出每个模型的**原生线**与**价格**，`Space Bunny Free` 一行价格是 `Free / Free / Free`、额度 `Unlimited`（限时））；
3. models.dev 的 `opencode-go` 目录（36 条，用于交叉核对价格；其中 `ox-alpha-free` 也是 cost 0，但上游 `/models` 与官方文档都没有它 → 未收录）。

### 变更
- **新增 7 个**：`deepseek-flash`（三线全通）、`grok-4.7`（responses）、`omen-alpha`（chat）、
  `gpt-6-luna`（responses）、`mimo-v2.6-flash`（chat）、`mimo-v2.6-pro`（chat）、
  `space-bunny-free`（chat + messages）。
- **下线 1 个**：`union-alpha`（Union Alpha Free）—— 上游 `/models` 已不再返回，实测回
  `400 Model is unavailable.`；条目移除，免费位由 `space-bunny-free` 接替。
- **免费标注**：`space-bunny-free` 的 `display_name` 写成 `Space Bunny Free（免费）`，模型页/接口
  一眼可见；yaml 里同时写了官方价格表证据的注释。
- **协议线**：7 个新模型逐条真实调用验过（chat / messages / responses 各一只瘦请求，
  `max_tokens=1`，带 `x-opencode-session` 与专属 UA）——上游对不可用的线回
  `503 {"error":{"type":"server_error","message":"Upstream request failed: Endpoint is unavailable"}}`，
  据此定 `protocols`。顺手复核了 3 个存量多线模型（`kimi-k3` / `minimax-m3` / `deepseek-v4.1-flash`），
  标注仍然正确。
- **README**：供应商表模型数 17 → 35、模型清单小节按「线 + 免费」重写、三线直传小节数字同步。

### 踩坑记录（下次别再犯）
- 探测上游必须带**专属 UA**（`api-gateway/2.4.0 (jakon opencode-go)`）：用 python urllib 的默认 UA
  会被 Cloudflare 拦成 `403 error code: 1010`，看起来像"模型不可用"，其实是 UA 被 ban。

### 验证
- `yaml.safe_load` 通过、`providers.opencode.models` = 35、带「免费」标注 1 个；
- 全量单测与 `scripts/ci_check.sh` 六道门禁；
- 重启后线上 `/v1/models` 抽查新模型与免费标注（见提交说明）。

## 2026-09-18 · 第三十一轮：虚拟模型 id（别名 `wjd`）—— 客户端固定一个模型名，实际打给谁由页面决定

**老板需求（原话）**：设一个 `wjd` 模型 id，路由到指定的模型上（不是"跨格式"）；**管理员**在
**网关配置**里可以选择 `wjd` 映射的模型；**谁都可以用 wjd**（所有账号的 Key）；只有 `admin` +
**网页会话**能改；`wjd` 只配置成 `chat/completions`（OpenAI 格式），**不是 OpenAI 格式的模型
置灰不让选**；member 在模型页只看到**一行只读**的映射。

### 语义（一句话）
`aliases.wjd.target` 是一个**一对一指针**：客户端写 `model: wjd`，网关在入口把它换成目标真实
模型 id，之后的挑供应商 / 盖强制参数 / 记账 / 熔断**全部按真实模型走**（一份事实来源）。
响应回显客户端请求名（`model: wjd`）、真实目标在 `served_model`；访问日志 `model` / `served`
两列分别记两者。

### 新增
- `app/config.py`：`AliasSpec`（`enabled` / `target` / `note`）+ `GatewayConfig.aliases`；
  `resolve_alias()` / `alias_of()`；`_parse_aliases()` 支持 `wjd: opencode/kimi-k3` 简写。
  **别名不进 `_model_index`**（它不是模型）—— 想用别名必须显式 `resolve_alias()`，这样媒体端点、
  未知模型分支不会"顺手"接受它。
- 保存期四条硬规则（`validate_config`，唯一的边界）：
  ① 目标必须存在；② 目标必须是**文本**模型且支持 `chat/completions`（图像/视频模型在 yaml 里
  没写 `protocols`、看着像 OpenAI 线，实际不能用 → 直接拦）；③ 别名 id 不得与真实模型 id 重名；
  ④ 启用却没写 target → 报错。**停用（`enabled: false`）时不校验 target** —— 允许"先停用再慢慢
  改目标"。别名 id 限 `[A-Za-z0-9._-]{1,64}`。
- 三处入口行为：`/v1/chat/completions`（含 `/batch`）→ 换 id 正常转发；
  `/v1/messages`、`/v1/responses` → **400 指路**（说清目标是谁、该用哪个端点，且**一次都不打上游**）；
  `/v1/images/generations`、`/v1/videos*` → **400 明确拒绝**（别名只在文本线上存在，不是"模型不存在"）。
- `GET /v1/models` 增加别名条目（`protocols: ["chat/completions"]` + `alias_of` + `alias_note`，
  视觉/参数/供应商/类型**继承目标**）；停用的别名不出现。
- 流式：别名调用时把 chunk 里的 `model` 统一成客户端请求名（元信息帧本就如此），
  同一条流不出现两个模型名。**非别名调用一个字节都不动。**
- `app/settings.py`：`config_to_view` 带出 `aliases`，`view_to_raw` 支持按段提交（没提交就沿用
  原文，提交空映射 = 明确清空），渲染 yaml 时插到 `providers` 之后并带说明注释。
- 前端「供应商与模型」页：新增「虚拟模型 id（别名）」卡片 —— admin 是 `启用` 开关 + 目标下拉
  （**只有支持 `chat/completions` 的文本模型可选，其余置灰并在标签里写明它支持哪几条线**）+
  「保存映射」；member 是只读一行（数据来自 `/v1/models` 的 `alias_of`，普通账号读不到也不必读
  `/config`）。别名**不进**模型计数、不进一键测速列表、不进模型表（它在自己的卡片里）。
- 日志：指向变更逐条记 `虚拟模型 id 生效/改指向/停用（<来源>）: wjd → …`，来源含页面保存的账号
  或"配置热重载" —— 这个入口被改过要能在日志里查得到。

### 验证
- 单测 **187 passed / 0 failed**（新增 8 条：四条规则与解析、目录继承、chat 线端到端、另两条线
  400 且不打上游、媒体端点拒绝、测速明确说明、流式只有一个名字、面板读写往返 + 非法目标 400 不写盘）。
- `typecheck` 0 错；`check:i18n` 中 753 / 英 753 对称；`check:svg` 通过。
- 生产实测：见下（改指向即时生效、`/v1/models` 带 `alias_of`、另两条线 400、媒体 400）。

## 2026-09-18 · 第三十轮：全面体检发现项「全部」修完（下载白名单 / 视频归属 / 会话落盘 / i18n / 口令下限）

**老板决策**：上一轮交了「只查未改」的体检清单，老板回一句「**全部**」—— 逐项修完并直接上生产验证。

### P0（实测复现的真 bug）产物下载白名单
- `download.allowed_hosts` 只放了 `cos-platform-outputs.agnes-ai.cn`，而 Agnes 的**水印版**
  产物走另一个 CDN `maas-watermark-prod-new.cn-wlcb.ufileos.com`（admin 历史里 9-16 的 3 条产物
  都是这个域名）→ 页面「下载」必 400「该地址不在允许下载的域名白名单内」。
- 补上水印域名；`data/gateway.yaml` 与 README 都写清**这张表是出网白名单、换 CDN 要同步维护**。
- 生产实测：`GET /v1/media/download?url=<水印图>` → **200 / 129871 bytes**（改前 400）；
  非白名单域名（`evil.example.com`）仍 400。

### 视频任务归属（新增 `app/video_owners.py`）
- 原先 `GET /v1/videos/{video_id}` **不判归属**：任何已登录账号拿到 id 就能读别人视频任务的状态
  与产物 URL（与 users/history/metrics 的「按账号隔离」口径不一致）。
- 创建成功那一刻登记 `video_id → 账号`（`_owners.record`，走 `_offload`），轮询据此判权：
  **非归属账号回 404**（而非 403 —— 403 等于确认 id 存在，会把 id 变成存在性探针）、
  **admin 放行**、**无登记放行**（改动前创建的老任务 / 落盘丢失时，渐进收紧，不砸正在轮询的流程）。
- 落盘 `data/video_owners.json`（600、原子写、TTL 7 天、上限 2000 条）。

### 登录会话落盘（`app/sessions.py`）
- 此前纯内存 → **每次重启把全体网页登录踢下线**。现在落盘 `data/sessions.json`：
  **只存 token 的 sha256**（不存 token 本身，备份泄漏也无法直接冒充）、原子写 + 权限 600、
  写失败只告警（退化成原行为）、载入时丢弃已过期会话。
- 吊销语义不变且**吊销也落盘**（改密 / 重置 / 删号 / 停用 → 该账号全部会话立即失效）。
- 生产实测：登录拿 token → `restart` → **同一 token 仍 200**（`/auth/apikeys`）；
  文件里 `token in raw == False`。

### 口令下限 4 → 8（`app/users.py`）
- `MIN_PASSWORD = 8`。只作用于「建号时显式给密码 / 事后改密」；**登录不做长度校验**，
  故既有短口令账号（如 `test`）照常能登，只是不能再被改回短口令。前端 `newPw`/`newUserTip` 同步。

### `/config/env` 值校验（配置页环境变量卡）
- 拒绝含 `\n` / `\r` / `\x00` 或长度 > 4096 的值 —— `.env` 是**逐行重建**的，一个带换行的值
  就能在文件里凭空多出几行、改掉同一份 `.env` 里的其它变量。
- 生产实测：换行值 / 超长值 / 非白名单变量名均 400，且 `md5(data/.env)` 前后一致。

### 性能 / 一致性
- `history` 四个端点补 `_offload`：HistoryStore 每次**整份**读写（上限 4MB），此前是唯一还在
  事件循环里同步跑的文件 IO（users/feedback/config 早已挪到线程池）。
- `GET /` 导航里的 `docs` 一行改为**仅当 `docs_enabled` 时**列出（关掉时路由不存在，列着会 404）。
- `/health/{provider}` 注释更正：它一直是 `require_auth`（**恒定要鉴权**，不随 `health_auth` 放宽），
  旧注释误写「同受 health_auth 保护」。保留更严的行为（它会用网关的 Key 打上游 `/models`），改注释。

### 前端 i18n 收口
- `views/Videos.vue` 素材摘要（原硬编码 `首帧:xx.png`）、`lib/upload.ts` 全部用户可见报错与
  preset 名、`lib/charts.ts` 空态文案 → 全部走 i18n（新增 `videos.media*` 5 键 + `upload.*` 15 键，
  中英 **737/737** 对称）；`lib/upload.ts` 取全局 `i18n.global.t`（模块非组件）。
- 删除 `router.ts` 中**无人消费**的 `meta.title`（只有中文的死元数据；菜单标题唯一来源是
  `lib/nav.ts` + i18n）。
- **`frontend/scripts/check-i18n.mjs` 新增「硬编码中文」扫描**：剥掉块/HTML/行注释（避开
  `https://`）后扫字符串字面量，命中即 build 失败；允许清单仅放「语言自称」（登录页的 `中`）。
  自测：临时塞一句 `'中文硬编码自测'` → 构建前置检查正确抓出。

### 文档 / 基建
- README：`.env` 段补 `OPENCODE_API_KEY`、说明**两层 `.env`**（`data/.env` 用
  `override=True` 压过根 `.env`，值不得含换行/控制字符、≤4096）、修掉 `data/gateway.yaml 的
  auth.users` 死引用与「自动路由」措辞、安全基线补「下载代理白名单维护」「视频任务归属」、
  数据文件清单补 `sessions.json` / `video_owners.json` 与对应 `GATEWAY_*` 覆盖项、测试计数
  165 → 179。
- `.env.example` 重写：删 `B_AI_API_KEY`（供应商早已删除）、补 `OPENCODE_API_KEY` 与全部可覆盖
  路径，写清三层优先级（真实环境变量 > `data/.env` > 根 `.env`）。
- `Makefile`：`lint` 从**手写文件清单**改 `python -m compileall -q app run.py`（原清单漏了
  `users.py` / `history.py` / `feedback.py` / `settings.py` / `sessions.py` / `video_owners.py`）。
- `.gitignore`：补 3 个测试临时文件、`data/sessions.json`、`data/video_owners.json`、`data/*.bak`；
  仓库里 3 个 `data/gateway.yaml*.bak` 执行 `git rm --cached`（本地文件保留，只是不再入库）。
- `app/settings.py`：配置备份写完后**显式 `chmod 600`**（`shutil.copy2` 会照抄源文件权限，
  源是历史遗留的 644 时备份也会跟着 644）。
- 新增 `scripts/ci_check.sh`：一条命令跑完「后端 compileall + 单测 + i18n/SVG 体检 + `vue-tsc` +
  build」（`--fast` 跳过 build），本地与 CI 通用；**任何一步失败都继续跑完再汇总**（一次暴露全部问题）。
  **没写 `.cnb.yml`**：CNB 流水线的配置 schema 我没把握，不想靠猜去改线上流水线 —— 交给老板确认后
  把 `bash scripts/ci_check.sh` 挂上去即可（脚本退出码就是判定依据）。
- `Makefile` 的 `test` 目标注释改成实话：环境里**没装 pytest**，「`pytest tests/ -q`」照着敲只会
  `command not found`；这套单测是自带 runner 的独立脚本，`python tests/test_gateway.py` 才是能跑的。
- `tests/` + `app/` 的 `flake8 --select=F,E9` 清到 **0**（原本 5 处：未用 import ×4、赋值未用 ×1）。
- 一键测速探针**不传 session** 处补注释：这是**有意**的 —— 复用真实会话值会被上游前缀缓存拉成
  「假快」，测速失去意义（所以适配器回退随机值）。

### 测试
- **179 passed / 0 failed**（173 → 179）。新增：会话落盘（跨重启有效 + 盘上无明文 + 吊销落盘）、
  过期会话载入即丢、视频归属语义（本人 / 他人 / admin / 无登记 + 跨重启保持）、端点级
  「他人 poll → 404」、`/config/env` 值校验（4 种坏值 + 沙箱写路径生效）、口令下限（含
  「默认密码=账号名不受限、短口令仍可登录」）。
- **修掉一个测试隐患**：写路径此前没有环境变量覆盖，测试一旦覆盖到就会**直接改生产
  `data/.env`** —— 新增 `GATEWAY_ENV_FILE`（与 `GATEWAY_SESSIONS` / `GATEWAY_VIDEO_OWNERS`
  同套路），测试全部指到 `.test_*` 沙箱文件。

### 结论：CSP 的 `'unsafe-eval'` 现在还不能删（有 A/B 实测）
- 临时把 CSP 改成 `script-src 'self'` 并重启 → 真浏览器打开 `/web/#/login`：**`#app` 全空、
  登录表单完全不渲染**；改回 `'unsafe-eval'` → 表单与中英切换全正常。
- 根因：打包产物里唯一一处 `new Function` 在 **vue-i18n 的消息编译器**（运行时编译带插值/复数的
  文案需要 eval）。**正解是把文案编译搬到构建期**（`@intlify/unplugin-vue-i18n` 预编译消息），
  在没做这件事之前不要动这条 CSP —— 旧注释里「本地无 CSP 的 vite preview 看不出、上线会白屏」
  是对的。

### 上线与验证
- 后端 `supervisorctl restart api-gateway:api-gateway_00`；前端 `npm run build` 覆盖
  `frontend/dist`（网关 `StaticFiles` 直接挂载）→ 线上入口 **`index-Bf3pWvHR.js`**。
- 生产验证：`/healthz` 200、登录页 + 中英切换（真浏览器）、`/health/agnes` 无凭据 401、
  `GET /` 导航无 `docs`、env 值校验 400 且文件未变、水印图下载 200、未登记 video_id 轮询
  照常走通（无 500）、同一会话 token 跨重启仍有效。

## 2026-09-18 · 第二十九轮：`x-opencode-session` 缺省按 **API Key 稳定**派生（提缓存命中）

**老板决策**：把网关接入 agent / QwenPaw 时，客户端多数不传 `conversation_id`，网关此前
回退成**每调用一个随机 UUID** → 上游（OpenCode Go）提示词缓存命中率恒为 0（换 session 就不
吃缓存）。

### 改法（`app/main.py`，只动网关，不动 QwenPaw）
- 缺 `conversation_id` 时不再随机：
  - 请求带 **API Key** → 上游会话 = `sha256("k:" + <该 Key 的随机串，即 wjd- 后面那串>)[:32]`，
    一个 Key 一个稳定会话（`_key_token()` 去前缀 + `_upstream_session` 哈希）。
  - 只有**登录会话**（页面）→ 上游会话 = `sha256("u:" + 账号)[:32]` 稳定兜底。
- 仍保留：客户端显式带 `conversation_id` → 按「账号:会话」哈希（按会话隔离）。
- **安全取舍**：不把 Key 随机串本体直接发给 opencode（那串是凭证内容，泄漏给第三方等于半个
  Key），而是先 `sha256` 再发；上游只看到 32 位十六进制串，既不暴露账号名也不暴露 Key 明文。

### 验证
- 单测 173 passed / 0 failed（`test_conversation_id_isolated_per_account`、
  `test_api_conversation_id_flows_to_session_header`、`test_non_ascii_...` 已更新为「不传→非
  None 稳定会话」新语义）
- 生产已重启（`api-gateway:api-gateway_00`，pid 1488509），`/healthz` ok、无回归
- **真实的 `cached_tokens>0` 演示暂被挡**：opencode 免费 5 小时额度仍锁定（上游 429），待额度
  重置（约 3h）后可再实测两通相同前缀调用确认命中。

## 2026-09-18 · 第二十七轮：三线直传 + **彻底删除路由别名**

**老板决策**：wjd 路由这个功能去掉、删除掉，相关的代码和文档一起更新。

### 删除（路由别名 / fallback 链 —— 整块功能）
- `app/config.py`：删掉 `RouteInfo` 类、`GatewayConfig.routes` 字段、`find_route()` /
  `all_routes()` / `validate_routes()` 三个方法，以及 YAML 里 `routes:` 段的解析。
  老配置残留的 `routes` 段**不报错、直接忽略**（日志一条 warning），免得旧配置起不来。
- `app/main.py`：删掉 `_route_chain()` / `_order_chain_by_speed()` / `_filter_chain_by_protocol()` /
  `_vision_first()` / `_chain_has_vision()`；`_chat_fallback()` → `_chat_once()`（单模型 + 同模型
  重试 + 熔断，不再"这个不行换下一个"）；流式 `_stream_response()` 单模型化（新增 `_sse_error()`
  统一错误帧）；batch / 媒体端点 / 热重载 / 启动日志 / `/` / `/health` / `/v1/models` 里的路由痕迹
  全部清掉。
- `app/metrics.py`：删掉 `routes` 维度、`fallback_total` 与 `record_fallback()`；指标只剩
  **模型 / 供应商 / 账号** 三个维度。
- `app/settings.py`：配置面板视图与写回不再有 `routes` 段。
- `data/gateway.yaml`：`routes: {}` 整段删除，`server.retry` 注释去掉 route.retry 说法。
- 限流从三级（全局/路由/供应商）变**两级（全局/供应商）**；`route.timeout`、路由级告警一并消失。
- 前端：Models 页路由别名表 → 全部文本模型 + 一键测速；Chat 页去掉"路由"选项组与降级徽标；
  Metrics 页「路由 vs 供应商」→「按供应商成功/失败」；Dashboard 路由链卡片 → 协议线覆盖卡片；
  Settings 默认模型下拉去掉 `wjd`；ApiKeys 页示例改真实模型 id；中英文案同步（699 / 699 对称）。

### 保留
- **一键测速**（Models 页每行的延迟实测）保留，改为按**模型自己的协议线**探测。
- `wjd-` 开头的 API Key 前缀规则不变（那是 Key，不是路由）。
- `app/routing.py` 保留 —— 它只做「model_id → 供应商 + 认证解析」，与被删的路由别名无关（名字沿用）。

### 为什么删（不是"藏"）
三线直传落地后「入站格式 = 出站格式」，模型 id 必须与协议线一一对应；再套一层"自动挑最快模型"
的别名，会把调用方选定的**格式与能力**（视觉 / 工具 / 协议线）悄悄换掉。要换模型就换 `model`，
网关不替谁做主。

### 验证
- 后端单测：**165 passed / 0 failed**（删 6 个路由引擎用例、改写 10 个、新增 2 个）。
- 前端：`vue-tsc --noEmit` 0 错、`npm run build` 成功、`check-i18n` 中英 699/699 对称。
- 生产**未动**：后端需 `supervisorctl restart api-gateway:api-gateway_00`（踢会话），
  前端 dist 覆盖需老板点头。

### 上线（老板批准重启，2026-09-18 15:16）
- 前端 `npm run build` 产物覆盖 `frontend/dist`（新入口 `index-CWUVm0px.js`，公网
  `https://api.wangjinding.com/web/...` 已服务新入口、旧入口 404）。
- 后端 `supervisorctl restart api-gateway:api-gateway_00` → 新 pid，127.0.0.1:8808 正常，
  `/health` 200、启动日志「已加载 3 供应商」。
- **代码仍未提交 git**（老板明确要求"先别提交"）。

### 上线后真机复验：抓到并修掉 3 个本轮新引入的缺陷（P0×3）
老板批准上线后，用 `live_check.py`（真实 key、走公网网关）复验，发现：
1. **【P0】三线直传入门即 500**：`_passthrough()` 里 `_enforce_rate_limit(cfg, request, None, pkey, model_in)`
   多传一个参数（函数只收 4 个）→ **每一次** `/v1/messages` 与 `/v1/responses` 都 `TypeError` → 500。
   即本轮的核心功能两条线全灭（chat/completions 不受影响）。已改为 4 参调用。
2. **【P0】`/metrics/prometheus` 500**：导出器仍读 `snapshot["fallback_total"]`（该字段已随路由/fallback 删除）
   → `KeyError`。已删该指标行 + 其 HELP/TYPE，并顺带删掉 `/health` 里恒为 0 的 `fallback_total` 死字段。
3. **【P0】图像/视频的 token 没发出去**：本轮改为"鉴权头按协议线在 `_req_headers` 组装"后，
   `create_adapter` 不再往静态 headers 塞 `Authorization`，但媒体三个调用点仍在用 `self._headers`
   ⇒ 图像生成/视频创建/视频轮询出站零鉴权头。真机：Agnes 图像 401「未提供令牌」，而同一 key
   直接 curl 同地址 **200**。已新增 `_media_headers()`（静态头 + Bearer + 会话头）并在三处使用。
4. **测试基建（漏检根因之一）**：`tests/test_gateway.py` 的 `if __name__ == "__main__"` 运行块夹在文件中间，
   脚本模式下**只收集执行到该行时已存在的测试** → 定义在其后的用例根本不跑
   （`test_image_request_goes_to_requested_model` 白写了一轮）。已把运行块移到文件末尾，
   并新增 6 条**真发请求**的回归测试（`/v1/messages` 往返 + conversation_id 不透传、`/v1/responses` +
   跨线 400 闸门、直传供应商级限流 429、上游 4xx 原样回且不计熔断、流式字节原样转发 + 上游 4xx 真实状态码、
   Prometheus 200 且无已删指标、媒体三处带 Bearer）。
   单测 **165 → 173 passed / 0 failed**。
5. **文档订正**：README 残留的旧句「调用时 `model` 填 `wjd`，网关按顺序尝试」删除（路由已整块移除）；
   鉴权小节「Key 均不落明文」改为与「Key 明文回显」取舍一致的表述。

### 复验结果（重启后再次真机）
`live_check.py`：**19 PASS / 0 FAIL / 0 WARN** —— 三线直传真实 200、跨线 400、`/metrics/prometheus` 200、
图像生成 200。另手工验证：视频轮询可达上游（404「任务不存在」而非 401「未提供令牌」）、
图像下载 `/v1/media/download` 560KB PNG 正常落盘、一键测速三条线各测一个模型均 ok
（chat/completions 7106ms、responses 2014ms、messages 1360ms）。


按轮次记录 api-gateway 的修复与优化（README 侧重能力/配置说明，这里侧重每轮的改动、排查、验证与上线）。

---

## 2026-09-18 · 第二十六轮：会话 id 入站校验 + 按账号隔离（老板：「修掉」）

**背景**：老板问「用网关 API 直接调 OpenCode Go 时 `x-opencode-session` 是什么逻辑、同一会话怎么设计」，
顺带实测出两个问题。查证与实测过程见 `memory/2026-09-18/`。

### 实测事实（真实 key）
- 上游**硬性要求**该头：缺失或空字符串 → `400 MissingSessionID`；**格式不限**（UUID / `user1:conv-42` /
  300 字符 / 4000 字符都收）。
- **⚠️ 结论更正（同一轮内自我推翻，务必记住）**：最初用「同一长 prompt 打 4 次」得到
  "缓存以该头为作用域"（同 session 第二次全量命中 897、换 session 归零、同 session 末尾追加前缀命中 832）。
  随后补做**时间对照实验**才发现那是**时间巧合**：同一 session 连打 6 次得到 `[0,0,0,0,896,896]`
  （前 4 次全不命中）；等 25 秒让缓存落定后**换全新 session** 打同一 prompt，出现「命中 936」与
  「不命中 0」**混合**。
  → **真实结论：`cached_tokens` 与该头没有稳定关系**。上游是机会性的**内容前缀缓存**（命中时比例很高，
  但不可依赖），推测受上游多实例/缓存分片/写入延迟影响。该头的真实作用是上游的**会话标识**（必带，缺失 400）。
  → **方法论教训：判断"缓存是否绑定某个变量"，必须做时间对照**（先让缓存落定，再只变那一个变量），
  否则会把"缓存写入延迟"误读成"变量生效"。一次顺序观察不足以定性。

### 修的 Bug 1：非法 `conversation_id` 被误报成「上游故障」
`conversation_id` 此前**零校验**。传中文（或任何非 ASCII）时：httpx 组 header 抛
`'ascii' codec can't encode characters` → 被当成上游失败 → **重试 3 次**（必然全失败）→
最终回 **502「路由 x 全部模型调用失败」** + 记熔断。客户端传错值，却被伪装成上游挂了。

- `app/models.py`：新增 `CONVERSATION_ID_MAX = 128` + `ChatRequest._validate_conversation_id`
  （剥两端空白；空白串按"未传"处理回退随机 UUID；非 ASCII / 超长 → 校验失败）。
- **入站即 422，且一次都不发上游**（回归用例断言适配器零调用）。
- *连带修掉一个通用隐患*：pydantic v2 会把校验器抛出的**异常对象**放进 `errors()[i]["ctx"]["error"]`，
  而 422 处理器原先直接把 `exc.errors()` 塞进响应体 → 序列化抛 `TypeError` → 中间件兜底成
  **500「网关内部错误: TypeError」**（任何自定义校验器都会踩）。现新增 `_jsonable_errors()`
  清洗 ctx（异常对象转字符串）、收敛 input，并在输出前 `json.dumps` 自检降级；顺带剥掉 pydantic 的
  `Value error, ` 前缀（字段名已在 `loc` 里，避免「conversation_id: Value error, conversation_id …」的噪音）。

### 修的 Bug 2：会话 id 原样透传 → 跨账号共用同一个上游会话标识
两个账号只要用了同一字符串（`test`、`1`、照文档抄的示例），就会把**同一个会话标识**发给上游。
没有证据表明这会让对方看到你的内容（缓存/上下文都不会把别人的回答返给你），但语义上是错的：
上游以为这是"同一个会话"，账号之间在归属/记账/可能的限流口径上互相串味。

- `app/main.py`：新增 `_upstream_session(conversation_id, account)`，派生
  `sha256(f"{account}:{conversation_id}")[:32]`；非流式（`_chat_fallback`）与流式（`_stream_response`）
  两个调用点统一改用它。同一账号同一对话仍是恒定标识（语义正确），账号间天然隔离，
  且上游只看到一个 32 位十六进制串（不暴露账号名与业务命名）。
  *注意：不要把它当成缓存优化——缓存与该标识无关（见上文结论更正）。*

### 验证
- 单测 **171 passed / 0 failed**（原 168 + 新增 3：模型层归一化与拒绝、按账号隔离与稳定性、
  非 ASCII/超长在入站被拦且**零上游调用**）。
- 隔离实例 8899（项目副本 `/tmp/gw26-verify`，pid 1238326；生产 8808 未受影响）真机实测：
  - 中文 `conversation_id` → **422**（此前 502 + 白重试 3 次）、超长 500 字符 → **422**、空白串 → **200**（视作未传）；
  - 两个账号（admin / liunuanshu）用**同一个** `conversation_id` 各调 `kimi-k3` → **都 200**（派生后的值被上游正常接受）；
  - 缓存归属实验（直连上游、真实 key，prompt≈940 token）：见上文**结论更正**的三组数据。
- 文档：README 补「取值限制 / 派生规则 / OpenAI SDK 用 extra_body」+ 缓存结论更正；本 CHANGELOG 记一轮。
- **上线状态**：含 Python 代码改动 → 需 `supervisorctl restart api-gateway:api-gateway_00` 才生效（会踢网页会话），
  **待老板批准**；本轮无前端改动，不需要 `npm run build`。

---

## 2026-09-18 · deepseek-v4.1-flash 视觉能力实测确认（纯配置，hot_reload 生效）

**背景**：老板提示 `deepseek-v4.1-flash`（OpenCode Go）是多模态。上游 `/models` 不提供能力字段（返回只有
`id`/`object`/`created`/`owned_by`），**只能靠真实调用判定**，不能凭文档或模型名猜。

### 实测方法与结果（真实 key）
- **对照组设计**：同一条带图请求（`content=[{type:"text"},{type:"image_url",image_url:{url:"data:image/png;base64,…"}}]`）
  分别打给目标模型和**已确认纯文本**的 `deepseek-v4-flash` —— 后者 **HTTP 400**，前者 **HTTP 200**，区分成立
  （否则"200"可能只是上游忽略图片块，无法证明它真的看了图）。
- **颜色判别（64×64 纯色 PNG，红 `220,30,30` / 蓝 `30,60,220` / 绿 `30,200,80`）**：`deepseek-v4.1-flash`
  **红/蓝/绿三色全部答对**，与已标视觉的 `deepseek-v4-flash-vision-exp` 表现完全一致。
- **过网关复测**（公网 `https://api.wangjinding.com`）：直连 `deepseek-v4.1-flash` 三色全对；路由别名 `wjd`
  带图请求被排到视觉模型、答对「红色」。
- **踩坑（判定方法的坑）**：它是**推理模型**，`max_tokens` 给小了（首次探针用 40）时 token 全被
  `reasoning_content` 吃光、`content` 返回空串——**看起来像"只收图不回答"**。复测放大到 300+ 才拿到答案。
  以后判定视觉能力别用 40 这种量级。

### 改动
- `data/gateway.yaml`：`deepseek-v4.1-flash` 增加 `vision: true`（附实测注释）。
- `README.md`：OpenCode Go 模型清单里该模型标注「（视觉）」，并补进「视觉能力位」说明段。
- **纯配置改动 → hot_reload 15s 自动生效，未重启后端、未踢网页会话**。`/v1/models` 中该模型 `vision`
  由 `false` → `true`（实测确认）；带图请求现在也会在 `wjd` 路线上把该模型视为视觉候选。

---

## 2026-09-18 · 第二十五轮：模型参数硬约束 + 测速超时/错误文案（一轮全修）

**背景（两条线一起收）**：上一轮排查出两个"看起来都坏了"的模型，其实是两回事：
1. **`kimi-k2.7-code` 每次必挂** —— 上游只接受 `temperature=1`，而一键测速探针固定发 `temperature: 0`，于是每次都 400 `invalid temperature: only 1 is allowed for this model`。更严重的是：这不只影响测速 —— **前端对话页的温度滑块默认 0.7**，用户把这个模型拖到任何非 1 的值都会 400。
2. **`mimo-v2.5` 不是坏，是慢** —— 直连 2.2~3s、过网关 2.9~8.4s，偶发冲破探针 15s 超时。但旧文案只回 `str(e)`，两者在前端都显示成含糊的「调用失败」，无从区分。

### 改动
- **`app/config.py`**
  - `ModelInfo` 新增 `params: dict`（模型级**强制参数**）：发往上游前覆盖客户端传值。
  - 新增 `MODEL_PARAM_KEYS` 白名单（`temperature/top_p/top_k/max_tokens/min_tokens/presence_penalty/frequency_penalty/repetition_penalty/seed/logit_bias/n`）。**结构性键（`model`/`messages`/`stream`/`tools`）刻意不放行** —— 放行等于把路由与协议语义交给配置文件，重开一条越权面。
  - 新增 `_parse_model_params()`：非映射、空参数名、白名单外的键、非法值类型一律 `ConfigError`（**写错立刻报错，绝不静默失效**）。
  - `all_models()` 暴露 `params`，供 `/v1/models` 与前端标注。
- **`app/adapters/openai_compat.py`**
  - 新增 `model_params` 入参、`_forced_params(model)`（配置 > 运行期自学习）。
  - `_build_payload()` **末尾**应用强制参数：在 `strip_protected(extra, CHAT_CORE_KEYS)` **之后**覆盖，客户端连同 `extra` 逃生舱都改不动。
  - 新增 `_learn_from_upstream_error()`：上游 400 报文里出现 `invalid <参数>: only <X> is allowed` / `<参数> must be greater than <N>` 时，按上游要求修正并**重发一次**。这不是"重试"——报文照原样重发只会再吃一次 400。学到的值缓存进实例（后续请求直接按修正值组装，不再多一轮往返）；学不到新东西就返回 `None`，绝不与同一约束来回打转；管理员已在配置里写死的参数**不被偷偷改**（值冲突时只打日志说明）。
  - `chat` / `chat_stream` 拆出 `_post_chat` / `_stream_once`，各自在"开局就被 400 顶回"时走一次修正重发；**流式若已开始吐内容则不重发**（避免重复输出/串话）。
- **`app/adapters/__init__.py`**：`create_adapter` 把各模型 `params` 带进适配器 —— 对话 / 流式 / 批量 / 一键测速**所有入口自动一致**，不会出现"主链路修了、测速还漏着"的偏差。配置变更时 `main._apply_config` 丢弃适配器缓存，约束不会滞留旧值。
- **`app/main.py`**
  - `_models_payload()` 在 `/v1/models` 暴露 `params`（该端点自己构造 data 项，不是直接用 `all_models()` 的结果）。
  - 测速探针超时 **15s → 30s**（≈实测最慢值的 3 倍）。该 timeout 只作用于探针自己创建的 adapter（适配器缓存 key 含 timeout），**不影响真实对话链路**的 `server.timeout`。
  - 新增 `_speedtest_error_text()`：超时 → `上游超时（>30s 未响应）`；上游 4xx/5xx → 透出**上游原文**（如 `invalid temperature: only 1 is allowed for this model`）。
- **`app/settings.py`**
  - `view_to_raw()` 的模型项改为「以文件原文为底」再覆盖面板管的 4 个键 —— 面板的模型编辑器不认 `params`，照它重建会**静默清掉**手写约束（管理员只是在页面上改了别的东西，上游硬约束就没了）。
  - `config_to_view()` 回传 `params`，roundtrip 不丢。
- **`data/gateway.yaml`**：`kimi-k2.7-code` 写死 `params: {temperature: 1}`。
- **前端**
  - `Models.vue`：模型行前缀挂锁图标（`title` 说明），模型清单「能力」列新增「参数固定」标签（列宽 120 → 190）。
  - `Chat.vue`：温度 chip 加挂锁；参数抽屉顶部新增说明条「该模型参数由网关固定：… （你在这里调的值会被网关覆盖后再发送）」；模型下拉选项同步挂锁。
  - `Icon.vue` 新增 `lock`；中英文案 `paramTag` / `paramLocked` 对称（726/726）。

### 验证
- **上游全量扫描**：20 个文本模型逐个以 `temperature=0` 探测，**只有 `kimi-k2.7-code` 被拒**（400 `invalid temperature: only 1 is allowed for this model`），改 `temperature=1` 复测 200。→ 约束只此一处，未凭猜测给别的模型加参数。
- **配置层**：非法 `params`（结构性键 / 非法值类型 / 非映射）全部被 `ConfigError` 拦下；真配置解析出 `opencode/kimi-k2.7-code → {'temperature': 1}`；`/v1/models` 暴露一致。
- **适配器层（MockTransport，7 个场景）**：① 配置强制覆盖客户端传值且只发 1 次；② 未配时 400 → 按上游要求修正重发（0 → 1）；③ 学到后后续请求直接带修正值（1 次）；④ 上游一直 400 → 只发 2 次就抛错；⑤ 配置值与上游要求冲突 → 尊重配置、不发第二枪；⑥ 非参数类 400（额度）→ 原样抛错，不误改参数；⑦ 流式开局 400 → 修正后重发成功。
- **隔离实例真机**（`/tmp/gw25-verify/project`，8899，生产 8808 全程未动）：真 key 实测 `kimi-k2.7-code` 测速 **1668ms 成功**、`mimo-v2.5` **4691ms 成功**；主链路显式 `temperature=0` → **200 + 正常内容**（`finish_reason=stop`），流式同样 200。
- **真浏览器**（同一隔离实例）：Models 页 `kimi-k2.7-code` 行出现橙色挂锁、「参数固定」标签、单行测速 **1566 ms**；Chat 页选中该模型后温度 chip 带挂锁、抽屉顶部出现固定说明条。
- **单测**：**168 passed / 0 failed**（+10：参数解析/白名单拦截/真配置锁定/强制覆盖/自动修正重发/不无限重试/配置冲突/流式修正/测速文案/面板保留 params）；前端 `vue-tsc --noEmit` 通过、`check:i18n` 726/726 对称、`check:svg` 通过、`npm run build` 成功（在副本内构建，未覆盖生产 dist）。

### 上线（待老板批准）
- 含 Python 代码改动（`config` / `adapters` / `main` / `settings`）→ 需 `supervisorctl restart api-gateway:api-gateway_00` 才生效（会踢网页会话）。
- 前端需 `npm run build` 覆盖 `frontend/dist`（挂锁/提示条才会出现在线上）。
- CNB 推送新增提交。

**一句话**：给模型加「强制参数」机制并给上游硬约束的 `kimi-k2.7-code` 写死 `temperature: 1`（同时兜一道"上游报什么就改成什么"的自学习重发），把探针超时从 15s 放宽到 30s 并让失败文案说清是超时还是上游拒绝 —— 于是"必挂的模型"变 1.5s 成功、"慢的模型"不再被误判成坏。

---

## 2026-09-18 · OpenCode Go 会话 id 升级为「对话级稳定」（方案 A）

**背景**：上一轮 `x-opencode-session` 是每请求随机 UUID —— 能过上游 400 强校验，但每次 id 都变，吃不到文档所述的提示词缓存。本轮按老板要求实现**稳定会话**：同一段对话共用同一个 session id。
（→ **该动因后来被实测推翻**：第二十六轮的时间对照实验证明缓存与该头无关，稳定会话本身不带来缓存收益；保留本节点只为记录当时的判断依据。）

### 改动
- **`app/models.py`**：`ChatRequest` 显式声明 `conversation_id: str | None`（网关内部字段，不进 `model_extra` → **不透传上游**）。
- **`app/adapters/openai_compat.py`**：`_req_headers(session_id)`、`chat`/`chat_stream` 新增 `session_id` 参数——传入则原样用作 `x-opencode-session`，缺省回退随机 UUID。
- **`app/main.py`**：非流式与流式主链路把 `req.conversation_id` 作为 `session_id` 传入适配器。
- **`frontend/src/views/Chat.vue`**：对话页新增 `convId`（`crypto.randomUUID`，带老浏览器回退），`buildPayload` 注入 `conversation_id`，随对话状态持久化（sessionStorage + 服务端历史）；`applyChat` 沿用历史里的 id、否则新建；`clearConversation` 生成新 id（新对话不共享旧缓存）。
- **`tests/test_gateway.py`**：新增 `test_chat_conversation_id_is_internal_not_upstream`、`test_opencode_session_header_from_conversation_id`。

### 验证
- 单测 **157 passed / 0 failed**（+2）；前端 `vue-tsc --noEmit` 通过。
- 真实 key 冒烟：同一 `conversation_id` 两次调用均 200，`x-opencode-session` 稳定一致；缺省回退 32 位随机、两次不同；非会话供应商不带该头。
- 说明：缓存命中是上游机会性优化，网关侧保证的是「同一对话稳定 session」。

### 上线（待老板批准）
- 含后端代码改动 → 需 `supervisorctl restart`；前端 `Settings.vue`/`Chat.vue` 改动需 `npm run build` 覆盖 `dist`；CNB 推送待确认。

**一句话**：给网关接入 OpenCode Go（`https://opencode.ai/zen/go/v1`）的 OpenAI 兼容 `/chat/completions` 线 17 个文本模型；按老板「wjd = 全部文本模型」规则全部并入 `routes.wjd`。

### 背景（文档要点）
- OpenCode Go 是每月订阅制编程模型入口；**每个请求必须带 `x-opencode-session` 头**（实测缺它接口 400 `MissingSessionID`），建议专属 `User-Agent`（实测 UA 不敏感，session 头才是硬性要求）。
- 模型分三条契约线：`/chat/completions`（OpenAI，本网关可直连）、`/messages`（Anthropic MiniMax/Qwen 等）、`/responses`（Grok/GPT/Muse）。仅接入 OpenAI 兼容线；其余两条不在本次范围。

### 改动
- **`data/.env`**：新增 `OPENCODE_API_KEY`（600、gitignored）。
- **`data/gateway.yaml`**：
  - 新增 `providers.opencode`：`base_url https://opencode.ai/zen/go/v1`、`token_env OPENCODE_API_KEY`、`requires_session_header: true`、`extra_headers.User-Agent: api-gateway/2.4.0 (jakon opencode-go)`，收录 **17 个**文本模型（`glm-5.3-flash/5.3/5.2/5.1`、`kimi-k3/k2.7-code/k2.6`、`longcat-2.0`、`deepseek-v4-pro/v4-flash/flash/v4.1-flash/v4-flash-vision-exp(视觉)`、`mimo-v2.5/v2.5-pro`、`hy4-preview/hy3`）。
  - `routes.wjd` 追加这 17 个（wjd 链 = 20 个文本模型）。
  - 未收录：`glm-5`、`kimi-k2.5`、`hy3-preview`（上游实测 400 Model is unavailable）；`/messages`、`/responses` 线模型未接入（适配器不含该契约）。
- **`app/config.py`**：`ProviderConfig` 加 `requires_session_header` 字段并解析。
- **`app/adapters/openai_compat.py`**：`OpenAICompatAdapter` 加 `requires_session_header`，在**每次请求**动态注入 `x-opencode-session: <uuid>`（不能放静态 headers——`_get_adapter` 缓存会冻结）。
- **`app/adapters/__init__.py`**：`create_adapter` 透传开关。
- **`app/settings.py`**：面板视图与写回均增补 `requires_session_header`（否则 roundtrip 后该字段被重置为 False）。
- **`frontend/src/views/Settings.vue`**：默认模型下拉 `TEXT_MODELS` 追加 17 个 go 模型（老板「全部文本模型」）、保留 `wjd` 在最前。
- **`tests/test_gateway.py`**：
  - `test_load_config_providers` 断言 `{"agnes","volc_engine","opencode"}`；`test_load_config_routes` wjd 长度 3→20。
  - **修复既有污染**：`test_effective_rps_priority` 直接用共享 `m._cfg()` 把 `agnes.rate_limit_rps` 改成 9 且未还原，新增 go 模型后触发限流桶被填满、令 `chat_unknown / batch / streaming fallback` 三个测试在全套运行时假红——已加 `finally` 还原（这本质是测试手尾泄漏，非业务 bug）。

### 验证
- 文档端点实测：`GET /zen/go/v1/models` 通，返回 38 个模型（原生 id、无前缀）。
- 契约实测：17 个 chat/completions 模型真实 key 全部 200；`glm-5` / `kimi-k2.5` / `hy3-preview` 400 未收录。
- 头敏感性实测：带 `x-opencode-session` 必 200，缺它必 400（UA 无关）。
- 适配器端到端（真实 key）：非流式 `glm-5.3-flash` 与流式 `deepseek-v4-flash` 均正常返回（证明会话头注入生效）。
- 单测：**155 passed / 0 failed**；前端 `vue-tsc --noEmit` 通过。

### 上线（待老板批准）
- 含 Python 代码改动（config / adapters / settings）→ 需 `supervisorctl restart api-gateway:api-gateway_00` 才生效（会踢网页会话）；纯 yaml 的 hot_reload 只重载供应商/模型定义，`requires_session_header` 字段与注入逻辑要重启后代码才认。
- 前端 build 覆盖 `dist`（纯前端上线动作）与 CNB 推送待老板确认。

**一句话**：老板拍板——**现有 + 以后新增的每一个文本模型都必须加入 `routes.wjd` 兜底链**。本次把 `agnes-2.5-flash` 补进 wjd，并把这句作为长期规则写入 MEMORY / README / CHANGELOG，供后续新增模型照做。

### 改动
- **`data/gateway.yaml`**：`routes.wjd.models` 补第 3 位 `agnes-2.5-flash`，链改为
  `ark-code-latest` → `agnes-3.0-flash` → `agnes-2.5-flash`；加上注释「wjd 链路 = 全部文本模型（新增/现有都必须加入）」。
- **`README.md`**：wjd yaml 示例链同步为 3 模型 + 同一句注释。
- **`tests/test_gateway.py`**：`test_load_config_routes` 断言 `len(wjd.models) == 2` → `3`。
- **`MEMORY.md` / `memory/2026-09-18/wjd-route-rule.md`**：记录决策为长期约束（新增文本模型必须同时进 `routes.wjd.models`）。

### 验证
- 静态校验：`data/gateway.yaml` 中全部 `type: text` 模型 = `agnes-2.5-flash / agnes-3.0-flash / ark-code-latest`，与 `routes.wjd.models` 完全一致（无缺失、wjd 无多出的非文本模型）。
- 单测：`python3 tests/test_gateway.py` → **155 passed, 0 failed**。
- 运行时排序不受影响：wjd 仍按「可用性 + 速度」实时重排，配置序只是冷启动初始序；`timeout:60 / retry:1` 不变。

### 上线
- 纯配置改动，hot_reload ≤15s 自动生效，无需重启、不踢会话；是否推 CNB 等老板确认。

**一句话**：给 Agnes 供应商注册新模型 `agnes-2.5-flash`（2.0 升级版，OpenAI 兼容 Chat Completions，支持图像 URL 输入）。

### 改动
- **`data/gateway.yaml`**：`providers.agnes.models` 新增：
  - `agnes-2.5-flash`（text，`vision: true`，注释注明：2.0 升级版、OpenAI 兼容 `/v1/chat/completions`、支持图像 URL 输入与工具调用、上下文 512K / 输出 65.5K、现价暂免）。
  - `pricing.per_model` 新增 `agnes-2.5-flash: { input: 0, output: 0 }`（文档现价输入/输出/缓存命中均 ¥0/百万）。
- **`frontend/src/views/Settings.vue`**：`TEXT_MODELS`（对话默认模型下拉）插入 `agnes-2.5-flash`（`['wjd','agnes-3.0-flash','agnes-2.5-flash','ark-code-latest']`）。
- **`README.md`**：供应商表 Agnes 行补 `agnes-2.5-flash`（text，支持图像输入）。

### 实测（连真实 AGNES_API_KEY，经生产适配器直调）
- 文本 `agnes-2.5-flash` → 200 "正常" ✅
- 视觉（200×200 红图 data:URI）→ 200 "红色" ✅（vision 标记确认正确）

### 上线
- 纯配置（gateway.yaml）`hot_reload` ≤15s 自动生效、**免重启**；前端 `npm run build` 覆盖 dist（新入口 `index-BcM3V8if.js`），`/web/` 静态服务自动服务新产物。
- 单测 **155 passed / 0 failed**（新增模型未破坏现有断言，`models[0]` 仍为 `agnes-3.0-flash`）。

### 说明
- **未加入 `routes.wjd` 兜底链**（boss 未要求）——如需让它在 wjd 路由里兜底，加进 `routes.wjd.models` 即可。
- `agnes-3.0-flash` 仍是 `TOKEN_MODELS` 里第 2 顺位（紧随 wjd）。

---

## 2026-09-17 · 移除智谱 GLM（bigmodel）供应商

**一句话**：老板要求删除智谱供应商，代码/配置/文档/测试/前端全清——覆盖上上轮新增的 6 个 GLM 文本模型（含 3 视觉）、CogView-3-Flash 文生图、CogVideoX-Flash 文生视频及配套的媒体契约代码。

### 改动清单（除 CHANGELOG 历史条目外全部清零）
- **`data/gateway.yaml`**：删 `providers.bigmodel` 整块（`GLM-4.7-Flash`/`GLM-4-Flash-250414`/`GLM-Z1-Flash`/`GLM-4.6V-Flash`/`GLM-4V-Flash`/`GLM-4.1V-Thinking-Flash` 6 文本 + `cogview-3-flash` 图像 + `cogvideox-flash` 视频 + 整套 media 配置）；`download.allowed_hosts` 删智谱 CDN 项（`maas-watermark-prod-new.cn-wlcb.ufileos.com`）；头注释供应商列表 → `Agnes / 火山方舟 Coding`。`routes.wjd`、`pricing` 本就不含智谱，无需动。
- **`data/.env`**：删 `ZHIPU_API_KEY`。
- **`app/config.py`**：`MediaConfig` 删 `video_style` / `video_default_size` 两个字段及其解析（智谱视频契约专用）。
- **`app/main.py`**：`_ZHIPU_VIDEO_SIZES` 常量删；`_validate_video_request` 删 zhipu 分支、恢复单参数 `(req)`、docstring 去"接入其它视频供应商"；`videos_create` 恢复"先入站校验后解析"原顺序（去掉按供应商媒体契约分流）；`_video_status` 改通用描述（去智谱词，状态优先级 status/state/task_status）；`description` → `（Agnes / 火山方舟）`。
- **`app/settings.py`**：面板 `MEDIA_KEYS` 删 `video_style` / `video_default_size`。
- **`app/adapters/{__init__,openai_compat}.py`**：docstring「三家」→「两家」；`create_video` / `poll_video` 删 zhipu 分支（恢复纯 Agnes 风格）；删 `urllib.parse.quote` 导入。
- **前端**：`Settings.vue` `TEXT_MODELS` 去 6 个 GLM；`Videos.vue` 删智谱视频专用表单/状态/`watch`/create 分支，恢复 Agnes 表单；`Images.vue` 删 cogview 像素档/`isZhipu`/参考图与 ratio 的 `v-if`；`locales/{zh,en}.ts` 删 `videos.z*`（7 键/语言）；`media.ts` `findVideoUrl` 优先键去 `video_result`。
- **`README.md`** / **`docs/frontend-design.md`**：供应商改两家，删智谱供应商表行、`.env` 说明去 `ZHIPU_API_KEY`。
- **`tests/test_gateway.py`**：`test_load_config_providers` 账号集合→`{agnes,volc_engine}`、删智谱媒体断言；速度排序测试用 `[ark-code-latest,agnes-3.0-flash]` 两模型演示（删 GLM）；`test_vision_first` 用 `agnes-3.0-flash`（vision）当视觉样例；媒体路由别名测试/配置写回测试/视频轮询测试改用真实模型或恢复原样。

### 验证 / 上线
- 单测 **155 passed / 0 failed**；全项目 grep `智谱|bigmodel|GLM|cogview|cogvideox|ufileos|ZHIPU|zhipu|video_style|_ZHIPU` 除 CHANGELOG 历史外**全部清零**。
- 前端 `npm run build` 过（typecheck + i18n 对称），新入口 `index-BynIw5Ue.js`。
- 配置校验：providers `[agnes,volc_engine]`、模型 `agnes[3] + volc[1]`、`routes.wjd` 2 项、下载白名单仅 Agnes。
- `supervisorctl restart api-gateway:api-gateway_00` 上线（`description`/适配器/校验等属代码改动，需重启）。

---

## 2026-09-17 · 移除 b.ai 供应商

**一句话**：老板要求删除 b.ai 供应商及其两个模型（`qwen3.8-flash`、`hy3`），代码、配置、文档、测试全清。

### 改动清单
- **`data/gateway.yaml`**：删 `providers.b_ai` 整块（含 `qwen3.8-flash`、`hy3` 两个模型）；`routes.wjd` 兜底链由 4 项（ark-code/agnes/qwen3.8/hy3）精简为 2 项（`ark-code-latest` → `agnes-3.0-flash`）；`pricing.per_model` 删 `qwen3.8-flash`/`hy3` 两条；头部供应商注释 `Agnes / b.ai / 火山方舟 / 智谱 GLM` → `Agnes / 火山方舟 / 智谱 GLM`。
- **`data/.env`**：删 `B_AI_API_KEY=sk-nf...`（纯删除，密钥不再使用）。
- **`app/main.py`**：`description` 字符串去 b.ai；测速探针注释去掉 `b.ai(qwen3.8-flash/hy3)` 引用。
- **`app/config.py`**：`ModelInfo.vision` 注释去掉 `b.ai 系`。
- **`app/adapters/{__init__,openai_compat}.py`**：docstring「四家」→「三家」，供应商列表去 b.ai。
- **`README.md`**：头部「四个供应商」→「三个」；供应商表删 b.ai 行；路由示例去 `qwen3.8-flash`/`hy3` 并去其重编号；pricing 示例去 b.ai 两条；`# 编辑 .env 填入` 行去 `B_AI_API_KEY`。
- **`frontend/src/views/Settings.vue`**：`TEXT_MODELS` 去 `qwen3.8-flash`。
- **`docs/frontend-design.md`**：供应商列表去 b.ai。
- **`tests/test_gateway.py`**：`test_load_config_providers` 集合去 `b_ai`；`test_load_config_routes` wjd 长度 4→2；`test_wjd_route_orders_by_availability_and_speed` 用 `GLM-4.7-Flash` 替代 `qwen3.8-flash` 做速度排序演示，env var 清单改 `ZHIPU_API_KEY`；`test_vision_first_reorders_only_when_vision_exists` 改用非视觉模型 `GLM-Z1-Flash`（原 `hy3`）；`test_config_endpoints_permission_matrix` 及路由写回测试中的 `hy3` 改 `GLM-4.1V-Thinking-Flash`；媒体路由测试的 `qwen3.8-flash` 改 `GLM-4.1V-Thinking-Flash`。

### 验证
- 单测 **155 passed / 0 failed**（修正 6 处断言后全绿）。
- 全项目 grep `b_ai|api.b.ai|B_AI_API_KEY|qwen3.8-flash|hy3` 命中仅为 CHANGELOG 历史条目（保留）—— 其余全部清零。
- 纯配置+文档改动，`hot_reload` 自动生效；若重启，`/v1/models` 由 14 → 12（`agnes` 3 + `volc_engine` 1 + `bigmodel` 8），`routes.wjd` 由 4 → 2。

---

## 2026-09-16 · 集成智谱 CogView-3-Flash（文生图）+ CogVideoX-Flash（文生视频）

**一句话**：给上一轮的智谱供应商补上媒体生成——图像页用 `cogview-3-flash`、视频页用 `cogvideox-flash`，前后端按「选到的供应商」自动切到智谱契约（不动 Agnes）。

### 契约（官网 + 真 Key 实测确认）
- **图像 cogview-3-flash**：`POST {base}/images/generations`，`{model, prompt, size}`（size 为像素档：1024x1024/768x1344/864x1152/1344x768/1152x864/1440x720/720x1440）→ 返回 `data[].url`；**无 ratio、不支持图生图**。
- **视频 cogvideox-flash**：`POST {base}/videos/generations`（**异步**）`{model, prompt, quality, with_audio, size, fps:30/60}` → 返回 `task_status:PROCESSING`；再 `GET /async-result/{id}` 轮询到 `SUCCESS` 取 `video_result[].url`。仅文生视频。

### 后端
- `data/gateway.yaml` bigmodel 加 `media`（图像端点+像素档、视频 `/videos/generations`+`/async-result/{id}`，新增 `video_style:"zhipu"`），并注册 `cogview-3-flash`(image)、`cogvideox-flash`(video)（顺带修复本轮早前编辑误吞的 `server:` 顶层键）。
- `config.py`：`MediaConfig` 新增 `video_style` / `video_default_size`。
- 适配器 `create_video` / `poll_video`：按 `video_style=="zhipu"` 走智谱异步契约（fps/quality/with_audio + 路径参数轮询）；Agnes 分支不变（`__init__.py` docstring 改四家）。
- `main.py`：新增 `_video_status`（兼容 `task_status`，智谱才识得 `SUCCESS/FAIL` 终态）与 `_video_terminal`，替换 3 处视频状态判定；`_validate_video_request` 按供应商分流（智谱仅 text 文生视频，size/quality/fps 合规校验，非法回干净 400 不进熔断）；`videos_create` 改为先 `_resolve_media_model` 再按供应商校验。
- `settings.py`：面板 `MEDIA_KEYS` 补 `video_style`/`video_default_size`（否则「网关配置」面板往返会丢这两个字段）。
- 顺带修正 `DELETE /feedback` 过时 docstring（admin 可真删 admin_delete）。

### 前端（按所选模型 `provider==='bigmodel'` 自动切）
- `Videos.vue`：选智谱视频模型显示专用表单（分辨率/质量 speed·quality/帧率 30·60/配音 switch，固定文生视频），body 用 `extra:{quality,fps,with_audio}`；选 Agnes 原表单不变；切智谱模型时强制 `mode='text'`。
- `Images.vue`：选 cogview 时 size 换成像素档、隐藏 ratio 与参考图（cogview 无图生图）。
- `media.ts`：`findVideoUrl` 优先键补 `video_result`。
- i18n：新增 `videos.zSize/zQuality/zSpeed/zQualityHigh/zFps/zAudio/zTip` 7 键，中英对称。

### 验证
- 单测：新增/修正 bigmodel 媒体契约断言、视频轮询必经 `?model=`（现两家视频供应商）、配置往返含新 media 键 → **155 passed / 0 failed**；前端 `npm run build` 过（typecheck + i18n 对称），新入口 `index-CxdHy_Sw.js`。
- 真机隔离（uvicorn:8899 临时 users/history，连真实智谱）：`/v1/models` 列出 8 个智谱模型；图像 → 200 `data[].url`；视频创建 → `video_id`+`task_status:PROCESSING`；轮询 → `status:processing`；非法参数（`mode=keyframe`）→ 400 干净拒绝。
- 上线：`supervisorctl restart api-gateway:api-gateway_00`（代码改动需重启），前端 dist 已 build 覆盖。
- **补「下载白名单」**：上线后老板实测智谱图/视频下载报「不在白名单：maas-watermark-prod-new.cn-wlcb.ufileos.com」。已确认智谱图+视频产物**同一 CDN 域名**（实测视频 `SUCCESS` 结果 url 同域），在 `download.allowed_hosts` 补 `maas-watermark-prod-new.cn-wlcb.ufileos.com`（精确项 + 子域匹配，不泛放整个 `ufileos.com`；`evilufileos.com`、后缀撞车域名仍拒绝）。纯配置改动，`hot_reload` ≤15s 自动生效免重启。已用隔离服务器实测：智谱图经 `/v1/media/download` 拉回 200 + 有效 JPEG 97KB。

---

## 2026-09-16 · 新增智谱 GLM（open.bigmodel.cn）供应商

**一句话**：接入智谱开放平台作为新的 OpenAI 兼容供应商，收录 6 个 Flash 模型（3 文本 + 3 视觉），密钥走 `data/.env` 的 `ZHIPU_API_KEY`，真图实测确认 3 个 V 型号支持图像输入。

### 供应商接入
- `data/gateway.yaml` 新增 `providers.bigmodel`：
  - `base_url: https://open.bigmodel.cn/api/paas/v4`（适配器自动拼 `/chat/completions`）
  - `auth.token_env: ZHIPU_API_KEY`（明文 key 不进 yaml，统一放 `data/.env`，chmod 600）
  - 媒体端点 `media: {}`（智谱无图像/视频生成端点）
  - 模型 6 个：`GLM-4.7-Flash`、`GLM-4-Flash-250414`、`GLM-Z1-Flash`、`GLM-4.6V-Flash`、`GLM-4V-Flash`、`GLM-4.1V-Thinking-Flash`，其中 3 个 V 型号标 `vision: true`
- `data/.env` 追加 `ZHIPU_API_KEY=<智谱 Key>`（49 位，未入版本库）。
- 前端 `Settings.vue` 的「对话默认模型」静态清单 `TEXT_MODELS` 追加这 6 个模型；Models 页 / `/v1/models` 本身是动态的，重启后自动出现。

### 排查与实测（重点）
- 首把 Key `cdfb6b5e...` 直接裸调 6 模型一度全 **401「令牌已过期」** → 误判 Key 无效。后用官方示例格式 + `glm-5.3` 重打是 **429「余额不足」**（说明 Key 认证是通的）→ 回到 6 模型官方格式再打：5 个 200，`GLM-4.7-Flash` 偶发 **429「访问量过大」(1305，瞬时过载，模型存在)**。**结论：Key 有效，之前 401 是上游瞬时过载造成的误报。**
- 视觉探针（1×1 红图）被智谱拒 **400 code 1210「图片输入格式/解析错误」** —— 是**图太小**的问题，非格式问题。换 200×200 标准红图后 data-URI 与外链 URL 均识别：`GLM-4V-Flash`、`GLM-4.1V-Thinking-Flash` 认「红色」✅；`GLM-4.6V-Flash` 全程遇上游过载(1305)未取到视觉结果，型号带 V 故仍标 `vision: true`（如后续实测不一致可再改）。
- 端到端：用生产同一适配器 `OpenAICompatAdapter`（真实 `base_url` + `Bearer` 解析 token）直连智谱——文本 `GLM-4-Flash-250414` ✅、推理 `GLM-Z1-Flash` ✅（返回含 thinking）、视觉 `GLM-4V-Flash`（data-URI 图）✅ 全部 HTTP 200。

### 上线
- 配置层 `hot_reload` 会自动热更 gateway.yaml；但 **ZHIPU_API_KEY 是新增环境变量，需重启进程才加载** → 待老板批准后重启，`/v1/models` 由 6 → 12 个文本模型、Models 页自动多出智谱 6 个。
- 单测：修正 `test_load_config_providers`（供应商集合加入 `bigmodel` + 智谱 6 模型/V 标记断言）；**一并修掉此前遗留的 `test_feedback_withdraw_only_when_pending`**（其旧断言「管理员 DELETE 反馈→403」没跟上「admin 收件箱可真删 admin_delete」的功能演进，已改为：普通用户撤别人→403、管理员真删→200 且图片文件一并清；并同步修正 DELETE 端点过时的 docstring）。最终 **155 passed / 0 failed**。

---

## 2026-09-15 · 新增 OpenCode Zen（免费档）供应商

**一句话**：把 OpenCode Zen 作为第 4 个供应商接入，只收录文档定价页列出的**免费模型**。打通后发现免费档比较刁钻：既被 Cloudflare 拦默认 UA，又强制要求每个请求带独立会话头——两条都已在适配器层解决，实测全部 200。

### 背景 / 需求
老板要求查看 [OpenCode Zen 文档](https://opencode.ai/docs/zh-cn/zen#pricing)，接入该供应商，**模型只要文档里免费的模型**，并提供一把 API Key。

### 上游真相（重点！）
文档定价页的"免费模型"共有 6 个。**直接以"Bearer key + 标准请求"裸调全部失败**，逐一实测定位（带浏览器 UA 后才能看到模型层错误）：

| 免费模型 id | 裸调结果 | 根因 | 处理 |
|---|---|---|---|
| `big-pickle` | 401 `Model is disabled` | 隐身模型，未对本工作区/Key 开放 | ❌ 排除 |
| `mimo-v2.5-free` | 400 `MissingSessionID` | 免费档要求每个请求带会话 id | ✅ 收录 |
| `ling-3.0-flash-fin-free` | 400 `MissingSessionID` | 同上 | ✅ 收录 |
| `nemotron-3-ultra-free` | 400 `MissingSessionID` | 同上 | ✅ 收录 |
| `nemotron-3.5-lightning-free` | 400 `MissingSessionID` | 同上 | ✅ 收录 |
| `muse-spark-1.3-contributor-free` | 400 `MissingSessionID` | 且走 `/responses` 端点（本适配器是 `chat/completions` 调不了）+ contributor 档会用你的提示词/补全训练 Meta 模型 | ❌ 排除 |

**根因一（Cloudflare）**：裸调用默认 `urllib` UA 报 `403 error code: 1010`（基于客户端签名的访问被禁）。改成浏览器 UA 后请求才抵达模型层。网关默认 `python-httpx` UA 大概率同样被拦 → 必须在请求头带浏览器 `User-Agent`。

**根因二（会话标识）**：通过 [DeepSeek Harness 生产集成讨论帖](https://github.com/deepseek-ai/deepseek-harness/discussions/5495) 确认——所有打到 `opencode.ai` 的请求都要带 **`x-opencode-session`** 请求头（会话 id，UUID 即可；同一次多轮保持稳定会走路由亲和性）。不带 → `400 MissingSessionID`。带上后 4 个免费模型全部可用。

**Key 与计费**：该 Key 有效（能通过鉴权、能识别到 workspace），但账号**未绑定支付方式** → 付费模型返回 `CreditsError: No payment method`。因此当前这套 Key 下能用的只有免费档（且经会话头 + 浏览器 UA 后确实能用）。

### 改动明细
- **`app/adapters/openai_compat.py`**（后端代码改动，需重启生效）
  - 新增常量 `OPENCODE_SESSION_SENTINEL = "@per-request-uuid@"`。
  - 新增 `_call_headers()`：若 `extra_headers` 里 `x-opencode-session` 为哨兵值，则**每次 `chat` / `chat_stream` 调用现生成一个新 UUID** 再发请求；否则原样返回静态头（其它供应商零行为变化）。
  - 目的：所有用户/会话共用一个固定 session 会互相踩 OpenCode 的限流与路由亲和，必须按请求/会话隔离。
  - `chat()` 与 `chat_stream()` 的请求头由 `self._headers` 改为 `self._call_headers()`。
- **`data/gateway.yaml`**
  - 新增 `opencode` 供应商：`name: OpenCode Zen`、`base_url: https://opencode.ai/zen/v1`、`token_env: OPENCODE_API_KEY`。
  - `extra_headers`：`User-Agent`（浏览器 UA，过 Cloudflare）+ `x-opencode-session: "@per-request-uuid@"`（哨兵）。
  - `media: {}`（仅文本，无图像/视频）。
  - `models`：4 个免费模型（`mimo-v2.5-free` / `ling-3.0-flash-fin-free` / `nemotron-3-ultra-free` / `nemotron-3.5-lightning-free`）。
  - `pricing.per_model`：新增这 4 个，`input: 0, output: 0`（免费）。
  - 顶部注释供应商清单补 `OpenCode Zen（免费档）`。
- **`data/.env`**：新增 `OPENCODE_API_KEY=<sk-...>`，权限 0600（保留原行、仅追加）。key 明文不进 yaml、不入版本库。
- **`tests/test_gateway.py`**：`test_load_config_providers` 断言更新为 4 家，并新增守卫断言（opencode.base_url / 浏览器 UA / 哨兵头 / 4 个模型集），防以后误改。
- **`README.md`**：顶部供应商清单、供应商表格、`openai_compat.py` 架构注释、OpenCode 两条硬性要求说明。

### 验证（非编造，实录）
- 上游直打 `https://opencode.ai/zen/v1/chat/completions`（**浏览器 UA + 每请求新 UUID 会话头**）：
  - 4 个免费模型**非流式全部 200**，`nemotron-3.5-lightning-free` 出真实中文回复 + `usage.reasoning_tokens`（推理模型先吐推理再吐内容）。
  - **流式全部 200**：`content-type: text/event-stream`，`nemotron-3.5-lightning-free` 收了 123 个 chunk 并正常收到 `[DONE]`。
  - `big-pickle` 反复 401 `Model is disabled` → 确认本 Key 未开放，排除。
- 适配器哨兵逻辑单测：哨兵值 → 每次新 UUID（36 位）；普通静态值 → 原样保留。
- 全量单测 `python tests/test_gateway.py`：**153 passed, 2 failed**。
  - 涉及的 provider 相关用例通过；余下 2 个失败为历史遗留、与本次无关：
    - `test_load_config_routes`：断言 `wjd.models == 6`，但 GLM-5.3-Flash 移除后 `wjd` 链是 5 个（旧断言未同步）。
    - `test_feedback_withdraw_only_when_pending`：feedback 撤回逻辑，非本次改动范围。

### 部署
- `supervisorctl restart api-gateway:api-gateway_00`。
- 重启前验证文件已就位：yaml `safe_load` 通过、`providers = [agnes, b_ai, volc_engine, opencode]`、opencode 4 个模型。
- 上线后校验：新 pid 61457、healthz `{"status":"ok"}`、`/v1/models` 200 且含 opencode 4 个免费模型、无凭证/错 key 仍 401、真实 chat 调用 `provider=opencode` 返回 200。

### 待办 / 已知
- `big-pickle` 与付费模型都因账号条件（Model disabled / 未绑支付方式）不可用，等以后条件变化再按需补录。
- 免费档是否偶尔有速率/额度上限：OpenCode 注明"目前免费提供，团队在收集反馈"，可能随时变化；若某模型开始报错，在 Models 页一键测速即可发现。
- 本次未把 OpenCode 加进 `wjd` 兜底路由，先在 **Models 页 + 直连模型 id** 可用；要进 `wjd` 需老板另行确认。

### 视觉支持实测（2026-09-15，真图判定）
发一张「青色背景 + 品红五角星」的答案唯一测试图，走 OpenAI 多模态 `image_url` 格式（带浏览器 UA + `x-opencode-session`）实测：
- `ling-3.0-flash-fin-free` → **❌ 无视觉**：上游 `400 ... [404] No endpoints found that support image input`。
- `nemotron-3-ultra-free` → **❌ 无视觉**：同上。
- `nemotron-3.5-lightning-free` → **❌ 无视觉**：同上。
- `mimo-v2.5-free` → **⚠️ 未判定**：连纯文本都持续 `429 FreeUsageLimitError`（免费额度是小时/天级窗口，等 7 分钟未回血），够不到读图那步。强先验：MiMo-V2.5 家族在 b.ai 早前实测即 `Vision is disabled`，故基本可断定无视觉。
- 结论：4 个 OpenCode 免费模型**均无视觉**（3 个实测坐实、1 个受限于限流未坐实但先验一致）。gateway.yaml 里这 4 个都只标 `type: text`、**未加 `vision:true`**，与实测一致，无需改动；Models 页也不会给它们显示 👁 图标。

---

## 2026-09-15 · 一键测速报错展示优化（Models 页）

**问题**：Models 页「一键测速」某个模型失败时，错误内容被塞进硬编码的窄 `pill`（`.st-lat`）里，长错误（无空格 JSON / 上游错误串）不换行、到卡片边缘就被切掉 → 报错显示不全。

**改动**（`frontend/src/views/Models.vue`，纯前端，不重启后端）：
- 模板：每个测速行 `st-row` 外包一层 `st-item`；当某模型测速失败时，在行下方新增一整行 `st-err`（`ban` 图标 + 完整错误文本）全宽展示。
- 逻辑：新增 `latErr(id)` 返回完整错误串（用于 st-err 行）；`latText(id)` 失败时只返回短的「失败」标记（长错误不再塞窄 pill）。
- 样式：`.st-err` 用 `--err` 文字 + `--err-soft` 底、圆角块；`overflow-wrap: anywhere` + `word-break: break-word` 让长 JSON/无空格错误串自动换行不溢出；`.st-item + .st-item` 虚线分隔。

**验证**：`npm run check:svg && npm run check:i18n && vite build` 通过；`vue-tsc --noEmit` 无错；新产物 `index-C_e1f2ok.js` / `Models-C-QAxhv1.js` 线上 200、旧 hash 404。后端挂载 `frontend/dist`，build 即可见，pid 未动、会话不掉。

### 附带修复：后端空错误兜底
真实实测中发现 nemotron-3-ultra-free / mimo-v2.5-free 偶尔 429 限流，部分时候上游只返回空 body 导致 `error = "{}"`（`str({})`），既丑又无信息。`app/main.py` 的 `_upstream_detail` 修复：当 `j` 无 `message/error/detail` 且 `str(j) in ("{}","","null","None")` 时返回空串，前端 `row.error` 兜底显示「调用失败」而不是 `{}`。重启后生效（`pid 121772`）。

### 截图留档
- `docs/st-err-speedtest-error-display.png`：真实浏览器实拍，两个 OpenCode 免费模型同时报错，`st-err` 红块完整展示 429 限流错误信息（含上游文档链接）——验证"显示不全"已修复。

---

## 2026-09-15 · 一键测速改为「逐个测、测一个显示一个」

**需求**：一键测速原来是并发/一次性返回全部结果、最后一起显示；改成**串行逐个测，每测完一个立刻显示那一行，再测下一个**。

**改动**
- **`app/main.py` `/v1/models/speedtest`**：新增可选 `body.model`。传了就只测该模型、返回单条（不排序）；乱传/非文本模型返回一条兜底错误行「模型不存在或非文本模型」。不传仍是一次性全测（保留兼容）。
- **`frontend/src/views/Models.vue`**：`runSpeedtest()` 改为按列表顺序 `for` 循环，逐个 `POST {model:id}`，每测完一个 `latByModel.value = {...latByModel.value, [id]: row}` 响应式即时刷新该行；**单个模型异常不中断整轮**（catch 后标失败继续下一个）。新增 `latDone/latTotal`，按钮测速中显示「测速中 n/total」进度。`latText()` 改为：已测行显示 ms/失败、测速中未轮到的显示 `…`、空闲显示 `—`。
  - 注：`textModelRows` 本就按供应商分组顺序、不随延迟重排，所以逐个测时行不会跳动，无需额外锁序。
- **i18n**：`models.testingProgress` 中英各加一条（`测速中 {n}/{total}` / `Testing {n}/{total}`），保持对称。

**为什么串行更好**：并发一次性测会把 OpenCode 免费档瞬间打满触发 429；串行逐个测天然错峰。

**验证**：后端单测 153 passed（2 个历史遗留不变）；前端 build + `vue-tsc` 通过；新产物 `index-D81T1xmr.js` / `Models-DpOHvPHF.js` 线上 200、旧 hash 404；`supervisorctl restart` → pid 187860、healthz ok。接口实测 `{model:"hy3"}` 只返回 1 条、乱传 model 有兜底错误行。浏览器实拍：点测速后按钮「测速中 1/9」、第一行已出 `748 ms`、其余 8 行 `…` 排队，33s 全部完成——逐个填充生效。
- 截图留档：`docs/speedtest-sequential-progress.png`

### 追加：按钮去数字 + 当前行加载图标
老板反馈测速按钮不要出现 `n/total` 数字，改成「轮到哪个模型、就在那一行显示加载图标」。
- 按钮：`runSpeedtest` 期间文案改 `t('models.testingShort')`（「测速中…」），去掉 `latDone/latTotal` 数字；`el-button :loading` 自带转圈。
- 当前行：新增 `latCurrentId`，循环里每测一个置为该 id、整轮结束清空；模板延迟格里 `v-if="m.id===latCurrentId"` 显示 CSS spinner（`.st-spin` 圆环旋转，`prefers-reduced-motion` 降速），否则显 `latText`。`latText` 未轮到/空闲统一 `—`（去掉原 `…`）。
- i18n：删 `testingProgress`、加 `testingShort`（中英）。
- 纯前端，build 覆盖 dist 即生效、未重启后端。新产物 `index-CZxVI3oL.js` / `Models-C-o3SZgZ.js`，线上 200、旧 hash 404。
- 浏览器实拍：按钮「测速中…」无数字、`agnes` 已出 `929 ms`、`qwen3.8-flash` 行内转圈、其余 `—`。截图 `docs/speedtest-current-row-spinner.png`。

### 追加：失败行「重新测速」按钮
老板要失败的模型旁边可点击单独重测。
- `Models.vue` 新增 `retestOne(id)`：只测这一个模型（`POST {model:id}`），复用当前行加载图标；`latTesting || latCurrentId` 时禁用/忽略，避免与整轮或另一单测打架；单模型异常也不抛、写回失败行。
- 模板：`st-err` 错误块右侧加 `.st-retry` 胶囊按钮（`refresh` 图标 + 「重新测速」），`v-if="latErr(m.id)"` 只在失败行出现；错误文本用 `.st-err-txt{flex:1}` 占满、按钮靠右。
- i18n：`models.retry`（`重新测速` / `Retry`）中英。
- 纯前端，build 覆盖 dist 即生效、未重启后端。新产物 `index-nCRwIFRg.js` / `Models-CGpanPte.js`，线上 200、旧 hash 404。
- 浏览器实拍：整轮后 4 个失败行各带「重新测速」；点其中一个 → 仅该行转圈（`.st-spin`=1）、按钮未变「测速中…」（未误触发整轮）、15s 后该行结果刷新。截图 `docs/speedtest-per-row-retry.png`。

### 追加：每行常驻「单独测速」按钮（取代仅失败行重试）
老板要常态下（没跑整轮时）每个模型行右侧都有测速按钮，可单独测任意一个模型。
- 把上一版只在 `st-err` 里的「重新测速」按钮改为**每个 `st-row` 右侧常驻一个 ⚡ 图标按钮** `.st-one`（`bolt` 图标，`title/aria-label=单独测速`），点击调 `retestOne(id)` 只测该模型；`latTesting || latCurrentId` 时禁用（一次只允许一个测速动作，防与整轮/另一单测打架）。
- `st-err` 移除旧 `.st-retry` 按钮，只留错误文本；删除 `.st-retry` 样式，新增 `.st-one` 图标按钮样式（hover 用 `--primary`/`--primary-soft`）。
- i18n：删 `models.retry`、加 `models.testOne`（`单独测速` / `Test this model`）中英。
- 纯前端，未重启后端。新产物 `index-BYzyfwi3.js` / `Models-CJ5mchi4.js`，线上 200、旧 hash 404。
- 浏览器实拍：9 个文本模型行 = 9 个 `.st-one` 按钮、无 `.st-retry` 残留；点第一行 `agnes-3.0-flash` 只它出 `754 ms`、其余仍 `—`、未误触发整轮。截图 `docs/speedtest-per-row-test-button.png`。

---

## 2026-09-15 · 移除 OpenCode 的 mimo-v2.5-free 模型

**需求**：老板要求把 `mimo-v2.5-free` 从 OpenCode 供应商去掉，相关配置与文档一并清理。

**改动**
- **`data/gateway.yaml`**：删 `providers.opencode.models` 里的 `mimo-v2.5-free` 条目、删 `pricing.per_model` 的 `mimo-v2.5-free`；opencode 注释「这 4 个」改「这 3 个」，并在排除/已移除清单加一行说明（此前实测可 200，但免费档常 429 限流，按要求移除）。opencode 现 3 个免费模型：`ling-3.0-flash-fin-free` / `nemotron-3-ultra-free` / `nemotron-3.5-lightning-free`。
- **`README.md`**：供应商表格 OpenCode 行的模型清单去掉 `mimo-v2.5-free`。
- **`tests/test_gateway.py`**：`test_load_config_providers` 里 opencode 模型集断言去掉 `mimo-v2.5-free`。
- 注意：只删 OpenCode 的 `mimo-v2.5-free`，**b.ai 的 `mimo-v2.5` 保留不动**。

**验证**：`yaml.safe_load` 后 opencode models=3、pricing 无该项；全量单测 153 passed（2 个历史遗留不变）。**纯配置改动，hot_reload 自动生效、未重启**——线上 `/v1/models` 由 12 → 11、`mimo-v2.5-free` 已消失、`mimo-v2.5`(b.ai) 仍在。CHANGELOG 保留此前收录/实测该模型的历史条目（真实记录），此处仅追加移除说明。

---

## 2026-09-15 · 排查「opencode 模型大概率失败」+ 测速探针加 429/5xx 重试

**排查结论（非网关 bug）**：翻网关日志 `opencode.ai/zen/v1/chat/completions` 上游响应 = 30×200 / 14×429 / 3×503，且有明显「首个 429 → 隔 1~2s → 200」模式。**根因是 OpenCode 免费档对共享 key 的激进限流(429) + 偶发 503**。真实 chat 直连有 `retry:3` 兜底（退避 0.3/0.6s）多能扛过；但**一键测速是单次探针、不重试**，正好撞高频首个 429 → Models 页显示一片"失败"。反复点测速又消耗同一把免费 key 的额度 → 越点越限流。

**修复（老板选定的方案 1）**：让测速探针也重试。
- `app/main.py` 新增 `_speedtest_retryable(e)`：仅上游 **429 / 5xx** 判为可重试；4xx（模型不存在/鉴权/参数）与超时**不重试**（前者无意义、后者拖慢整轮）。
- `/v1/models/speedtest` 探针由单次改为 `for attempt in range(1,4)` 最多 3 次、退避 `0.3*attempt`（与真实链路一致），成功即 break；`latency_ms` 记整个重试过程总耗时（用户真实等待）。

**验证**：`_speedtest_retryable` 判定单测全对（429/503/500→重试、400/401/404/超时→不重试）；全量单测 153 passed；`supervisorctl restart` → pid 249194、healthz ok。日志实测到重试生效：`ling-3.0-flash-fin-free` 一次 `503→503→200`（attempt 1/3、2/3）被救回。

**遗留发现（待老板定）**：`nemotron-3-ultra-free` 仍测速失败，但**已不是 429**——直连实测它响应要 **88.4 秒**（"Ultra" 大推理模型 + 免费档，极慢），远超探针 15s 超时；`nemotron-3.5-lightning`(2.4s)、`ling`(1.1s) 正常。可选：①调大探针超时（但 88s 会让整轮测速极慢、意义不大）；②像 mimo-v2.5-free 一样把 nemotron-3-ultra-free 移除（88s 不实用）；③保留（测速如实显示它太慢，真实 chat 靠 120s server timeout 仍可用但要等 ~88s）。

---

## 2026-09-15 · 整体移除 OpenCode Zen 供应商

**需求**：老板决定把 OpenCode Zen 供应商整个去掉，相关配置、代码、文档一并清理。

**移除（opencode 专属）**
- **`data/gateway.yaml`**：删 `providers.opencode` 整块（base_url / `token_env: OPENCODE_API_KEY` / UA+哨兵 `extra_headers` / 3 个免费模型）+ `pricing.per_model` 里 3 条 opencode 免费模型 + 顶部注释供应商清单去 OpenCode。
- **`app/adapters/openai_compat.py`**：删 `OPENCODE_SESSION_SENTINEL` 常量、`_call_headers()` 方法、`import uuid`；`chat()`/`chat_stream()` 请求头由 `self._call_headers()` 回滚为 `self._headers`（适配器回到"纯静态头"）。
- **`data/.env`**：删 `OPENCODE_API_KEY` 行（保留 AGNES/B_AI/VOLC 三把，权限 0600）。
- **`README.md`**：顶部"四个供应商"→"三个"、架构注释"四家通用+opencode 会话头"→"三家通用+媒体端点"、供应商表格 OpenCode 行删除、OpenCode 两条硬性要求 blockquote 删除。
- **`tests/test_gateway.py`**：`test_load_config_providers` 回退到 3 家、删 opencode 相关断言。

**保留（通用改进，非 opencode 专属）**：测速探针 429/5xx 重试（`_speedtest_retryable` + 重试循环）与 `_upstream_detail` 空 body→「调用失败」兜底 —— 对 b.ai/agnes 等偶发限流同样受益，仅把注释里的 "OpenCode" 字样改通用。前端 Models 页动态读 `/v1/models`，opencode 行自动消失，无需改前端。

**验证**：`yaml.safe_load` 后 providers=[agnes,b_ai,volc_engine]、文本模型回到 5 个；`openai_compat` 已无 `_call_headers`/`OPENCODE_SESSION_SENTINEL`；`import app.main` OK；源码/配置/README/测试 grep `opencode` 清零（CHANGELOG 历史条目保留）；全量单测 153 passed。代码有改动 → `supervisorctl restart` → pid 254282、healthz ok、线上 `/v1/models` = 8（无 opencode）。CHANGELOG 此前收录/排查 opencode 的历史条目全部保留（真实记录）。

---

## 2026-09-15 · 移除 b.ai 的 mimo-v2.5 模型

**需求**：老板要求把 b.ai 供应商下的 `mimo-v2.5` 也去掉，相关配置、代码、文档一并清理。（注：与上一轮已删的 OpenCode `mimo-v2.5-free` 是两个不同模型。）

**改动**
- **`data/gateway.yaml`**：删 `providers.b_ai.models` 里的 `mimo-v2.5` 条目、删 `pricing.per_model` 的 `mimo-v2.5`、并从 **`routes.wjd` 兜底链**移除第 4 项 `mimo-v2.5`（`hy3` 由「5)」改「4)」重编号）。wjd 链现 `[ark-code-latest, agnes-3.0-flash, qwen3.8-flash, hy3]`。
- **`app/main.py`**：测速探针注释里 `b.ai(qwen3.8-flash/mimo-v2.5/hy3)` 去掉 `mimo-v2.5`（`max_tokens>2` 约束对 qwen3.8-flash/hy3 仍成立，逻辑不变）。
- **`README.md`**：供应商表格 b.ai 行模型清单去掉 `mimo-v2.5`；路由链示例同步去掉并重编号。
- **`tests/test_gateway.py`**：`test_load_config_routes` 断言 `wjd.models` 数量由历史遗留的 `== 6` 修正为实际 `== 4`（该用例此前一直 FAIL，本次顺带修好）。

**验证**：`yaml.safe_load` 后 b_ai models=[qwen3.8-flash,hy3]、wjd 链 4 项、pricing 无 mimo-v2.5；源码/配置/README/测试 grep `mimo-v2.5` 清零（CHANGELOG 历史保留）；全量单测 **154 passed / 1 failed**（routes 用例转绿，仅剩 feedback 无关遗留）。**纯配置改动，hot_reload 自动生效、未重启**——线上 `/v1/models` 由 8 → 7、`mimo-v2.5` 已消失。

---

## 2026-09-15 · 初始化 Git 仓库并推送到 CNB（cnb.cool）

- 项目此前**不是 git 仓库**：`git init` 建库、设提交身份、首次提交（**91 个源码文件**）。
- **`.gitignore` 补充**忽略 `node_modules/`、`frontend/node_modules/`、`frontend/dist/`、`dist/`、`*.egg-info/`、`.DS_Store` —— 否则 `git add -A` 会把 **15342 个 node_modules 文件**塞进仓库。
- **推送前安全审计**：确认 `.env`、`data/users.json`、`data/history.json`、`data/feedback.json`、`data/feedback_media/` 全部被忽略、无任何密钥/隐私入库（仅无密钥的 `.env.example` 模板随库）。
- **远程**：CNB `https://cnb.cool/Jidekin/api-gateway`，分支 `master`，首推成功（远端 `HEAD=6cc65ff` 与本地一致）。`origin` 配为不含 token 的干净地址，**令牌不写入 `.git/config`**；后续推送用 `git push https://cnb:<token>@cnb.cool/Jidekin/api-gateway.git master`。