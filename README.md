# cateye_better_post_processing（更好的消息后处理）

出站消息增强插件，提供下列可分别开关的独立功能：

| 功能 | 默认 | 说明 |
|---|---|---|
| 后处理接管 | 开 | **需宿主关闭** `response_post_process.enable_response_post_process` **与** `experimental.enable_rich_reply`：以与 MaiBot 完全一致的原逻辑接管错别字注入 + 分段 + 颜文字保护 + 长度/句数守卫（参数沿用宿主 `response_splitter.*` / `chinese_typo.*` / `typing_speed`）；**各分段作为独立消息依次发送**（首段走宿主原生链路，其余由插件补发并模拟打字，更正段引用上一段） |
| 引用回复接管 | 开 | **需宿主关闭** `chat.reply_style.enable_reply_quote` **与** `experimental.enable_rich_reply`：为指向目标消息的回复**按权重**抽取发送方式：直接回复 / 引用回复 / @回复 / 引用＋@回复；**群聊与私聊权重、过旧规则、"同一消息只引用一次"记录各自独立**（私聊恒不 @） |
| 同一消息只引用一次 | 开 | 一条目标消息被引用过之后，之后对该消息的任何回复都不再引用（群聊/私聊分开记录）；抽到直接回复不消耗机会 |
| 回复后表情包 | 开 | planner 激活回复后，若整轮回复没有携带表情包，按概率补发一张（指定情绪标签或随机）；planner 本轮已计划发表情/贴表情、或本轮回复实际没发出时不补发 |
| 表情包跟风 | 开 | 群友连续发送 `threshold` 条表情包后，按配置方式跟发一张（与最后一个同情绪 / 指定情绪 / 纯随机）；不计入 bot 自身消息与插件注入的合成记录 |
| 文本替换规则 | 空 | 对 bot 发出的纯文本做词替换或整条覆盖，可配多条；默认只作用于 planner 拉起的回复流程，不碰其它插件直接发出的文本 |
| 表情包含义库 | 关闭（试验性） | 为每张表情包的描述标签补录视觉模型生成的"准确内容"，并在回复前把上下文中出现的表情包含义注入 replyer（默认只注入 planner 拉起的回复流程） |

<!-- TOC -->

## 目录

