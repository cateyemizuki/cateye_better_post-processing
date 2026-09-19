# cateye_better_post_processing（更好的消息后处理）

出站消息增强插件，提供五个互相独立的功能：

| 功能 | 默认 | 说明 |
|---|---|---|
| 引用回复接管 | 开 | 框架 `chat.reply_style.enable_reply_quote` 关闭时，为指向目标消息的回复**按权重**抽取发送方式：直接回复 / 引用回复 / @回复 / 引用＋@回复 |
| 回复后表情包 | 开 | planner 激活回复后，若整轮回复没有携带表情包，按概率补发一张（指定情绪标签或随机） |
| 表情包跟风 | 开 | 群友连续发送 `threshold` 条表情包后，按配置方式跟发一张（与最后一个同情绪 / 指定情绪 / 纯随机） |
| 文本替换规则 | 空 | 对 bot 发出的纯文本做词替换或整条覆盖，可配多条 |
| 表情包含义库 | 开 | 为每张表情包的描述标签补录视觉模型生成的"准确内容"，并在回复前把上下文中出现的表情包含义注入 replyer |

## 安装

把整个 `cateye_better_post-processing` 目录放进 MaiBot 安装目录的 `plugins/` 下，
然后重启 MaiBot（或在 WebUI 插件管理中启用本插件）。

> 本插件使用的 Hook 与能力（`send_service.*`、`maisaka.reply.before_post_process`、
> `maisaka.replyer.before_request`、`chat.receive.after_process`、
> `emoji.register.after_build_description`、`message.get_by_id`、
> `message.get_by_time_in_chat`、`emoji.*`、`database.query`、`llm.generate`、
> `send.emoji`）在 MaiBot 1.2.3 / maibot_sdk 2.8.0 上验证。manifest 声明的最低
> 宿主版本为 1.2.3。

## 配置

配置文件位于 `plugins/cateye_better_post-processing/config.toml`（或 WebUI 插件配置页）。

### `[quote_reply]` 引用回复接管

| 字段 | 默认 | 说明 |
|---|---|---|
| `takeover` | `true` | 框架【启用引用回复】关闭时接管；框架开关开启时插件不介入 |
| `weight_direct` | `2` | 权重：直接回复（不引用也不@） |
| `weight_quote` | `6` | 权重：引用回复 |
| `weight_at` | `1` | 权重：@回复（@被回复消息的发送者，仅群聊生效） |
| `weight_quote_at` | `1` | 权重：引用＋@回复（仅群聊生效） |
| `stale_threshold_messages` | `3` | 回复目标之后出现 ≥N 条消息即视为"对话已推进"：抽取时剔除"直接回复"，并按 `stale_at_weight` 调整 @回复 权重（0=关闭） |
| `stale_exclude_bot_messages` | `false` | 统计目标后消息数时排除 bot 自己的消息（默认计入所有消息） |
| `stale_at_weight` | `-1` | 对话已推进时 @回复 的权重；`-1`（或留空）表示自动取原 @权重 的一半 |
| `stale_age_seconds` | `1800` | 回复目标发出超过 N 秒后，本次回复强制不引用直接发送（0=关闭；优先级高于"对话已推进"的剔直调整） |

抽取仅在"这条回复确实指向某条目标消息"（`reply_message_id` 非空）时进行；
主动发言、表情等非回复发送不受影响。**同一轮回复只抽取一次**（首个携带轮目标
消息的分段生效，后续分段与错别字更正段保持宿主原生行为），多分段回复不会出现
"前一段引用、后一段@"的混搭。@ 目标查询失败（目标消息已被清理）时回退为引用
回复；**私聊会话自动剔除包含 @ 的权重判定**（QQ 私聊正常不会出现 @，只有引用/
不引用两种可能）；全部权重为 0 时回退为仅引用回复。

**对话已推进时的调整**（0.5.0 新增）：发送时统计回复目标之后入库的消息条数
（`message.get_by_time_in_chat`，查询上限为阈值+1，默认计入所有消息、可用
`stale_exclude_bot_messages` 排除 bot 自己的）。当条数 ≥
`stale_threshold_messages` 时，本次抽取剔除"直接回复"，并把 @回复 权重调为
`stale_at_weight`（`-1` 或留空则取原 @权重 的一半），避免裸回复指代不明。目标时间戳
查询失败或统计失败时按原权重抽取（fail-open）。

**目标超时的降级**（0.6.0 新增）：目标消息发出超过 `stale_age_seconds` 秒
（默认 30 分钟）后，本次回复强制**不引用直接发送**——对很久以前的消息做引用
观感突兀。该规则优先级高于"对话已推进"的剔直调整（两者同时命中时按超时处理）。
目标消息的时间戳取不到（已被清理、负缓存窗口内、无时间戳字段）时本规则跳过、
按正常抽取执行。

### `[emoji_after_reply]` 回复后表情包

planner 通过 `maisaka.reply.before_post_process` 激活一轮回复后，插件用
`send_service.after_send` 观察本轮全部出站消息（多分段也覆盖）。当出站消息静默
`quiet_seconds` 秒且本轮没有出现表情包组件时，按 `probability` 概率抽取一张：

- `emotion` 留空 → 随机抽取；
- `emotion` 填情绪标签（即表情包描述/情绪词，如 `开心`）→ 按情绪抽取，取不到就跳过。

`round_window_seconds` 限制一轮记录的最长存活时间。planner 自己先发过表情包
（`send_emoji` 工具，15 秒内、含未入库的）时不会补发。发送前统一受聊天流表情
冷却约束（见下节）。

### `[emoji_follow]` 表情包跟风

通过 `chat.receive.after_process` 统计群聊中连续入站表情包条数（中间出现任何
非表情包消息即重新计数；系统通知消息如戳一戳不参与计数也不会打断连击），
达到 `threshold` 后按 `probability` 概率跟发一张：

| `mode` | 行为 |
|---|---|
| `same` | 抽取与最后一个表情包描述相同情绪的一张（描述缺失则跳过） |
| `specified` | 抽取 `emotion` 配置的指定情绪 |
| `random` | 纯随机抽取 |

`cooldown_seconds` 已移除；触发一次后计数立即归零（概率或冷却落空同样归零，
需重新攒满 `threshold` 条才会再次尝试）。发送前统一受聊天流表情冷却约束（见下节）。
仅群聊生效。

### `[emoji_cooldown]` 聊天流表情包冷却

同一聊天流内，**任意来源**的表情包消息只要入库（planner 的 `send_emoji` 工具、
其它插件、本插件自身；以发送时的 `storage_message` 为准），即进入 `seconds` 秒
冷却。冷却期内本插件不会主动发送表情包——"回复后表情包"与"表情包跟风"共用
同一冷却；本插件自己发送的表情包同样会刷新冷却起点，因此不会出现"补发一张又
跟发一张"的连击。设为 `0` 可关闭冷却。

> 行为变更（0.4.0）：原 `[emoji_after_reply] cooldown_seconds` 与
> `[emoji_follow] cooldown_seconds` 两个独立冷却已移除，统一由本节控制。

### `[text_rules]` 文本替换规则

规则列表，每条一个字符串，两种格式：

- `"shit"replace"filter"` —— 把消息文本中的 `shit` 全部替换为 `filter`
  （替换词留空等价于删除该词）；
- `"草"cover"你好"` —— 消息文本包含 `草` 时，整条文本覆盖为 `你好`。

解析是严格的：关键词非空、ASCII 双引号、小写 `replace`/`cover` 关键字；**格式
错误的条目在加载/热更新时记录日志并忽略**。多条规则的应用顺序：先依次应用全部
`replace`（全量替换），再按配置顺序检查 `cover`，第一条命中的 `cover` 直接覆盖
整条文本。规则只作用于 bot 自己发出的纯文本（text 组件），不影响表情包、图片
等组件。

### `[emoji_meaning]` 表情包含义库

宿主里每张表情包都带一个视觉模型生成的**情绪标签描述**（`images.description`），
replyer 在上下文里看到的表情包就是这个标签（`[表情包: 描述]`）——只有"大致的情绪"，
看不到图片的实际内容。本插件维护一份**插件自有 SQLite 库**（位于插件数据目录
`emoji_meanings.db`），以描述标签为关联键，补录由视觉模型生成的"准确内容"
（画面内容 + 梗含义），并在 replyer 请求前注入上下文。

**两条补录通道**：