- [安装](#安装)
- [模块与前置条件（重要）](#模块与前置条件重要)
- [配置](#配置)
  - [配置板块总览](#配置板块总览)
  - [三条通用规则](#三条通用规则)
  - [取值范围与校验](#取值范围与校验)
  - [[plugin]](#plugin)
  - [[quote_reply] 引用回复](#quote_reply-引用回复)
  - [[quote_reply_weights] 引用回复 · 权重](#quote_reply_weights-引用回复--权重)
  - [[quote_reply_private] / [quote_reply_private_weights] 引用回复（私聊）](#quote_reply_private--quote_reply_private_weights-引用回复私聊)
  - [[chinese_typo] 错别字](#chinese_typo-错别字)
  - [[chinese_typo_weights] 错别字 · 纠正方式权重](#chinese_typo_weights-错别字--纠正方式权重)
  - [[response_splitter] 分段](#response_splitter-分段)
  - [[emoji_after_reply] 回复后表情包](#emoji_after_reply-回复后表情包)
  - [[emoji_follow] 表情包跟风](#emoji_follow-表情包跟风)
  - [[emoji_cooldown] 聊天流表情包冷却](#emoji_cooldown-聊天流表情包冷却)
  - [[text_rules] 文本替换规则](#text_rules-文本替换规则)
  - [[emoji_meaning] 表情包含义库（试验性，默认关闭）](#emoji_meaning-表情包含义库试验性默认关闭)
  - [WebUI 显示与翻译（0.6.1；提示文本 0.11.1）](#webui-显示与翻译061提示文本-0111)
- [与框架的分工](#与框架的分工)
- [与其它插件共存](#与其它插件共存)
  - [与「智能分段插件」(saberlights/smart_segmentation_plugin)](#与智能分段插件saberlightssmart_segmentation_plugin)
  - [与其它出站类插件](#与其它出站类插件)
- [能力声明（capabilities）](#能力声明capabilities)
- [开发](#开发)
- [参考实现与出处](#参考实现与出处)
- [作者与许可](#作者与许可)
- [致谢](#致谢)

<!-- /TOC -->

## 安装

把整个 `cateye_better_post-processing` 目录放进 MaiBot 安装目录的 `plugins/` 下，
然后重启 MaiBot（或在 WebUI 插件管理中启用本插件）。

> 本插件使用的 Hook 与能力（`send_service.before_send` / `send_service.after_build_message` /
> `send_service.after_send`、`maisaka.reply.before_post_process`、
> `maisaka.planner.before_request`、`maisaka.planner.after_response`、
> `maisaka.replyer.before_request`、`chat.receive.before_process`、`chat.receive.after_process`、
> `emoji.register.after_build_description`、`message.get_by_id`、`message.get_by_time_in_chat`、
> `emoji.*`、`database.query`、`database.delete`、`llm.generate`、`send.emoji`、`send.text`、
> `api.call`）在 MaiBot 1.2.3 / maibot_sdk 2.8.0 上验证。manifest 声明的最低宿主版本为 1.2.3。
>
> 多段发送（`[response_splitter]` 的 `takeover`）需要 `send.text` 能力，
> 「撤回重发」纠错分支需要 `api.call` 能力，**改动过 manifest 需完整重启 MaiBot**。

## 模块与前置条件（重要）

插件按"接管对象"拆成独立模块（代码在 `modules/` 子目录）。**两个接管模块必须由宿主关闭对应能力
才会工作**——否则即使插件配置里把它们打开，模块也会**完全静默**：不改写文本、不抽取回复方式、
不发任何消息。启动时与**每次配置变更时**都会在日志里打印各模块的可用状态：

```text
[模块状态｜启动] 引用回复接管：静默（需在宿主配置关闭「丰富回复」（experimental.enable_rich_reply，当前 True））
[模块状态｜启动] 后处理接管：可用（启用回复后处理已关闭、丰富回复已关闭）
[模块状态｜启动] 回复后表情包：可用（不受宿主开关约束）
```

| 模块 | 需要的宿主配置（**全部满足**才可用） | 是否受制 |
|---|---|---|
| 引用回复接管（`quote_reply`） | `chat.reply_style.enable_reply_quote = false` 且 `experimental.enable_rich_reply = false` | 受制 |
| 后处理接管（`response_splitter`） | `response_post_process.enable_response_post_process = false` 且 `experimental.enable_rich_reply = false` | 受制 |
| 回复后表情包 / 表情包跟风 / 表情包含义库 / 文本替换规则 | —— | **不受制**，宿主开关任意状态都照常工作 |

> **为什么两个接管都要关「丰富回复」**：开启后回复工具会给消息挂图片/表情/@ 等附件，分段与引用语义
> 都由富回复链路接管，插件的"首段文本"判定与引用注入会与宿主打架。这也是官方文档要求的使用前提。
>
> 想"开着丰富回复也要接管"时，把插件配置 `[plugin] rich_reply_gate` 关掉即可（默认开）：
> 此时不再把丰富回复当硬门槛，状态行会写成 `可用（…、丰富回复未做门控）`。代价是附件只挂在
> **首段**、补发分段不带附件，与宿主原生"附件挂最后一段"的语义不同，请自行确认可接受。
>
> 读不到宿主配置时（能力异常）按**未满足**处理，模块保持静默并在日志里写明原因——宁可少做事，
> 也不要和宿主原生能力重复处理同一条消息。
>
> 日志级别：可用模块用 `info`、**静默模块用 `warning`**——"配置打开了却什么都不做"最难排查，
> 静默原因必须显眼。

## 配置

配置文件位于 `plugins/cateye_better_post-processing/config.toml`（或 WebUI 插件配置页）。

### 配置板块总览

**按功能板块排版**（`config.toml` 里的分节顺序就是这个）：

| 顺序 | 板块 | 内容 |
|---|---|---|
| 1 | `[plugin]` | 插件开关与配置版本 |
| 2 | `[quote_reply]` | 引用回复：什么时候接管、目标过旧怎么处理、引用去重 |
| 3 | `[quote_reply_weights]` | 引用回复的**权重** |
| 4 | `[quote_reply_private]` | 引用回复（私聊） |
| 5 | `[quote_reply_private_weights]` | 私聊引用回复的**权重** |
| 6 | `[chinese_typo]` | 错别字：注入参数 + 纠正行为 |
| 7 | `[chinese_typo_weights]` | 错字**纠正方式权重** |
| 8 | `[response_splitter]` | 分段：分割参数 + 打字速度 + 接管机制 |
| 9 | 其它功能 | `[emoji_after_reply]` / `[emoji_follow]` / `[text_rules]` / `[emoji_cooldown]` |
| 10 | `[emoji_meaning]` | **试验性**（表情包含义库）——试验性功能一律放最后 |

### 三条通用规则

1. **不区分"宿主提供的"还是"插件提供的"**：同一个功能的开关都放同一个板块；宿主本来就有的参数
   用宿主的字段名与说明（宿主 WebUI 上叫什么，这里就叫什么）。
2. **权重单独成板块**，紧跟在它的功能板块之后。
3. **留空 = 跟随宿主**：数值项留空（空字符串）、开关项选「跟随宿主」、文本项留空，都表示用宿主配置里
   的值；填了值就以插件为准。**首次生成 `config.toml` 时插件会读宿主 `config/bot_config.toml` 的现值
   填进去**，所以默认看到的就是宿主当前值（想重新跟随宿主就把它清空）。每项都带一句行尾中文注释，
   同一句文案也会作为 WebUI 的**提示文本**显示（见下方「WebUI 显示与翻译」）。

> 唯一**故意不搬**的是宿主 `[response_post_process] enable_response_post_process`：它是本插件的
> **生效条件**（宿主开着它时宿主自己做后处理，插件必须让位），不是可以随便改的参数。

### 取值范围与校验

所有数值项的合法范围写在下表；**超范围的值会被配置校验直接拒绝**（Pydantic `ValidationError`，
不是静默截断成边界值），WebUI 输入框也带同样的 min/max。留空（空字符串）= 跟随宿主，不受数值范围约束。

| 板块 | 字段 | 取值范围 | 含义 |
|---|---|---|---|
| `[quote_reply]` | `stale_threshold_messages` | `0~` | 对话已推进阈值（条） |
| `[quote_reply]` | `stale_age_seconds` | `0~` | 目标超时后强制直接回复（秒） |
| `[quote_reply_weights]` | `weight_direct` | `0~` | 权重：直接回复 |
| `[quote_reply_weights]` | `weight_quote` | `0~` | 权重：引用回复 |
| `[quote_reply_weights]` | `weight_at` | `0~` | 权重：@回复 |
| `[quote_reply_weights]` | `weight_quote_at` | `0~` | 权重：引用＋@回复 |
| `[quote_reply_private]` | `stale_threshold_messages` | `0~` | 对话已推进阈值（条） |
| `[quote_reply_private]` | `stale_age_seconds` | `0~` | 目标超时后强制直接回复（秒） |
| `[quote_reply_private_weights]` | `weight_direct` | `0~` | 权重：直接回复 |
| `[quote_reply_private_weights]` | `weight_quote` | `0~` | 权重：引用回复 |
| `[chinese_typo]` | `correction_max_cjk` | `0~` | 超限不纠错 |
| `[chinese_typo_weights]` | `weight_correction_direct` | `0~` | 权重 · 直接发送 |
| `[chinese_typo_weights]` | `weight_correction_quote` | `0~` | 权重 · 引用纠正 |
| `[chinese_typo_weights]` | `weight_correction_recall` | `0~` | 权重 · 撤回重发 |
| `[chinese_typo_weights]` | `weight_correction_none` | `0~` | 权重 · 不纠正 |
| `[chinese_typo_weights]` | `weight_correction_last` | `0~` | 权重 · 最后纠正 |
| `[emoji_after_reply]` | `probability` | `0~1` | 补发概率 |
| `[emoji_after_reply]` | `quiet_seconds` | `0.5~` | 静默判定时长（秒） |
| `[emoji_after_reply]` | `round_window_seconds` | `1~` | 回复轮窗口（秒） |
| `[emoji_after_reply]` | `first_send_timeout_seconds` | `0~` | 首条出站等待（秒） |
| `[emoji_after_reply]` | `planned_emoji_ttl_seconds` | `5~` | planner 表情标记有效期（秒） |
| `[emoji_follow]` | `threshold` | `1~` | 触发条数 |
| `[emoji_follow]` | `probability` | `0~1` | 跟发概率 |
| `[emoji_follow]` | `streak_window_seconds` | `10~` | 连击保鲜（秒） |
| `[emoji_cooldown]` | `seconds` | `0~` | 冷却时长（秒） |
| `[emoji_meaning]` | `max_tokens` | `32~4096` | 生成 max_tokens |
| `[emoji_meaning]` | `scan_interval_seconds` | `60~` | 库扫描间隔（秒） |
| `[emoji_meaning]` | `worker_interval_seconds` | `5~` | 生成循环间隔（秒） |
| `[emoji_meaning]` | `batch_size` | `1~50` | 每轮生成条数 |
| `[emoji_meaning]` | `max_attempts` | `1~20` | 失败重试上限 |
| `[emoji_meaning]` | `max_meaning_length` | `20~1000` | 单条含义最大长度 |
| `[emoji_meaning]` | `inject_max_emojis` | `1~20` | 单次注入条数上限 |

> 共 32 项有范围约束（配置面共 81 项）；其余项是布尔 / 三态开关 / 文本 / 列表，不受数值范围限制。

### `[plugin]`

`enabled` 控制插件总开关；`config_version` 由插件维护，请勿手改。

`rich_reply_gate`（0.10.2 新增，默认 `true`）控制两个接管是否把宿主「丰富回复」
（`experimental.enable_rich_reply`）当硬门槛。默认开启时，宿主开着丰富回复 → 两个接管静默
（原因见[模块与前置条件](#模块与前置条件重要)）；关掉本项后不再门控，代价是富回复附件只挂在首段。


**`[plugin]` 字段表**（默认值 / 说明与 `config.toml` 行尾注释、WebUI 提示同源）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `True` | 是否启用本插件。 |
| `rich_reply_gate` | `True` | 开启时：宿主开着「丰富回复」就不接管（附件与分段语义冲突）。 |
| `config_version` | `'0.11.2'` | 配置版本，与插件版本同步（自动维护，不要手改）。 |

### `[quote_reply]` 引用回复

| 字段 | 默认 | 说明 |
|---|---|---|
| `takeover` | `true` | 宿主【启用引用回复】关闭**且** `experimental.enable_rich_reply` 关闭时，由插件为指向目标消息的回复按权重抽取发送方式；任一未关闭则本模块**静默** |
| `stale_threshold_messages` | `3` | 回复目标之后出现 ≥N 条消息即视为「对话已推进」：剔除直接回复，并把 @ 权重按权重板块里的「推进时 @ 权重」调整。`0` = 关闭 |
| `stale_exclude_bot_messages` | `false` | 统计"目标之后的消息数"时是否不数 bot 自己发的消息 |
| `stale_age_seconds` | `1800.0` | 回复目标发出超过 N 秒后**强制直接回复**——即**不引用、也不 @**，纯文本发出去（就是权重里的「直接回复」，**不是**"以引用的方式发"）。`0` = 关闭；优先级高于上面的推进判定 |
| `quote_once_per_target` | `true` | **同一消息只引用一次**：一条目标消息被引用过之后，后续回复不再引用它（抽到直接回复不消耗机会）。与群聊/私聊各自独立记录 |
| `style_once_per_target` | `true` | **回复方式只取首条**：只有对该目标消息的**第一条**回复能抽到 @，避免"前一条什么都没挂、后一条才 @ 人"的错位 |

#### 同一消息只引用一次（`quote_once_per_target`，群聊/私聊各自独立）

一条目标消息被引用过（本插件抽到引用/引用＋@，或被观测到以引用方式发出）之后，之后对该消息的任何
回复都不再引用它（只可能直接回复或 @）。抽到"直接回复"**不消耗**这次机会。该规则优先于
「目标超时后强制直接回复」。

#### @ 与正文之间的空格（全来源统一）

不管 @ 是本插件注入的还是消息里**本来就有**（宿主 `attach_at`、别的插件注入），最终都统一成
**`@某人 正文`（恰好一个半角空格）**：多出来的空白段会被删掉、正文自带的前导空白会被抹掉、
一个空格都没有时补一个。每次抽取回复方式还会打一行实际组件，便于核对：

```text
回复方式=at（注入 @）：前缀组件 [at:某人, text:' ', text:'正文']（会话 …，引用=…）
回复方式=quote_at（消息已有 @，仅统一空格）：前缀组件 [at:某人, text:' ', text:'正文']（会话 …）
回复方式=quote（消息已有 @，无需改动）（会话 …）
```

### `[quote_reply_weights]` 引用回复 · 权重

按权重抽取这条回复怎么引用：`weight_direct`（**直接回复 = 不引用也不 @**，默认 `2`）/ `weight_quote`（引用，默认 `6`）/
`weight_at`（@ 对方，默认 `1`）/ `weight_quote_at`（引用＋@，默认 `1`）；全为 0 时回退为"仅引用回复"。

`stale_at_weight`：**「对话已推进」时 @ 类回复的权重**，三档语义（与 WebUI 标签一致；`0` **真的**是不再 @）：

| 填法 | `at`（纯 @） | `quote_at`（引用＋@） |
|---|---|---|
| **留空** | 取原值的一半 | 不动 |
| **正数**（如 `3`） | 取该值 | 不动 |
| **`0`** | `0`（不再纯 @） | **也归 `0`**（不再「引用＋@」） |

（刻意不用 `-1` 这类负数哨兵。）

### `[quote_reply_private]` / `[quote_reply_private_weights]` 引用回复（私聊）

私聊只有「直接回复 / 引用回复」两种（QQ 私聊不会出现 @），因此权重板块只有
`weight_direct` / `weight_quote`。功能项与群聊**同名同义**（去重与过旧判定各自独立记录），
但**比群聊少一项**：私聊没有 `style_once_per_target`，因为私聊不存在 @，
**"回复方式只取首条"对私聊恒等效**（抽到 @ 的机会本来就没有），所以不对外暴露。


**`[quote_reply_private]` 字段表**（默认值 / 说明与 `config.toml` 行尾注释、WebUI 提示同源）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `takeover` | `True` | 私聊也按权重决定怎么引用（私聊恒不 @）。 |
| `stale_threshold_messages` | `3` | 被回复消息之后又来了这么多条消息，就算「对话已推进」。0 = 不判定。 **取值范围** `0~`。 |
| `stale_exclude_bot_messages` | `False` | 统计「对话已推进」时是否不数 bot 自己发的消息。 |
| `stale_age_seconds` | `1800.0` | 被回复的消息发出超过这么多秒后，本次回复既不引用也不 @（就是「直接回复」）。0 = 不判定。 **取值范围** `0~`。 |
| `quote_once_per_target` | `True` | 同一条目标消息只引用一次，后续回复不再引用它。 |

### `[chinese_typo]` 错别字

| 字段 | 默认 | 说明 |
|---|---|---|
| `enable` | `跟随宿主` | 宿主【启用错别字】：让麦麦偶尔打错字。关闭后不会注入错字，也就不会有纠正消息 |
| `error_rate` | 留空 | 宿主【单字错字概率】：**错字出现的总频率**（这是唯一的"错字概率"）。留空 = 跟随宿主（宿主默认 `0.01`）；想更容易看到错字就调大（如 `0.05`~`0.1`） |
| `min_freq` | 留空 | 宿主【最小字频】：只对常见程度达到该值的字尝试制造错字 |
| `tone_error_rate` | 留空 | 宿主【声调错字概率】：按相近声调制造错字的概率 |
| `word_replace_rate` | 留空 | 宿主【整词替换概率】：整词被替换成错词的概率 |
| `enable_correction_quote` | `跟随宿主` | 宿主【错别字纠正时引用原消息】：**仅未开启**下面的「错字纠正方式」时有意义 |
| `correction_quote_probability` | 留空 | 宿主【错别字纠正引用概率】：生成纠正消息时引用上一条错字消息的概率 |
| `correction_mode_enabled` | `false` | **错字纠正方式**（宿主没有的分支，见下） |
| `correction_max_cjk` | `20` | **超限不纠错**：某段汉字数超过该值时不纠正（错字留着）；`0` = 不限制 |
| `recall_fallback` | `quote` | 撤回失败时的兜底：`quote` 改成引用发送 / `direct` 直接发送 / `none` 放弃这条纠正 |
| `recall_purge` | `true` | **撤回后彻底清理**：删掉宿主消息库里那条记录，并让"即将被撤回"的自发分段不写进 bot 自己的上下文 |

> **没有第二个错字概率**：宿主代码里那半句「抽到纠正建议后 50% 发错字 / 50% 整句换成正确句」的门控
> **照抄硬编码 0.5**，不对外暴露——错字多久出现一次只看 `error_rate`。

> **与宿主唯一有意不同的一处**（0.11.2）：整词同音替换（`word_replace_rate`）在宿主里用
> `itertools.product` **无上限**穷举"每个字的同音字组合"，组合数随词长指数爆炸（「中华人民共和国」
> 约 11.5 亿、「社会主义现代化」约 5028 亿），一旦命中就会把工作线程占住几分钟到几小时。
> 本插件加了组合数硬上限（20 万），超限就跳过**该词**的整词替换（逐字错字照常），
> 并在「后处理接管统计」日志里以 `整词替换跳过=N(累计)` 提示。常用词（≤4 字）组合数远低于上限，
> 行为与宿主完全一致。

#### 错字纠正方式（宿主没有的分支）

宿主原生只会「抽到纠正建议 → 追加一条更正消息」。开启 `correction_mode_enabled` 后，改为对**每一处错字**
按权重抽取纠正方式：

| 方式 | 群里看到的样子 |
|---|---|
| 直接发送 | 错字消息之后紧跟一条纠正内容（不引用） |
| 引用纠正 | 紧跟一条纠正内容，**引用**那条带错字的消息（宿主原生观感） |
| 撤回重发 | **撤回**那条带错字的消息，然后重发**修正后的整句**（撤回后必须给完整内容） |
| 不纠正 | 错字留在消息里，不发任何纠正 |
| 最后纠正 | 该处纠正推迟到**本轮全部分段发完之后**，并**强制引用**它所属的错字消息（需在权重板块打开） |

**顺序保证（发送前定好）**：所有纠正方式在分段全部发出**之前**一次算完，动作序列是
`第 1 段（宿主发） → 它的纠正 → 第 2 段 → 它的纠正 → …`，所以"第二段出了错字"的纠正一定在
**第三段发出之前**完成；只有"最后纠正"按定义排在全部段之后。

注意事项：

- 需要后处理接管生效（`[response_splitter] takeover`，且宿主【启用回复后处理】关着）。
- 更正段是否引用（宿主的 `enable_correction_quote`）只在**未开启**本分支时生效；开启后引用与否由
  抽到的纠正方式决定（`quote`/`last` 必引用）。此时想"纠正一律不引用"就把 `weight_correction_quote`
  设为 `0`。
- **撤回后彻底清理**（`recall_purge`）的**局限**：宿主发出的**第 1 段**由宿主自己写历史，插件拦不住，
  它的库记录会被删掉，但内存上下文里那条要等重启/上下文重建后才消失。
- 顺带一个宿主事实：宿主的"正常撤回"**不会**删除原消息——NapCat 的 `group_recall` 只是被适配器包成
  一条 `is_notify` 通知消息入库（"[事件] xxx 撤回了一条消息"），原消息仍在库里与上下文里。
  "原消息消失"是本插件额外做的。
- 撤回走适配器公开 API（`adapter.napcat.message.delete_msg`，napcat 与 SnowLuma 都提供），
  失败按 `recall_fallback` 兜底。**该能力必须在 manifest 声明 `api.call`**（0.11.2 前漏声明，
  导致「撤回重发」静默失效；见[能力声明](#能力声明capabilities)）。

#### 错字「改了配置却没反应」怎么查

插件每次回复都会打两行日志，直接说明"到底有没有生成错字"：

```text
后处理接管参数：分段enable=True(宿主值) 长度=… 句数=… 条数=… 颜文字=… 超限全文=… | 错字enable=True(宿主值) error_rate=0.01(宿主值) min_freq=9(宿主值) tone=…(宿主值) word=…(宿主值) 纠正引用=… 引用概率=…(宿主值) 昵称=麦麦(宿主值)
后处理接管统计：宿主开关[分段=True 错字=True] 生效参数[错字总开关=True error_rate=0.01(源宿主值) …] → 抽到纠正建议=1 句、真出现错字=1 句、宿主原逻辑、共 2 段
```

| 日志现象 | 含义 / 怎么办 |
|---|---|
| `错字enable=False` | 宿主【启用错别字】关着，或 `[chinese_typo] enable = "关闭"` |
| `抽到纠正建议=0`、`真出现错字=0`（很多轮都这样） | 错字压根没生成：`error_rate` 太小、`min_freq` 太高，或 `depends-data/char_frequency.json` 异常 |
| `真出现错字>0` 但段数没涨 | 宿主那半句"抽到建议 → 50% 整句替换成正确句"的门控生效（正常行为） |
| 参数行写 `(插件值)` | 该值来自插件配置（不是跟随宿主），宿主配置改了也不会变（统计行写成 `(源插件值)`） |
| 额外一行「以下宿主镜像项由插件指定、与宿主当前值不同（想重新跟随宿主就把它们清空）」 | 插件里那几个值会盖住宿主；想重新跟随宿主就把它们**清空** |
| 统计行多出 `、整词替换跳过=N(累计)` | 撞上了整词同音替换的组合数上限（宿主在这里会卡死），本次跳过该词的整词替换 |

### `[chinese_typo_weights]` 错别字 · 纠正方式权重

`weight_correction_direct`（`1`）/ `weight_correction_quote`（`4`）/ `weight_correction_recall`（`1`）/
`weight_correction_none`（`1`）。

`last_correction_enabled`（默认 `false`）：**只决定「最后纠正」是否加入权重池**；加入后由
`weight_correction_last`（默认 `1`）决定占比。

### `[response_splitter]` 分段

| 字段 | 默认 | 说明 |
|---|---|---|
| `takeover` | `true` | 宿主【启用回复后处理】关闭时，由插件以与宿主一致的原逻辑接管**错字注入 + 分段 + 分段打字**；宿主开着该开关时插件不介入 |
| `enable` | `跟随宿主` | 宿主【启用回复分割】：把过长回复拆成多条发送。跟随宿主 / 开启 / 关闭 |
| `max_length` | 留空 | 宿主【单条最大长度】：超过就回退默认回复 |
| `max_sentence_num` | 留空 | 宿主【单条最大句数】 |
| `max_split_num` | 留空 | 宿主【最多分割条数】 |
| `enable_kaomoji_protection` | `跟随宿主` | 宿主【保护颜文字】：尽量避免把颜文字从中间拆开 |
| `enable_overflow_return_all` | `跟随宿主` | 宿主【超限保留全文】：句子太多时直接保留完整回复，不再强行截断 |
| `typing_speed` | 留空 | 宿主【打字速度】：模拟打字等待时间；`0` 最快，`1` 默认，`2` 更慢。留空 = 交给宿主按它自己的速度等待 |
| `fallback_nickname` | 留空 | 宿主【bot · 昵称】：只用于复刻「回复过长/句子太多时回退默认回复」的文本（「<昵称>不知道哦」） |
| `yield_to_other_plugins` | `true` | **让位给其它插件**：别的插件已认领这轮文本后处理（`skip_post_process=True`，例如智能分段插件命中预分段缓存）时把分段让给它；`@/引用`、文本规则、表情包照常。关掉表示本插件无条件优先 |
| `max_segments` | `0` | 分段条数上限，超过就退化成换行拼接一条（防刷屏）；`0` = 不额外限制，完全沿用宿主【最多分割条数】 |
| `wait_timeout_seconds` | `30` | planner 请求与新入站消息等待本轮补发完成的秒数（超时放行，不阻塞宿主；`0` = 不等待） |

> **没有**「分段多条发送」「分段模拟打字」这两个开关：它们本来就是宿主的
> 分段功能，插件接管时**照宿主那样**每段一条消息、按宿主的【打字速度】模拟打字——
> 要关掉分段就关 `enable`，要关掉打字就把 `typing_speed` 设 `0`。

#### 发送形态（复刻宿主分段发送）

- **第 1 段**：仍走宿主原生发送链 → 引用位置、写库、平台行为、Maisaka 历史同步全部原生；
- **第 2..N 段**：插件按顺序补发（`ctx.send.text(...)`），与宿主分段循环的 `typing=index>0`、
  历史同步口径一致；
- **更正段**（`quote_previous`）：插件给该条发送注入引用指向上一段；
- **首段发送失败**：整轮放弃，不会出现"只发出尾巴"；
- **时序守卫**：补发期间 planner 请求与新入站消息会先等待补发完成（超时放行）。
  两个等待处理器（`chat.receive.before_process` 与 `maisaka.planner.before_request`）
  注册的 `timeout_ms` 是 **65000（65 秒）**，**比宿主全局默认的 60 秒更长**——这是
  有意为之：`wait_timeout_seconds` 默认 30 秒，把 Hook 级上限设得更宽，才能让
  "等补发"由配置而不是由 Hook 超时来决定（Hook 超时会让宿主直接放行并记一条告警）。
  **代价要讲清楚**：极端情况下（补发卡住且 `wait_timeout_seconds` 调得很大）这个
  blocking 处理器最多会让**新入站消息**的处理等 65 秒。不想承担就把
  `wait_timeout_seconds` 调小或设 `0`（完全不等待）。

**与宿主原生的差异（纯插件路径固有限制，3 处）**：

1. `reply` 工具结果里的 `reply_segments` / `combined_reply_text` / `track_reply_effect` 只反映第 1 段；
2. 补发分段不带表情附件（`ctx.send.text` 不支持 `selected_expressions`，宿主原生每段都带）；
3. 若 planner prompt 在补发完成前就已构建，该轮上下文仍只含首段——插件用"等待 + 历史同步"尽量规避
   （需要绝对一致时请保持框架开关开启）。

#### 打字速度是怎么复刻的（`typing_speed`）

留空 = 把 `typing=True` 交给宿主，由宿主按它自己的速度等待；填了值 = 插件按**与宿主逐项一致的公式**
自己等待并传 `typing=False`（免得宿主再等一遍）：中文 `0.3s`/字、其它 `0.15s`/字符，乘速度系数；
`0` = 不等待。**两个宿主细节照抄**：① 宿主 docstring 写的 `0.2/0.1` 是过时注释，真实默认值是
`0.3/0.15`；② 整条只有 1 个汉字时宿主提前返回固定的 `0.3×3+0.3 = 1.2s`，既不乘速度系数也不受
`≤0` 影响。只作用于插件补发的分段（第 1 段由宿主发送链发出，宿主分段循环里首段本就是 `typing=False`）。

### `[emoji_after_reply]` 回复后表情包

planner 通过 `maisaka.reply.before_post_process` 激活一轮回复后，插件用
`send_service.after_send` 观察本轮全部出站消息（多分段也覆盖）。当出站消息静默
`quiet_seconds` 秒且本轮没有出现表情包组件时，按 `probability` 概率抽取一张：

- `emotion` 留空 → 随机抽取；
- `emotion` 填情绪标签（即表情包描述/情绪词，如 `开心`）→ 按情绪抽取，取不到就跳过。

**planner 自己打算发表情时不会补发（0.7.0）**：宿主工具调用按模型输出顺序串行
执行，而 `send_emoji` 内部还要跑视觉子代理选图（常需数秒到数十秒），因此
"planner 已经发表情"往往晚于出站静默判定——只看已发出的消息会漏判并多发一张。
插件改为在 `maisaka.planner.after_response` 里读本轮 `output_items` 的工具调用名，
命中 `planner_emoji_tools` 即打标记（有效期 `planned_emoji_ttl_seconds`），本轮跳过
补发；planner 的表情真的发出后标记立即清除。默认包含 `send_emoji` 与贴表情工具
`emoji_like`/`emoji_like_list`（贴表情同样表达情绪，不该再补一张）；留空则关闭该判定。
该判定依赖宿主输出项快照中 `FunctionCallItem.tool_call.func_name` 的形态（形态在
MaiBot 1.2.5 源码里核对，插件本体在 1.2.3 上实跑）：形态不匹配时静默退化为"只看已发出的出站消息"，不会误抑制。

**回复轮只有在真的发出消息后才算成立（0.7.0）**：`first_send_timeout_seconds` 秒内
没等到本轮出站消息（发送失败、被其它插件在 `send_service.before_send` 中止）即丢弃
该轮，避免之后**任何来源**的出站消息（其它插件的命令响应、主动问候、转发插件）
被当成"本轮回复的输出"而触发补发。

`round_window_seconds` 限制一轮记录的最长存活时间。planner 先发表情后回复
（15 秒内）时同样不会补发。发送前统一受聊天流表情冷却约束（见下节）。

> 补发判定完成后轮记录**不会**立即删除（0.7.1）：只标记 `emoji_done`，记录继续供
> "引用回复接管"判定"同一轮只抽取一次"用，避免后缀分段（分段间隔常超过
> `quiet_seconds`）被当成新的回复逐条追加引用。


**`[emoji_after_reply]` 字段表**（默认值 / 说明与 `config.toml` 行尾注释、WebUI 提示同源）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `True` | planner 这轮没发表情包时，按概率补发一个。 |
| `probability` | `0.2` | 补发概率（0~1）。 **取值范围** `0~1`。 |
| `emotion` | `''` | 补发时优先挑这个情绪标签的表情包；留空表示随机。 |
| `quiet_seconds` | `4.0` | 群聊安静这么久才考虑补发（避免打断热火朝天的聊天）。 **取值范围** `0.5~`。 |
| `round_window_seconds` | `90.0` | 判定「同一轮回复」的时间窗。 **取值范围** `1~`。 |
| `first_send_timeout_seconds` | `20.0` | 等首条消息出站的最长时间。 **取值范围** `0~`。 |
| `planner_emoji_tools` | `['send_emoji', 'emoji_like', 'emoji_like_list']` | planner 调用了这些工具就认为它自己发过表情包，本轮不补发。 |
| `planned_emoji_ttl_seconds` | `90.0` | 上面那个标记的有效期。 **取值范围** `5~`。 |

### `[emoji_follow]` 表情包跟风

通过 `chat.receive.after_process` 统计群聊中连续入站表情包条数（中间出现任何
非表情包消息即重新计数；系统通知消息如戳一戳不参与计数也不会打断连击），
达到 `threshold` 后按 `probability` 概率跟发一张：

| `mode` | 行为 |
|---|---|
| `same` | 抽取与最后一个表情包描述相同情绪的一张（描述缺失则跳过） |
| `specified` | 抽取 `emotion` 配置的指定情绪 |
| `random` | 纯随机抽取 |

**不会跟自己的表情包（0.7.0）**：宿主没有 self 过滤（`ignore_self_message` 是
适配器侧行为），而 NapCat/SnowLuma 出站表情按 `image/sub_type=1` 下发、入站又会被
判为 emoji 组件——适配器关闭"忽略自身消息"时，bot 自己刚发的表情会回环成入站
表情并凑满连击。`ignore_self_messages`（默认开）按 `additional_config` 的
`self_id`/`platform_io_account_id` 与发送者比对，剔除 bot 自身消息与 `*injected*`
合成记录（既不计入、也不打断连击、也不进含义缓存）。
`streak_window_seconds`（默认 120）控制连击保鲜：距上一条入站表情超过该时长即重新
计数（旧版固定为 600 秒，安静群里前后相隔十分钟的表情也会被算作连击）。

`cooldown_seconds` 已移除；触发一次后计数立即归零（概率或冷却落空同样归零，
需重新攒满 `threshold` 条才会再次尝试）。发送前统一受聊天流表情冷却约束（见下节）。
仅群聊生效。


**`[emoji_follow]` 字段表**（默认值 / 说明与 `config.toml` 行尾注释、WebUI 提示同源）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `True` | 群友连着发表情包时跟着发一个。 |
| `mode` | `'same'` | 跟发方式：same 同款 / specified 指定情绪 / random 随机。 |
| `threshold` | `3` | 连续多少条表情包后触发。 **取值范围** `1~`。 |
| `probability` | `0.5` | 触发后的跟发概率（0~1）。 **取值范围** `0~1`。 |
| `emotion` | `''` | mode=specified 时用哪个情绪标签。 |
| `ignore_self_messages` | `True` | 统计连击时不数 bot 自己的表情包。 |
| `streak_window_seconds` | `120.0` | 连击保鲜时间，超过就重新计数。 **取值范围** `10~`。 |

### `[emoji_cooldown]` 聊天流表情包冷却

同一聊天流内，**任意来源**的表情包消息只要发出（planner 的 `send_emoji` 工具、
其它插件、本插件自身）即进入 `seconds` 秒冷却，冷却期内本插件不会主动发送表情包
——"回复后表情包"与"表情包跟风"共用同一冷却；本插件自己发送的表情包同样会刷新
冷却起点，因此不会出现"补发一张又跟发一张"的连击。设为 `0` 可关闭冷却。

`count_unstored`（默认开，0.7.0）：`storage_message=False`（只发不入库）的出站
表情同样计入冷却。转发类等插件常用该参数发消息，不计入时本插件会被判为"本轮
没有表情"，紧接着在别人的表情后面再补/跟一张。

> 行为变更（0.4.0）：原 `[emoji_after_reply] cooldown_seconds` 与
> `[emoji_follow] cooldown_seconds` 两个独立冷却已移除，统一由本节控制。


**`[emoji_cooldown]` 字段表**（默认值 / 说明与 `config.toml` 行尾注释、WebUI 提示同源）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `seconds` | `300.0` | 同一会话里两次表情包之间的最小间隔（秒）。 **取值范围** `0~`。 |
| `count_unstored` | `True` | 没能入库的表情包是否也计入冷却。 |

### `[text_rules]` 文本替换规则

| 字段 | 默认 | 说明 |
|---|---|---|
| `rules` | 空 | 规则列表（见下） |
| `apply_scope` | `reply_flow` | `reply_flow`=只作用于 planner 拉起的本轮回复自己发出的消息；`all_outbound`=作用于 bot 的全部出站文本（含其它插件直接发出的消息） |

规则列表，每条一个字符串，两种格式：

- `"shit"replace"filter"` —— 把消息文本中的 `shit` 全部替换为 `filter`
  （替换词留空等价于删除该词）；
- `"草"cover"你好"` —— 消息文本包含 `草` 时，整条文本覆盖为 `你好`。

解析是严格的：关键词非空、ASCII 双引号、小写 `replace`/`cover` 关键字；**格式
错误的条目在加载/热更新时记录日志并忽略**。多条规则的应用顺序：先依次应用全部
`replace`（全量替换），再按配置顺序检查 `cover`，第一条命中的 `cover` 直接覆盖
整条文本。规则只作用于纯文本（text 组件），不影响表情包、图片等组件。

**作用范围（0.9.0）**：宿主的出站 Hook 载荷里**没有来源字段**，无法直接区分
"bot 的回复"与"其它插件用 `ctx.send.*` 直接发出的消息"，因此默认 `apply_scope="reply_flow"`：
只有**本插件登记的回复轮**（`maisaka.reply.before_post_process` 标记，即 planner 拉起的
正常回复流程）自己发出的消息才应用规则，判定条件是「会话存在本轮回复轮且已看到过
本轮出站消息」+「该消息指向本轮目标消息，或引用了本轮已发出的某条消息（错别字更正段
会引用上一段）」。因此其它插件的命令响应/提示、宿主命令提示等**都不会被改写**。
需要像旧版一样覆盖 bot 全部出站文本时，改为 `all_outbound`（此时确实会改写到其它
插件直接发出的文本）。

### `[emoji_meaning]` 表情包含义库（试验性，默认关闭）

> ⚠️ **试验性功能，默认不开启**：依赖宿主视觉模型任务的输出质量，token 消耗随
> 表情库规模增长，且以描述标签为关联键存在固有的合并折衷。建议先在小范围会话
> 观察 `已收录表情包含义` 日志的质量后再长期开启。位于配置页末尾。

宿主里每张表情包都带一个视觉模型生成的**情绪标签描述**（`images.description`），
replyer 在上下文里看到的表情包就是这个标签（`[表情包: 描述]`）——只有"大致的情绪"，
看不到图片的实际内容。本插件维护一份**插件自有 SQLite 库**（位于插件数据目录
`emoji_meanings.db`），以描述标签为关联键，补录由视觉模型生成的"准确内容"
（画面内容 + 梗含义），并在 replyer 请求前注入上下文。

**注入范围（0.9.0）**：`inject_request_types`（默认 `["maisaka.replyer"]`）限定只注入
**planner 拉起的正常回复流程**（宿主回复工具取生成器时用的就是该 `request_type`）：

- 插件自己拉起回复生成（`generator_api`、`plugin.<插件id>` 等 `request_type`）→ **不注入**；
- 插件激活 planner（主动触发发言、注入上下文等）后由 planner 拉起的 reply →
  仍是 `maisaka.replyer`，**照常注入**；
- 留空表示不过滤（全部注入），写 `*` 表示全部注入。

**两条补录通道**：

| 通道 | 触发方式 | 说明 |
|---|---|---|
| 周期补录 | 每 `scan_interval_seconds` 扫描一次宿主表情库（`database.query`） | 把还没有含义的描述标签加入队列，覆盖插件安装前就存在的表情包 |
| 即时补录 | 宿主 `emoji.register.after_build_description` 钩子 | 新表情包完成描述生成的瞬间入队，覆盖安装后新入库的表情包 |

含义生成由后台工作循环完成（每 `worker_interval_seconds` 一轮、每轮最多
`batch_size` 条），取图方式为 `emoji.get_random` **分批抽样**：每轮随机抽 k 张
（`emoji.get_all` 会把整个表情库 base64 塞进单个 RPC 帧，大库超过宿主 16MB 帧
上限，故弃用；k 随帧错误自动减半、随命中恢复），命中队列描述的图片立即交给
`llm.generate` 的 `model_task` 视觉任务生成含义；未命中的描述留队等待后续轮次
（随机抽样对全库均匀覆盖，新表情几分钟内必然命中）。单条生成失败最多重试
`max_attempts` 次，单条长度截断到 `max_meaning_length`。生成提示词可用
`generation_prompt` 覆盖（留空用内置）。

收敛与复活规则：扫描只补录"已注册、有文件、未禁用"的表情（与宿主运行库对齐——
`emoji.get_random` 只会抽到这些，Images 表里描述已生成但从未注册成功的行不会被
反复空扫）；一条
含义连续失败 `max_attempts` 次后进入放弃名单，周期扫描不再重试，只有该表情包
重新注册（重新触发描述生成钩子）才会复活重试。

**上下文注入**（`inject_enabled`）：replyer 请求前（`maisaka.replyer.before_request`），
插件收集**被回复消息**与**会话近期消息**中出现的表情包描述，从含义库查出准确内容，
注入 `extra_prompt`（最多 `inject_max_emojis` 条），让 replyer 准确理解表情包语境：

```text
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


**`[emoji_meaning]` 字段表**（默认值 / 说明与 `config.toml` 行尾注释、WebUI 提示同源）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `False` | 启用表情包含义库（试验性）：为表情包生成并维护准确含义。 |
| `inject_enabled` | `True` | 把表情包含义注入 replyer 上下文。 |
| `inject_request_types` | `['maisaka.replyer']` | 注入到哪些请求类型（如 replyer）。 |
| `model_task` | `'vlm'` | 用哪个模型任务生成含义。 |
| `generation_prompt` | `''` | 生成含义用的提示词。 |
| `max_tokens` | `256` | 生成含义时的 max_tokens。 **取值范围** `32~4096`。 |
| `scan_interval_seconds` | `1800.0` | 多久扫一遍库、把缺含义的表情包入队。 **取值范围** `60~`。 |
| `worker_interval_seconds` | `30.0` | 生成循环的间隔。 **取值范围** `5~`。 |
| `batch_size` | `5` | 每轮生成几条含义。 **取值范围** `1~50`。 |
| `max_attempts` | `3` | 单条含义生成失败后的重试上限。 **取值范围** `1~20`。 |
| `max_meaning_length` | `160` | 单条含义的最大字数。 **取值范围** `20~1000`。 |
| `inject_max_emojis` | `5` | 单次往上下文里注入多少条含义。 **取值范围** `1~20`。 |

### WebUI 显示与翻译（0.6.1；提示文本 0.11.1）

- 所有配置字段都有中文短标签（`label`），不再显示英文变量名；详细说明悬停在
  字段标签上查看。
- 插件声明 `supported_locales: ["zh-CN", "en"]`，并为每个配置节（标题/描述）与
  字段（标签/提示）内置英文 i18n——WebUI 切换到英文界面时配置页整体显示英文，
  中文界面与英文界面之外的语言回退中文。
- **配置页的说明文字（0.11.1 修复）**：宿主的插件配置页只渲染字段的 `hint`，不读
  `description`。本插件覆写了 `get_webui_config_schema()`，把每项的简短注释
  （`x-toml-comment`，也就是 `config.toml` 里那句行尾中文注释）同时作为 `hint` 交给界面，
  所以**配置文件和 WebUI 上的说明是同一份文案**，改一处两处同步；英文界面用
  `i18n.en.hint`，未命中时回退到这句中文。

## 与框架的分工

- **回复后处理（错别字 + 分段 + 打字 + 更正段引用）**：0.10.0 起**完整复刻**，见
  [`[response_splitter]` 分段](#response_splitter-分段)。框架开关
  `response_post_process.enable_response_post_process` **关闭**时由插件接管，按宿主同款逻辑分段后
  **每段一条消息**发出（首段走宿主原生链路，其余由插件补发）；开关**开启**时插件完全不介入，
  由框架原生处理。两种模式二选一，不会重复处理。
- 引用回复：框架 `chat.reply_style.enable_reply_quote` 开启时插件完全不介入。
- 两个接管模块都额外要求关闭 `experimental.enable_rich_reply`（丰富回复），否则模块静默——
  原因与判定逻辑见[模块与前置条件](#模块与前置条件重要)。
- 表情包相关功能与文本替换规则不依赖任何宿主开关，始终按插件配置工作。

## 与其它插件共存

### 与「智能分段插件」(`saberlights/smart_segmentation_plugin`)

两者都会介入"分段"这件事，0.10.4 起按**让位机制**分工，不会互相压制、也不会重复发送：

| 场景 | 谁分段 | 本插件负责 |
|---|---|---|
| 宿主总开关**开启** | 该插件（它的 `skip_post_process=True` 让宿主不再处理；本插件接管本就静默） | `@/引用` 抽取、文本规则、表情包 |
| 宿主总开关**关闭** + 该插件命中（切好段） | **该插件**（本插件让位：不改写正文、不登记补发） | 同上 |
| 宿主总开关**关闭** + 该插件未命中/未安装/失败 | **本插件**（宿主复刻分段器兜底） | 同上 + 分段/打字/更正段引用 |

- 让位判据是**通用的** `skip_post_process=True`（谁先认领谁干活），不针对某个插件硬编码；
  可用 `[response_splitter] yield_to_other_plugins=false` 关掉让位（本插件无条件优先）。
- 为什么不会重复发送：两套补发都依赖**各自的登记缓存**——本插件让位时不会登记；
  反之本插件接管时改写了 `response`，对方的"原文哈希"必然失配、它的补发也不会触发。
- 为什么 `@/引用` 永远安全：两个插件的补发分段都用 `ctx.send.text` 发出、**不带
  `reply_message_id`**，本插件的 `@/引用` 抽取只作用于"本轮指向目标消息的首条文本"，
  因此后排分段不可能被加上 `@`。
- 想让**文本替换规则**也覆盖对方补发的分段：把 `[text_rules] apply_scope` 设为
  `all_outbound`（默认 `reply_flow` 只认本插件自己参与的发送，认不出对方的补发分段）。

### 与其它出站类插件

- 别的插件若在同一 Hook 主动设置 `skip_post_process=True`，本插件一律让位（同上）；
- 别的插件已设置 `set_reply=True` 的发送，本插件不重复处理（不抢引用）；
- 其它插件用 `ctx.send.*` 直接发出的文本，`apply_scope="reply_flow"` 下不会被本插件改写。

## 能力声明（capabilities）

manifest 声明 **12** 项能力，与代码里的实际调用一一对应（审核时用 AST 双向对账过）：

| 能力 | 用途 |
|---|---|
| `api.call` | 走适配器公开 API 撤回消息（「撤回重发」纠错分支） |
| `config.get` | 读宿主配置（模块前置条件判定、`留空 = 跟随宿主`） |
| `message.get_by_id` | 取被回复消息（@ 目标、目标时间戳用于过旧判定） |
| `message.get_by_time_in_chat` | 统计"目标之后已有多少条消息" |
| `emoji.get_by_description` / `emoji.get_random` / `emoji.get_count` | 表情包抽取、含义库抽样与统计 |
| `database.query` | 读宿主表情库（描述标签）；读宿主 `[chinese_typo]` 等配置 |
| `database.delete` | **撤回后彻底清理**时删掉宿主消息库里那条原消息记录 |
| `llm.generate` | 表情包含义的视觉模型生成 |
| `send.emoji` | 补发 / 跟发表情包 |
| `send.text` | 补发第 2..N 段与错字更正段 |

> **`api.call` 必须在 manifest 里声明**：宿主 `plugin_runtime/host/authorization.py` 的
> 免声明白名单**只有** `api.replace_dynamic` 一项，其余能力一律按 manifest 令牌放行。
> 0.11.1 及更早漏声明了 `api.call`，宿主返回 `E_CAPABILITY_DENIED` 被插件吞成 debug 日志，
> 结果是**「撤回重发」永远静默退化成 `recall_fallback`**（0.11.2 修复）。

插件**不访问宿主内部模块**（无 `import src.*`），也**不直接读写宿主的数据库文件或文件系统**；
唯一的宿主数据访问走公开能力代理：

- 读库走 `database.query`，删自己刚撤回的那条记录走 `database.delete`（空值校验 + 开关门控 + 异常兜底）；
- 读宿主配置有两处：宿主 `bot_config.toml`（**只读**，用于"留空 = 跟随宿主"与模块前置条件判定）
  和 `config.get` 能力；
- 撤回走 `api.call` 调适配器公开动作（`adapter.napcat.message.delete_msg`）。
- 自有数据（表情包含义库 `emoji_meanings.db`、`config.toml`）落在 `ctx.paths.data_dir` 与插件目录内。

## 开发

- **模块结构**（0.10.1 起）：

  | 文件 | 职责 |
  |---|---|
  | `plugin.py` | 配置模型、生命周期、共享基础设施（回复轮记录、文本规则、表情包功能），组合下面两个 mixin |
  | `modules/requirements.py` | 各模块的宿主前置条件与可用性判定（纯逻辑，可离线单测） |
  | `modules/post_process_takeover.py` | 后处理接管：Hook 1~5 + 多段发送 + 补发缓存 |
  | `modules/quote_takeover.py` | 引用回复接管：权重抽取 + 过旧规则 + 同一消息只引用一次 |
  | `post_processing.py` | 宿主后处理算法逐行复刻（纯逻辑，可离线单测） |
  | `emoji_meanings.py` / `text_rules.py` | 表情包含义库存储与提示词 / 文本规则解析 |

  两个接管模块以 **mixin** 形式被插件类继承——SDK 用 `dir(instance)` 收集组件，继承来的
  `@HookHandler` 一样会被注册；共享状态（`self._reply_rounds` 等）通过 `self` 访问。

- 新增/调整"需要宿主关闭某能力"的模块时，只需改 `modules/requirements.py` 的
  `MODULE_REQUIREMENTS`，门控与启动日志会自动跟随。
- `post_processing.py`（后处理复刻）与 `text_rules.py`（文本规则解析）为纯逻辑模块（不依赖
  SDK），可离线单测；后处理复刻依赖 `jieba` / `pypinyin`（宿主 venv 自带），缺失时只有
  "后处理接管"降级，插件其它功能照常工作，加载时日志会给出明确报错。
- 修改配置用 WebUI 热重载即可；改 `_manifest.json`（能力声明）需完整重启 MaiBot。

## 参考实现与出处

- **派生自 MaiBot**（<https://github.com/Mai-with-u/MaiBot>，**GPL-3.0**）：`post_processing.py`
  复刻并**部分逐字复制**了宿主 `src/chat/utils/utils.py`（`process_llm_response_segments`）与
  `src/chat/utils/typo_generator.py`（`ChineseTypoGenerator`）。这是本插件按 GPL-3.0-or-later
  发布的原因，详见[作者与许可](#作者与许可)。**与宿主唯一有意不同的一处**：整词同音替换的
  候选组合数加了一道硬上限（宿主穷举 `itertools.product` 无上限，长词会指数爆炸卡住线程），
  超限时跳过该词的整词替换，并在日志里记账。
- **参考了**[saberlights/smart_segmentation_plugin](https://github.com/saberlights/smart_segmentation_plugin)
  （作者久远，GPL-3.0-or-later）的**多段发送思路**（`send_service.after_build_message` 登记待补发
  → `send_service.after_send` 用 `ctx.send.text(typing=…)` 补发 → `maisaka.planner.before_request` /
  `chat.receive.before_process` 时序守卫）；该部分的实现为**独立编写**：无成段代码复制
  （最长连续相同代码为 3 行通用惯用法），同名函数体相似度 ≤20.7%，重合的长行均为宿主公开接口名
  与载荷键名，文档/注释无重合。相似性取证脚本与结论见
  `smart_segmentation_plugin-main/LICENSE-审查-协议义务分析.md` 与同目录 `check_license_similarity.py`。

## 作者与许可

- **作者**：cateye（<https://github.com/cateyemizuki>），仓库
  <https://github.com/cateyemizuki/cateye_better_post-processing>。
- **许可证**：**GPL-3.0-or-later**（见 [`LICENSE`](LICENSE)）。

> 为什么是 GPL 而不是 MIT：本插件的 `post_processing.py` **派生自 MaiBot**
> （<https://github.com/Mai-with-u/MaiBot>，**GPL-3.0**）的 `src/chat/utils/utils.py` 与
> `src/chat/utils/typo_generator.py`——分段、颜文字保护、长度/句数守卫、错字候选与概率等逻辑与上游
> **逐行一致**，其中相当一部分是**逐字复制**（含正则字面量与中文行内注释）。按上游同一协议发布是
> 分发该派生代码的前提。**因此本插件整体按 GPL-3.0-or-later 提供**：你可以自由使用、修改、再分发，
> 但再分发时必须同样开源、保留版权与许可声明。若你只需要 MIT 的代码，请勿使用本插件。

## 致谢

- 特别感谢 **[saberlights/smart_segmentation_plugin](https://github.com/saberlights/smart_segmentation_plugin)**
  （作者**久远**）——本插件的**多段发送机制**参考了它的思路，0.10.4 起又靠它与本插件的
  **让位机制**和平共存（见[与其它插件共存](#与其它插件共存)）。没有它先把"接管宿主分段"这条路
  蹚出来，本插件不会在 0.10.0 去逐项复刻宿主的分段链路。**感谢久远与这个项目。**
  如果你的需求是"更主动、更激进的分段"，请直接使用它——本插件的分段只在宿主总开关关闭时兜底。
- 感谢 **MaiBot**（[Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot)）与其插件体系
  （`maibot_sdk`）：本插件的分段与错别字逻辑派生自宿主源码，全部宿主数据访问都走宿主公开能力。