| 通道 | 触发方式 | 说明 |
|---|---|---|
| 周期补录 | 每 `scan_interval_seconds` 扫描一次宿主表情库（`database.query`） | 把还没有含义的描述标签加入队列，覆盖插件安装前就存在的表情包 |
| 即时补录 | 宿主 `emoji.register.after_build_description` 钩子 | 新表情包完成描述生成的瞬间入队，覆盖安装后新入库的表情包 |

含义生成由后台工作循环完成（每 `worker_interval_seconds` 一轮、每轮最多
`batch_size` 条，用 `emoji.get_all` 按描述取图 + `llm.generate` 的 `model_task`
视觉任务生成），单条失败最多重试 `max_attempts` 次，单条长度截断到
`max_meaning_length`。生成提示词可用 `generation_prompt` 覆盖（留空用内置）。

收敛与复活规则：扫描只补录"已注册、有文件、未禁用"的表情（与 `emoji.get_all`
的运行库对齐，Images 表里描述已生成但从未注册成功的行不会被反复空扫）；一条
含义连续失败 `max_attempts` 次后进入放弃名单，周期扫描不再重试，只有该表情包
重新注册（重新触发描述生成钩子）才会复活重试。

**上下文注入**（`inject_enabled`）：replyer 请求前（`maisaka.replyer.before_request`），
插件收集**被回复消息**与**会话近期消息**中出现的表情包描述，从含义库查出准确内容，
注入 `extra_prompt`（最多 `inject_max_emojis` 条），让 replyer 准确理解表情包语境：

```
【表情包准确含义参考】
以下是本会话近期出现的表情包的实际内容，供你准确理解语境；仅供理解，不要复述其文字，也不要模仿发送。
1. 表情包标签「一只柴犬裂开」：柴犬瘫倒翻白眼，配文"毁灭吧"，表达摆烂/自嘲的无奈。
```

说明与限制：

- 以描述标签为关联键：宿主公开能力不返回表情包文件哈希，无法按哈希精确取图；
  而描述标签正是 replyer 上下文里表情包的呈现键。同一描述对应多张图时取任意
  一张生成含义。
- bot 自己发送的表情包在上下文中没有可关联的描述标签（`send.emoji` 通道渲染为
  无标签占位、`send_emoji` 工具通道只写裸描述文本），均不参与注入。
- 含义文本来自视觉模型对群友图片的描述，属不可信数据：插件只做长度截断并包裹
  为"参考数据"，请勿在其周围放置可被拼接的指令文本。

### `[plugin]`

`enabled` 控制插件总开关；`config_version` 由插件维护，请勿手改。

### WebUI 显示与翻译（0.6.1）

- 所有配置字段都有中文短标签（`label`），不再显示英文变量名；详细说明悬停在
  字段标签上查看。
- 插件声明 `supported_locales: ["zh-CN", "en"]`，并为每个配置节（标题/描述）与
  字段（标签/提示）内置英文 i18n——WebUI 切换到英文界面时配置页整体显示英文，
  中文界面与英文界面之外的语言回退中文。

## 与框架的分工

- **回复后处理（错别字 + 分段）已交还框架**：0.1.x 版本曾在框架关闭
  `response_post_process.enable_response_post_process` 时复刻后处理，但分段发送
  循环锁死在宿主回复工具内部、受全局开关门控，插件 Hook 无法重新触发，接管后
  只能降级为换行拼接单条消息。请保持该框架开关**开启**以获得原生分段发送。
  可行的绕行方案（Hook 里置空 response 后由插件自发分段）会让 planner 观察到
  "回复工具失败"，存在模型重新回复导致刷屏的风险，故不采用。
- 引用回复：框架 `chat.reply_style.enable_reply_quote` 开启时插件完全不介入。

## 能力声明（capabilities）

`config.get`、`message.get_by_id`、`message.get_by_time_in_chat`、
`emoji.get_by_description`、`emoji.get_random`、`emoji.get_all`、`database.query`、
`llm.generate`、`send.emoji`。插件不访问宿主内部模块（无 `import src.*`）、不直接
读写宿主数据库与文件系统，自有数据（表情包含义库）落在 `ctx.paths.data_dir`，
全部宿主数据访问均通过公开能力代理完成。

## 开发

- `text_rules.py` 为纯逻辑模块（不依赖 SDK），可离线单测；`plugin.py` 为 SDK 接线层。
- 修改配置用 WebUI 热重载即可；改 `_manifest.json`（能力声明）需完整重启 MaiBot。
