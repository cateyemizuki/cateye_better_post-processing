# 更新日志

> **留档说明**：0.9.0 及更早的版本没有逐条留档，下面带「回溯」标记的条目是按代码注释与 README
> 里的版本标注整理出来的；**0.9.1 起为逐条记录**。

---

## [0.11.3] - 表情包跟风：连击判定收紧为"中间不能有任何非表情包内容" + 默认 2 条触发

用户要求：**"跟风连发表情包的判断为要求当前聊天流连续发了 x 条表情包，确保要连续，
中间插入任何一个非表情包信息都不激活，且默认的 x 为 2"**，并且**先确认逻辑正确、再改默认值**。
按这个顺序做的：先用探针把 12 种入站形态下的实际行为打成表，找出漏判，修完复跑同一张表对照，
最后才动默认值。

### ① 修逻辑：三处"该打断却没打断"（探针实测）

改动前用探针（`scratch/probe_emoji_follow_streak.py`）跑出来的计数轨迹：

| 场景 | 改前 | 改后 |
|---|---|---|
| 表情包 → **系统通知（戳一戳）** → 表情包 → 表情包 | `1 → 1 → 2 → 3`（**通知被整个跳过，等于不算插入**） | `1 → 0 → 1 → 2`（打断） |
| 表情包 → **bot 自己的文本** → 表情包 | `1 → 1 → 2`（self 过滤顺带跳过） | `1 → 0 → 1`（打断） |
| 表情包 → **其它插件的注入记录** → 表情包 | `1 → 1 → 2`（同上） | `1 → 0 → 1`（打断） |
| 表情包 → 群友文本 / 图片 | `1 → 0 → 1`（本来就对） | 不变 |
| 表情包 → **bot 自己的表情包（回环）** → 表情包 | `1 → 1 → 2` | **不变** |

根因：原来的实现在取 `session_id` 之前就先 `if is_notify: return`，而 self / 注入消息也是
"提前 return"——它们**既不计数也不打断**，于是在"连击"里等同于不存在。

改后的判定规则（`handle_inbound_emoji_observer` 的 docstring 重写了，代码里逐条有注释）：

1. **任何非表情包入站内容一律打断连击**：群友的文本 / 图片 / 语音、**系统通知**、
   **bot 自己的文本**、**其它插件的注入记录**都算。
2. **只有两类不打断**：① bot 自己 / 注入的**「表情包」回环**——它本身就是表情包，不是
   "插入的非表情包信息"，而且让它打断会导致本插件跟发的那张把自己的连击清掉；
   ② 无法归属会话的载荷（没有 `session_id`），既不能计数也不知道该清谁的连击。
3. 含表情包组件的**混合消息**（「哈哈哈 + 表情包」）按"发了表情包"处理，**继续**连击
   （用户确实发了表情包，且这不属于"两条消息之间插入了别的东西"）。

### ② 改默认值：`threshold` 3 → 2

顺带把这一项从"一句话糊过去"改成写清判定规则的说明与行尾注释（含英文 i18n）。

**老配置的迁移**：`threshold` 这一项宿主一定会写成旧默认值 `3`，如果原样带进新默认值，
升上来还是 3、会让人以为"改了没生效"。所以 `_legacy_config_values()` 里加了一条：
**值恰好等于旧默认 `3` 时丢弃它**，让新默认 `2` 生效；用户改过的其它值（如 `1`、`5`）
原样保留。真想要 `3` 的话手改一下即可。

> 取舍说明：无法区分"用户显式设成 3"与"从来没人动过、就是旧默认值"，这里选择让新默认生效
> ——"没改过"是绝大多数情况。若你确实想要 3，把 `config.toml` 的
> `[emoji_follow] threshold` 改回 `3` 即可。

### ③ 验证

- 新增 `归档测试\20260921_better-post-processing_0.11.3@跟风连击判定\`：**34 项断言**
  （12 种入站形态的计数轨迹、两类例外、混合消息、阈值触发与触发后清零、`streak_window`
  超时复位、迁移的四种取值、文档与配置文案一致性）。
- 0.10.2–0.11.3 **九套全绿**。

## [0.11.2] - 三路审核修复：许可改为 GPL / 两个真机缺陷 / 文档重排与致谢

本版**没有新增功能**，全部是审核发现的问题修复。三路只读审核（功能一致性 / 插件规范 / 安全）
共出 21 + 7 + 30 条问题，下面按"会不会影响你"排序。

### ① 阻塞级真机缺陷（两个，都不是新引入的，是早就坏了）

- **`[response_splitter] typing_speed` 是死字段**（0.11.1 及更早）：`_typing_speed_override()`
  误把**宿主** `bot_config.toml` 的节名 `response_post_process` 当成**插件**配置节传给了
  `_mirror_raw()`（插件里根本没有这个节）→ 恒返回 `None` → **你填什么都不会生效**，
  打字等待永远由宿主按它自己的速度执行，连"设 0 关掉打字"都做不到。
  README 还专为它写了一整节（"填了值由插件按同一公式自己等待"），属于"文档描述了不存在的行为"。
  现已改为正确的插件节名 `response_splitter`，并补了专门的回归断言。
- **`api.call` 未在 manifest 声明**：宿主 `plugin_runtime/host/authorization.py` 的**免声明白名单
  只有 `api.replace_dynamic` 一项**，其余能力一律按 manifest 令牌放行。漏声明时宿主返回
  `E_CAPABILITY_DENIED`，被插件吞成 debug 日志 → `_recall_message()` 恒 `False` →
  **「撤回重发」纠错分支永远静默退化成 `recall_fallback`**（错字版与修正版两条都留在群里）、
  `recall_purge` 也永不执行。已声明 `api.call`，并把源码里"免声明"的错误注释与 CHANGELOG
  原文一并更正（那句话正是会让人**改回缺陷**的隐患）。

### ② 许可：MIT → GPL-3.0-or-later（协议不兼容问题，必改）

安全审核用 difflib 取证：`post_processing.py` 与宿主 `src/chat/utils/utils.py` **逐字重合 214 行**
（占该文件 21.7%，17 处连续 ≥4 行、最长 14 行）；`kaomoji_pattern` 那条正则连**中文行内注释**
都与宿主一字不差（只差 `__import__("re")` 与 `re`）。而宿主 MaiBot 的 LICENSE 是 **GPL-3.0**——
MIT 发布一份 GPL 派生代码是协议不兼容的。现已：

- `LICENSE` 换成 GPL-3.0 全文；manifest `license` 改 `GPL-3.0-or-later`；
- README 新增「作者与许可」与「参考实现与出处」两节，写清**派生自 MaiBot** 与**参考了
  smart_segmentation_plugin 的思路**（后者为独立编写，取证见协议义务分析文档）；
- `post_processing.py` 头部加 GPL 来源声明，明确"请勿当作可单独按 MIT 使用的代码"。

### ③ 安全加固（审核中/低危项）

- **`_write_config_comments()` 可能改值**（安全审查中-2）：`generation_prompt` 是多行字段，
  朴素实现会把注释追加到三引号字符串**内部**。现在识别三引号多行块整段跳过，并在写盘前用
  `tomllib` 把**改前/改后**都解析一遍断言相等——一旦发现会改动值就**放弃写入**并记 warning，
  绝不改用户配置。
- **整词同音替换的组合数爆炸**（安全审查中-3）：宿主用 `itertools.product` **无上限**穷举，
  「中华人民共和国」约 11.5 亿、「社会主义现代化」约 5028 亿组合，命中 `word_replace_rate`
  就把工作线程占住几分钟到几小时。现在加了 20 万组合的硬上限，超限跳过**该词**的整词替换
  （逐字错字照常），并在「后处理接管统计」日志里以 `整词替换跳过=N(累计)` 提示。
  **这是与宿主唯一有意不同的一处**，README 已单独写明。

### ④ `stale_at_weight = 0` 现在真的"不再 @"

字段说明与 README 一直写 `0 = 不再 @`，但旧实现只把**纯 @** 权重清零，「引用＋@」仍保持原权重
（约 1/8 概率继续 @ 人）——典型的"设了没用"。现在**显式填 0** 时把 `quote_at` 一起清零；
**留空/正数**的行为完全不变（留空仍只把纯 @ 减半，不动 `quote_at`）。

### ⑤ manifest 补齐与文档规范化

- `dependencies` 补上真实硬依赖 `jieba >=0.42.1`、`pypinyin >=0.54.0`（此前是空数组，
  与 `post_processing.py` 的 import 不符）。已用宿主 `PluginDependencyPipeline` 实测：
  与主程序约束不冲突、已安装版本满足、**不会触发 pip 安装**。
- README 全面重排：新增**目录**、「取值范围与校验」（把 32 项 `ge/le` 约束列全——以前一处都没写，
  而超范围会被配置校验**直接拒绝**）、以及 6 个此前只有散文的板块的**字段表**（默认值/说明与
  `config.toml` 行尾注释、WebUI 提示同源）。
- 修正文档与实现相反的表述：`api.call` "免声明"、能力清单缺 `database.delete`/`api.call`、
  "不直接读写宿主文件系统"（实际会只读宿主 `bot_config.toml` 与 `depends-data/char_frequency.json`）、
  私聊节"与群聊同名同义"（私聊没有 `style_once_per_target`）、日志文案 3 处、失效锚点
  `#post_process-后处理接管`（`[post_process]` 节 0.11.1 已删）、"六个功能"（实际 7 项）、
  1.2.3/1.2.5 的说法统一定稿。
- 顺带把 `send_service.after_build_message` 等 Hook 写全、给 4 处示例代码块补语言标注。

### ⑥ 文档结尾新增「致谢」

感谢 **smart_segmentation_plugin**（久远）——多段发送机制参考了它，0.10.4 起靠让位机制和平共存；
也感谢 **MaiBot** 与 `maibot_sdk`。同时把「与智能分段插件共存」的细节保留在原处。

> **升级提示**：manifest 改过（能力声明与依赖），**必须完整重启 MaiBot**；`config_version`
> 自动升到 `0.11.2`，配置项没有增删，用户改过的值全部保留。

## [0.11.1] - 配置重新排版：按功能板块分（引用回复 / 错别字 / 分段）

按你的要求把配置面**重新排版**，不再区分"宿主提供的"还是"插件提供的"：

**① 板块按功能划分，标题不再标注「（宿主）」**

| 顺序 | 板块 | 内容 |
|---|---|---|
| 1 | `[plugin]` | 插件开关与配置版本 |
| 2 | `[quote_reply]` | 引用回复（接管 / 过旧判定 / 引用去重） |
| 3 | `[quote_reply_weights]` | 引用回复 · 权重 |
| 4 | `[quote_reply_private]` | 引用回复（私聊） |
| 5 | `[quote_reply_private_weights]` | 引用回复 · 权重（私聊） |
| 6 | `[chinese_typo]` | 错别字（宿主 `[chinese_typo]` 全部参数 + 纠正行为） |
| 7 | `[chinese_typo_weights]` | 错别字 · 纠正方式权重 |
| 8 | `[response_splitter]` | 分段（分割参数 + 打字速度 + 接管机制） |
| 9 | 其它功能 | `[emoji_after_reply]` / `[emoji_follow]` / `[text_rules]` / `[emoji_cooldown]` |
| 10 | `[emoji_meaning]` | **试验性**——试验性配置一律放最后（`BetterPostProcessingConfig` 里有注释写明） |

**② 接管板块按分类并入对应板块**

- `takeover` / `yield_to_other_plugins` / `max_segments` / `wait_timeout_seconds` 与
  打字速度 `typing_speed`、兜底自称 `fallback_nickname` → **分段板块**
- `correction_mode_enabled` / `correction_max_cjk` / `recall_fallback` / `recall_purge` 与
  宿主 `[chinese_typo]` 的参数 → **错别字板块**
- 旧的 `[post_process]`、`[host_override]`、`[response_post_process]`、`[bot]` 四个节**全部消失**

**③ 所有权重类参数另开"权重"板块，紧跟它的功能板块**

`weight_direct/quote/at/quote_at` 与 `stale_at_weight` → `[quote_reply_weights]`；
私聊两个权重 → `[quote_reply_private_weights]`；
`weight_correction_*` 与 `last_correction_enabled` → `[chinese_typo_weights]`。
功能板块里不再出现任何 `weight_*` 项。

**④ 旧配置自动迁移（不会丢用户设置）**

排版一变老键就"消失"了，宿主在版本升级时又是以**最新默认值**为骨架重建配置，直接升级会把用户改过的
权重/开关静默重置。因此新增 `migrate_legacy_config()`：

- `get_default_config()` 现在还会读插件自己的 `config.toml`、迁移成新排版、把这些值**带进默认值**，
  于是宿主的重建过程会原样保留它们；
- `normalize_plugin_config()` 里再兜一道（覆盖 WebUI 直接提交旧格式数据的场景）；
- 转换规则：老布尔 → 目标若是三态开关则变 `开启/关闭`（普通布尔保持布尔）；老 `-1` 哨兵 → 留空；
  0.11.0 的 `host_override` 三态 `host/on/off` → `跟随宿主/开启/关闭`；已删除的
  `multi_message`/`typing`/`correction_gate_probability` 直接丢弃；
- 迁移只在旧键存在时生效，且是幂等的。

**⑤ 修复：配置注释在 WebUI 里看不到**

上一版把"每项一句说明"写成了 `config.toml` 的**行尾注释**，但宿主的插件配置页**只渲染
`field.hint`**（`dashboard/src/routes/plugin-config.tsx` 读的是
`resolveLocalizedText(field.hint, …)`），根本不读 `description`，所以可视化界面上每项
只有一个名字、说明全是空白。现在覆写 `get_webui_config_schema()`，把每个字段的
`x-toml-comment` 同时补进 `hint`：

- 文案**只有一份**（`x-toml-comment`），`config.toml` 的行尾注释与 WebUI 的说明永远一致；
- 字段自带 `hint` 时不覆盖（英文界面走 `i18n.en.hint`，未命中回退到中文 `hint`）；
- 顺带给 `plugin.config_version` 补了英文 label/hint（它是隐藏项，之前英文说明是空的）。

**⑥ 措辞修正：「目标超时强制直发」→「目标超时后强制直接回复」**

原来的说法（"强制直发"/"强制直接回复"）没写清楚**引不引用**，容易误读成"以引用的方式发出去"。
字段 `stale_age_seconds` 的真实行为是：目标消息发出超过 N 秒后，本次回复**既不引用、也不 @**，
纯文本发出去（就是权重池里的「直接回复」）。现在把这条写进了 WebUI 标签、字段说明与行尾注释：
标签 `目标超时后强制直接回复（秒）`、注释「被回复的消息发出超过这么多秒后，本次回复**既不引用也不 @**
（就是「直接回复」）。0 = 不判定。」，代码注释与运行日志（`直接回复（不引用、也不 @）`）同步改了措辞。
只改文案，**行为与字段名都没变**。

## [0.11.0] - 配置改版：宿主已有参数照搬宿主 + 留空跟随宿主 + 每项注释

这一版**按你的四条要求重整了整个配置面**，几条老配置项被删掉（升级会自动迁移，旧键在归一化时丢弃）：

**① 宿主已有参数不再换名字、不再另起配置项，直接照搬宿主的 WebUI**

现在有四个镜像节，**节名、字段名、字段说明都跟宿主 `config/bot_config.toml` 一模一样**
（也就是宿主 WebUI 上显示的中文）：

| 插件节 | 宿主节 | 字段（同名） |
|---|---|---|
| `[response_post_process]` | `[response_post_process]` | `typing_speed` |
| `[response_splitter]` | `[response_splitter]` | `enable` / `max_length` / `max_sentence_num` / `max_split_num` / `enable_kaomoji_protection` / `enable_overflow_return_all` |
| `[chinese_typo]` | `[chinese_typo]` | `enable` / `enable_correction_quote` / `correction_quote_probability` / `error_rate` / `min_freq` / `tone_error_rate` / `word_replace_rate` |
| `[bot]` | `[bot]` | `nickname` |

旧的 `[host_override]`（`splitter_*` / `typo_*` 那套自造名字）**整体删除**。

**② `[post_process]` 只留"接管机制 + 宿主没有的分支"**

- 删掉 `multi_message`（分段多条发送）：宿主的"分段"本来就是每段一条消息，插件接管时照此复刻，
  不该有自己的开关——要关分段就关宿主的【启用回复分割】。
- 删掉 `typing`（分段模拟打字）：宿主的打字模拟由【打字速度】控制，`0` 就是不等；补发分段一律按
  宿主 `typing=index>0` 的口径带打字。
- 删掉 `quote_correction`：它就是宿主的 `chinese_typo.enable_correction_quote`，现在搬进
  `[chinese_typo]` 同名项。
- 保留 `takeover` / `yield_to_other_plugins` / `max_segments` / `wait_timeout_seconds`，以及
  **宿主没有的**「错字纠正方式」分支（`correction_mode_enabled` + 四个权重 + 最后纠正 +
  撤回兜底 + 超限不纠错 + 撤回后清理）。

**③ 只有一个错字概率**

删掉 `correction_gate_probability`（旧的「错字出现概率」）。错字出现的总频率**只由宿主
【单字错字概率】`[chinese_typo] error_rate` 决定**，与宿主同效；宿主那句硬编码的
「抽到建议后 50% 发错字 / 50% 整句换成正确句」门控**照抄 0.5、不再对外暴露**
（`_HOST_TYPO_VISIBLE_PROBABILITY`，代码里有注释）。之前"改插件的错字概率没反应"就是因为
那一项只在纠正方式分支里才生效，容易被当成总开关。

**④ 留空 = 跟随宿主，不用负数**

- 数值项：**空字符串 `""`** = 跟随宿主（WebUI 里是文本框，填数字即覆盖）；
- 开关项：三选一 **`跟随宿主` / `开启` / `关闭`**（宿主关着也能开、开着也能关）；
- 文本项：空字符串；
- 顺手把插件自己的 `quote_reply.stale_at_weight` 从 `-1` 哨兵改成**留空 = 自动取原 @ 权重一半**，
  配置文件里不再出现负数。

**⑤ 首次生成配置时读宿主现值当默认值**

新增 `get_default_config()` 覆写：直接读宿主 `config/bot_config.toml`（同步、只读；
找不到宿主配置就退回模型默认值），把宿主**当前**的值填进插件配置。于是你打开 `config.toml`
看到的就是宿主现在的值，想重新跟随宿主把那项清空即可。

**⑥ 每个配置项都有简短注释**

81 个字段全部带 `x-toml-comment`；新增的 `_write_config_comments()` 在 `on_load` 里给
`config.toml` 逐行补**行尾中文注释**（只补注释、绝不改值，已带注释就跳过，幂等）——宿主的插件配置
写入器本身不写注释，所以这一步只能插件自己做；之后的合并（Runner 与 WebUI 都走增量合并）会保留注释。

**⑦ 节顺序：试验性功能在最后**

`plugin.py` 的 `BetterPostProcessingConfig` 里写明了顺序约定并用注释标注：`[plugin]` → 四个宿主镜像节
→ `[post_process]` → 其它功能节 → **试验性节（当前 `[emoji_meaning]`）必须放最后**。`__ui_order__`
与字段顺序保持一致，WebUI 与 `config.toml` 看到的顺序相同。

## [0.10.8] - @ 空格全来源规范化 + 错字诊断日志

服务器日志复盘（2026-09-21，MaiBot 1.2.3）发现两个"日志里看不出来"的问题：**@ 后面依然两个空格**、
**错字概率改成 1 却没有错字**。查下来不是后处理/分段没生效（日志里 `共 N 段，首段直发、登记 M 条后续动作` +
`补发完成` 一直在打，说明接管与多段发送都正常），而是**可观测性不足 + 空格只在自己注入时才管**：

- **@ 后空格：从"只管自己注入"改成"全来源统一"（`_normalize_leading_at_spacing`）**。
  改造前遇到"消息里已经有 @"（宿主 `attach_at`、别的插件注入）直接放行，而空格数量由注入方决定：
  宿主 **1.2.3 不加空格**、1.2.5+ 带一个、别的插件可能带两个 ⇒ 观感不稳定。现在无论谁注入的 @，
  都统一成 **`[at, 一个半角空格, 正文]`**（多出的空白段删掉、正文前导空白抹掉、没有空格就补一个），
  且**改动会真的回传宿主**。
- **修一个真 bug**：`_has_leading_at` 分支在 `style == "at"` 时返回 `False`，而调用方只在返回 `True`
  时才回传 `modified_kwargs` ⇒ **对"已经有 @"的消息做的任何改动都会被丢掉**（规范化等于白做）。
  现在"改了消息体就返回 True"。
- **@/引用决策日志**：每次抽取回复方式都打一行 `回复方式=at（注入 @）：前缀组件 [at:某人, text:' ', text:'正文']`，
  空格数量一眼可见（用的是组件真实结构，不是猜测）。
- **错字诊断**：每个插件实例构建处理器时打一行 `后处理接管参数：… 错字enable=True(源宿主) error_rate=0.01(源宿主) min_freq=9 …`，
  **每轮回复**再打一行 `后处理接管统计：宿主开关[分段=True 错字=True] 生效参数[…] → 抽到纠正建议=N 句、
  真出现错字=N 句、旧分支模式=…、共 N 段`。以前只能从"段数"反推，现在"到底有没有生成错字"直接可读：
  - `错字enable=False` ⇒ 宿主 `chinese_typo.enable` 关着，或本插件 `[host_override] typo_enable="off"`；
  - `抽到建议=0` ⇒ 错字压根没生成（`error_rate` 太小 / `min_freq` 太高 / 词频表异常）；
  - `出现错字>0 但段数没涨` ⇒ 宿主那半句"50% 整句替换成正确句"的门控生效（正常行为）。
- `PostProcessor.last_stats` 暴露最近一次的统计（抽了几句 / 几句真出现错字 / 几条纠正建议 / 几段），
  `process()` 与 `plan()` 都写，不改变任何随机数调用顺序（差分测试仍全绿）。

> **本轮排查结论（供你核对配置）**：日志里 18 轮回复**都没有出现错字**，而插件侧参数是
> `error_rate=0.01(源宿主)` 时的正常表现（可见率 0.9%）。如果你改的是**本插件**的
> 「错字出现概率」`correction_gate_probability`——**它只在开启「错字纠正方式」新分支时才有意义**
> （默认关），旧分支下改它没有任何效果；要直接拉高错字率请改宿主【中文错别字 · 单字错字概率】，
> 或在本插件 `[host_override] typo_error_rate = 1`（0.10.7 起支持，且**以插件为准**）。

## [0.10.7] - 宿主可配置项全覆盖（`[host_override]`）

宿主配置界面里的可配置项，插件现在**逐一提供同义配置**——默认值 = 跟随宿主，填了值 = 以插件为准。
于是既能"只维护宿主配置"，也能"只在本插件里改"，不必两处来回切。

**取值约定**（由 WebUI 控件类型决定）：三态开关用下拉框 `host`/`on`/`off`（默认 `host`）；
数值项**填任何负数 = 跟随宿主**（默认 `-1`，`0` 是真的用 0）；文本项留空 = 跟随宿主。
**故意不用 `Optional[...]`**：SDK 的 Schema 生成器把 `Optional` 映射成 `type=string / ui_type=text`，
数字框还会把 `null` 当默认值显示成 `0`——在 WebUI 里一保存就会把"跟随宿主"改成 0。回归测试里
专门有一条守着它（任何字段退化成 `text` 即失败）。

| 宿主界面项 | 宿主路径 | 插件字段（`[host_override]`） |
|---|---|---|
| 后处理 · 打字速度 | `response_post_process.typing_speed` | `typing_speed` |
| 回复分割器 · 启用回复分割 | `response_splitter.enable` | `splitter_enable` |
| 回复分割器 · 单条最大长度 | `response_splitter.max_length` | `splitter_max_length` |
| 回复分割器 · 单条最大句数 | `response_splitter.max_sentence_num` | `splitter_max_sentence_num` |
| 回复分割器 · 最多分割条数 | `response_splitter.max_split_num` | `splitter_max_split_num` |
| 回复分割器 · 保护颜文字 | `response_splitter.enable_kaomoji_protection` | `splitter_kaomoji_protection` |
| 回复分割器 · 超限保留全文 | `response_splitter.enable_overflow_return_all` | `splitter_overflow_return_all` |
| 中文错别字 · 启用错别字 | `chinese_typo.enable` | `typo_enable` |
| 中文错别字 · 错别字纠正时引用原消息 | `chinese_typo.enable_correction_quote` | `[post_process] quote_correction` |
| 中文错别字 · 错别字纠正引用概率 | `chinese_typo.correction_quote_probability` | `typo_correction_quote_probability` |
| 中文错别字 · 单字错字概率 | `chinese_typo.error_rate` | `typo_error_rate` |
| 中文错别字 · 最小字频 | `chinese_typo.min_freq` | `typo_min_freq` |
| 中文错别字 · 声调错字概率 | `chinese_typo.tone_error_rate` | `typo_tone_error_rate` |
| 中文错别字 · 整词替换概率 | `chinese_typo.word_replace_rate` | `typo_word_replace_rate` |
| （bot 自称，兜底默认回复用） | `bot.nickname` | `bot_nickname` |

- **故意不提供覆盖**的只有一项：宿主【后处理 · 启用回复后处理】。它是本插件的**生效条件**
  （宿主开着它时宿主自己就做后处理，本插件必须让位），不是可以随便改的参数。
- **打字速度**覆盖的实现：跟随宿主（默认 `-1`）时照旧把 `typing=True` 交给宿主（宿主按它自己的速度睡）；
  填了值后由插件按**与宿主 `calculate_typing_time` 逐项一致的公式**自己睡，并传 `typing=False` 免得宿主再睡一遍。
  公式（1.2.3，已在本机 venv 里 import 宿主函数实测核对）：中文 `0.3s`/字、其它 `0.15s`/字符，求和后乘
  速度系数；`≤0` 不等待（填 `0` = 不打字等待）。**两个容易踩的宿主细节照抄**：① 宿主 docstring 里写的
  `0.2/0.1` 是过时注释，实际签名默认值是 `0.3/0.15`；② 整条只有 1 个汉字时宿主**提前返回**固定的
  `0.3×3+0.3 = 1.2s`，既不乘速度系数也不做 `≤0` 判定。
  只作用于本插件补发的分段（第 1 段由宿主发送链发出，宿主分段循环里首段本就是 `typing=False`）。
- `[post_process] quote_correction` **同时**作为 `chinese_typo.enable_correction_quote` 的取值来源
  （改造前它只在"发出时"再拦一道，宿主关掉引用时插件开着也引不了）：现在它以插件配置为准，
  行为单一来源。
- 修一个配置生效问题：改插件配置（`on_config_update` scope=self）时**没有作废后处理处理器**，
  新增/修改的 `[host_override]`、`[post_process]` 参数要等宿主配置变化或重启才生效；现在
  `_rebuild_derived_state()` 里一并作废。

## [0.10.6] - 错字出现概率可调 / 超限不纠错 / 撤回后彻底清理

- **错字出现概率可调（`correction_gate_probability`，默认 `0.5`）**：宿主那句
  「抽到纠正建议 → `random() < 0.5` 发错字、否则整句替换成正确句」的硬编码 0.5 做成配置项。
  **取 0.5 与宿主完全同频**，取 1.0 表示错字一律可见。上一版把这条门控去掉了，导致错字可见率
  比宿主高；实测（4000 句、宿主默认 typo 参数）：错字+建议 0.7%、只有错字 0.5%、无错字 98.8%，
  宿主可见率 = 0.5% + 0.5×0.7% = **0.9%**，无门控新分支 = 0.7% + 0.5% = **1.2%（1.39×）**；
  现在默认 gate=0.5 → 回到 0.9%。
- **超限不纠错（`correction_max_cjk`，默认 `20`）**：某段汉字数**超过**该值时，该处错字即使
  出现也**不纠正**（错字留着、不消耗纠正方式抽取）；`0` = 不限制。这样短消息（含单段）照常走
  纠正判定，长消息不乱补。
- **撤回后彻底清理（`recall_purge`，默认开）**：撤回成功后
  1. 删掉宿主消息库里那条记录（`database.delete`，按平台消息 id 过滤 `Messages`／`mai_messages`）；
  2. 对我们自己补发的分段，**发出时就不写 Maisaka 历史**（`sync_to_maisaka_history=False`）——
     因此"撤回后重发"在 bot 自己的上下文里只剩修正句。
  限制（已写进 README）：**宿主发出的第 1 段**由宿主自己写历史，插件拦不住；它的库记录会被删掉，
  但内存上下文里那条要等重启/上下文重建后才消失。
  另记：宿主的"正常撤回"其实**不会**删除原消息——NapCat 的 `notice.group_recall` 只是被适配器包装成
  一条 `is_notify` 通知消息入库（"[事件] xxx 撤回了一条消息"），原消息照旧留在库里与上下文里；
  "原消息消失"是本插件额外做的。
- `_manifest.json` 新增 `database.delete` 能力（删库用；**manifest 变更需完整重启**）。
- 更正段引用开关 `quote_correction` 的说明改写，并**统一了它的生效范围**：它只在**未开启**「错字纠正方式」时生效
  （等同宿主 `typo_enable_correction_quote`）；开启新分支后引用与否由 `weight_correction_quote`
  决定，此时把 `quote_correction` 设为 `false` **不再有任何效果**（之前会悄悄把抽到的 `quote`
  降级成 `direct`，撤回失败的兜底引用却不受它管，两处不一致）。想让纠正不引用就把
  `weight_correction_quote` 设为 `0`。

## [0.10.5] - 错字纠正方式（新分支）：直接发送 / 引用 / 撤回重发 / 不纠正 / 最后纠正

- **新分支 `[post_process] correction_mode_enabled`（默认关）**：开启后不再走宿主"抽到纠正建议就
  50% 追加更正段"的逻辑，而是对**每一处错字**按权重抽取纠正方式：
  | 方式 | 行为 |
  |---|---|
  | `direct`（直接发送） | 紧跟错字分段发一条纠正内容（不引用） |
  | `quote`（引用） | 紧跟错字分段发一条纠正内容，**引用**那条带错字的消息 |
  | `recall`（撤回重发） | **撤回**那条带错字的消息，然后重发**修正后的整句**（撤回后必须给完整内容） |
  | `none`（不纠正） | 错字留在消息里，不发任何纠正 |
  权重：`weight_correction_direct`(1) / `weight_correction_quote`(4) / `weight_correction_recall`(1) /
  `weight_correction_none`(1)；全 0 时按"不纠正"。
- **逐段决策、发送前定好顺序**：所有纠正方式在**分段全部发出之前**一次算完，动作序列 =
  第 1 段(宿主发) → 它的纠正 → 第 2 段 → 它的纠正 → …，因此"第二段出了错字"的纠正一定
  在**第三段发出之前**完成（不再出现"一边发一边随机"的时序不确定）。
- **`last_correction_enabled`（默认关）+ `weight_correction_last`(1)**：开启后"**最后纠正**"
  与上面四项一起参与抽取；抽到时该处纠正推迟到**本轮所有分段发完之后**再发，并**强制引用**
  它所属的那条错字消息（不引用会看不出在纠正谁）。
- **撤回实现**：走适配器公开 API `adapter.napcat.message.delete_msg`（napcat 与 SnowLuma 都暴露
  该名字；**能力 `api.call` 必须在 manifest 里声明**——0.11.2 修正，此处原文写"免声明"是**错的**，
  详见 0.11.2 条目），失败再试通用
  `adapter.napcat.action.call(action_name="delete_msg")`；两者都失败时按
  `[chinese_typo] recall_fallback`（默认 `quote`，另有 `direct` / `none`）兜底。
  撤回目标用发送回执里的**平台消息 id**（首段来自 `after_send` 的回填、补发段来自
  `ctx.send.text(return_details=True)`）。
- **实现方式**：`post_processing.PostProcessor` 新增 `plan()`（分段与宿主一致，纠正方式交给插件），
  原 `process()` 一行未改、RNG 序保持——差分测试（默认 / 超限保留全文+颜文字 / 高错字率 各 40 组）
  **0 处不一致**；合并逻辑抽出 `_merge_groups()` 以便把纠正动作的下标从"合并前"重映射到"合并后"。
- 需要注意：开启新分支后，宿主那句"整句换成正确句（等于没打错）"的分支被权重抽取取代，
  **错字的可见频率约翻倍**（想调低就用宿主 `chinese_typo.error_rate`，或把「不纠正」之外的
  权重整体调小）；本分支依赖【分段多条发送】开启，关闭时自动退回宿主原逻辑并打印警告。
- `quote_correction`（更正段引用上一段）在开启新分支时不再生效：引用与否由抽到的纠正方式决定。

## [0.10.4] - 与智能分段插件共存（让位机制）与参考实现致谢

- **让位给其它插件**（新配置 `[post_process] yield_to_other_plugins`，默认开）：同一轮回复里，
  如果别的插件已经认领文本后处理（判据是通用的 `skip_post_process=True`，智能分段插件
  `smart_segmentation_preserve_prepared_response` 命中它的预分段缓存时正是这么做的），
  本插件**不改写正文、不登记补发**，分段交给对方；`@/引用` 抽取、文本规则、回复后表情包、
  表情包跟风/含义库等功能照常工作。对方未认领（未安装 / 未命中 / LLM 失败）时本插件照常接管，
  作为兜底。关掉本项则本插件无条件优先（会压制对方的预分段）。
- **Hook 顺序调整**：后处理接管由 `BLOCKING/EARLY` 改为 **`BLOCKING/LATE`**，`reply_round_marker`
  由 `NORMAL` 改为 `LATE`（同一 Hook 上按名字排在接管之后）。这样其它插件的
  `BLOCKING/NORMAL` 处理器（智能分段插件就是默认的 NORMAL）会**先**执行，让位判据才有机会生效；
  轮记录仍是阻塞登记，且让位时照常登记，所以"@ 只挂首条"不受影响。
- 修复了此前二者同装的相互压制：BPP 先跑会把 `response` 改写成第 1 段，导致智能分段插件按
  "原文哈希"查预分段必然失配 → 它切好的段和 planner 等待全部空转（LLM 调用白花）。
  现在由**让位机制**决定谁干活，且不会出现两套补发同时发出（各自的登记缓存互不命中）。
- **参考实现致谢**：README 新增「参考实现与致谢」，写明多段发送机制参考
  [saberlights/smart_segmentation_plugin](https://github.com/saberlights/smart_segmentation_plugin)
  （GPL-3.0-or-later）的思路，**代码为本插件独立实现**（相似性取证见
  `smart_segmentation_plugin-main/LICENSE-审查-协议义务分析.md`：无成段代码复制，同名函数体
  相似度 ≤20.7%，重合长行均为宿主公开接口名/载荷键名）。

## [0.10.3] - @ 只挂在首条：轮记录确定化 + 回复方式只取首条

修的是实测现象：**bot 连着发两条消息时，第一条什么都没挂、第二条才 @ 人**。根因有两个，分别治：

- **轮记录（A：确定性）**：`reply_round_marker` 从 `OBSERVE` 改为 **`BLOCKING/NORMAL`**。
  宿主对 observe 处理器是 `asyncio.create_task` 后台调度、**不等待**（`hook_dispatcher._schedule_observe_handler`），
  于是轮记录可能晚于首条消息的发送。此时首条消息会命中"查不到本轮记录"或"只查到上一轮的旧记录
  （`style_done=True`）"而**被静默跳过**（不置 `style_done`），@ 就落到第二条上。改成 blocking 后
  轮记录在宿主继续后处理/发送之前必定就绪；顺带让"回复后表情包""文本规则归属判定"也变确定性。
- **回复方式只取首条（B1+C：新增配置 `[quote_reply] style_once_per_target`，默认开）**：
  - 同一条目标消息**只有第一条回复**能抽到 @，之后的回复只在「直接回复 / 引用回复」里抽
    （第一条没抽到 @，后续也不会再抽）——覆盖"一轮里回了两条、@ 落在后面那条"的情形；
  - 抽到 @ 也算**用过一次指代**（计入「同一消息只引用一次」），后续回复不再引用同一条消息，
    避免"先 @、再引用同一条"的重复指代；
  - 记录按 (会话, 目标消息) 存放，保留 30 分钟（`_STYLED_TARGET_TTL_SECONDS`，超时的目标本就
    被 `stale_age_seconds` 兜底强制不引用）；关掉本项即回到 0.10.2 的"每轮独立抽 @"行为。
- 文档：README 的「引用回复接管」补 `style_once_per_target` 字段说明与"@ 只挂首条"的判定链路；
  `_apply_reply_style` docstring 同步。

## [0.10.2] - @ 空格口径统一与补发健壮性

- **@ 后空格统一为"恰好一个半角空格"**（`modules/quote_takeover.py`）：
  - 注入前先抹掉正文自带的前导空白（半角/全角空格、Tab、NBSP 都算），再恒定补一个半角空格
    组件。此前"正文自己带空白时守卫跳过、正文不在首段时守卫又漏判"，会出现"有时 1 个、有时
    2 个空格"，现在与正文原样无关。
  - "不重复 @"的判据从"`components[0]` 是 at"改为"**第一个非空文本段之前存在 at**"：
    宿主 1.2.3 的 `attach_at`（`[at, ...]`）、1.2.5+ 的（`[at, " ", ...]`）、以及其它插件在
    更前面插引用段的情况都能认出来，不再重复注入第二个 @。
  - 删除 README/注释里"QQ 不会在 at 段后自动补空格"这类绝对结论（客户端行为不可控，
    插件只保证自己的口径）。
- **补发重入判据由"会话级"改为"文本级"**（`modules/post_process_takeover.py`）：
  此前补发窗口内同一会话的**任何**出站消息都会被跳过预分段登记，导致"另一轮回复只发出首段、
  尾部静默丢失"。现在只跳过"本插件正在补发的那条文本"，并留 debug 日志。
- **卸载清理补全**（`plugin.py::on_unload`）：多段发送的补发任务不在 `self._tasks` 里，
  此前卸载/热重载时不会被取消（可能补发半途而废或向已卸载上下文发送）；现在一并取消并等待，
  同时清空预分段/待补发/引用注入等运行时缓存。
- **`[plugin] rich_reply_gate`（新配置，默认 `true`）**：把"两个接管要求宿主关闭「丰富回复」"
  从硬编码改为可配置。默认仍是门控；关掉后宿主开着丰富回复也允许接管（代价：附件只挂首段、
  补发分段不带附件）。模块状态日志里对应写成"丰富回复未做门控"。
- **静默改为显眼**：模块状态日志中"受制且不可用"的行改用 `warning` 级别输出（可用行仍为 `info`），
  避免"配置打开了却什么都不做"被埋在一堆 info 里。
- 清理死代码：删除 `post_processing.py::process_to_text`（0.10.0 起接管改用 `process()` 取分段，
  已无调用方）；`_FOLLOW_UP_WAIT_HOOK_TIMEOUT_MS` 补注释说明"必须大于等待窗口、且有意大于系统
  默认 60s（处理器自带 timeout_ms 优先于 Hook 规格默认值）"。

## [0.10.1] - 前置条件门控与模块化拆分

- **前置条件（重要）**：两个"接管"模块都要求宿主**关闭对应能力**才会工作，且都额外要求关闭
  **丰富回复**（`experimental.enable_rich_reply`）：
  - 引用回复接管：`chat.reply_style.enable_reply_quote = false` + 丰富回复关闭；
  - 后处理接管：`response_post_process.enable_response_post_process = false` + 丰富回复关闭。
- **未满足即静默**：条件没满足（或宿主配置读不到）时，对应模块**完全静默**——不改写文本、
  不抽取回复方式、不登记补发、不发任何消息；插件配置里打开也不生效。读不到配置按"未满足"处理，
  宁可少做事，也不与宿主原生能力重复处理同一条消息。
- **不受制的模块**：回复后表情包、表情包跟风、表情包含义库、文本替换规则——宿主开关任意状态都照常工作。
- **模块状态日志**：启动时与**每次配置变更时**（插件配置 / 宿主配置）逐模块打印可用状态：
  `[模块状态｜启动] 引用回复接管：静默（需在宿主配置关闭「丰富回复」（experimental.enable_rich_reply，当前 True））`。
- **模块化拆分**：新增 `modules/` 子包，把两个接管的实现从 `plugin.py` 拆出：
  | 文件 | 职责 |
  |---|---|
  | `modules/requirements.py` | 各模块的宿主前置条件与可用性判定（纯逻辑） |
  | `modules/post_process_takeover.py` | 后处理接管：Hook 1~5 + 多段发送 + 补发缓存 |
  | `modules/quote_takeover.py` | 引用回复接管：权重抽取 + 过旧规则 + 同一消息只引用一次 |
  两个接管以 **mixin** 形式被插件类继承（SDK 用 `dir(instance)` 收集组件，继承来的 `@HookHandler`
  一样会被注册）；`plugin.py` 只保留配置模型、生命周期与共享基础设施（回复轮记录、文本规则、表情包功能）。
- 拆分**不改变任何 Hook 点位与行为**：组件数仍为 12 个，多段发送 / 引用接管链路回归通过。
- `README.md` 新增「模块与前置条件」章节，`plugin.py` 顶部补代码结构说明。

## [0.10.0] - 完整复刻宿主后处理框架效果（含每段一条消息）

- **多段发送**：接管后不再把各分段换行拼成一条，而是**每段作为独立消息**依次发出——
  第 1 段仍走宿主原生发送链（引用位置、写库、平台行为、Maisaka 历史同步全部原生），
  第 2..N 段由插件补发。
- **模拟打字**：补发分段前按宿主 `calculate_typing_time` 模拟打字等待（速度沿用宿主
  `response_post_process.typing_speed`），与宿主分段循环 `typing=index>0` 的口径一致。
- **更正段引用**：错别字更正段（`quote_previous`）引用上一段消息，复刻宿主行为
  （`ctx.send.text` 无法携带 `reply_message`，改为在 `send_service.before_send` 注入引用）。
- 新增 Hook：`send_service.after_build_message`（登记待补发）、`send_service.after_send`（observe，首段
  发出后启动后台补发）、`maisaka.planner.before_request` 与 `chat.receive.before_process`（补发未完成时
  先等待，防下一轮上下文缺段 / 防新消息与旧回复交错）。
- 新增配置（`[post_process]`）：`multi_message`（默认开）、`typing`（默认开）、`quote_correction`（默认开）、
  `max_segments`（默认 0 = 不额外限制，沿用宿主 `max_split_num`）、`wait_timeout_seconds`（默认 30）。
- **修复**：`text_rules` 的 `reply_flow` 归属判定原本不认插件补发的分段（这类发送没有
  `reply_message_id`），导致补发段不会被词替换/覆盖规则命中。
- 首段发送失败时整轮放弃，不会只发出"尾巴"；补发期间有重入保护，避免补发内容被二次分段。
- `_manifest.json` 新增 `send.text` 能力（**改动 manifest 需完整重启 MaiBot**）。
- **已知差异**（纯插件路径固有限制，已写入 README）：`reply` 工具结果里的 `reply_segments` /
  `track_reply_effect` 只反映第 1 段；补发分段不带表情附件（`ctx.send.text` 不支持 `selected_expressions`）；
  若 planner prompt 在补发完成前就已构建，该轮上下文仍只含首段（用等待 + 历史同步尽量规避）。

## [0.9.1] - 还原后处理接管（换行拼接版）

- 按需求**还原被移除的后处理接管**：新建 `post_processing.py`，逐行复刻宿主
  `process_llm_response_segments` 与 `ChineseTypoGenerator`（错别字注入、分段、颜文字保护、
  长度/句数守卫、默认回复）。
- 新增配置节 `[post_process] takeover`；新增 Hook `maisaka.reply.before_post_process`（blocking），
  框架后处理总开关关闭时接管，把结果写回 `response` 并置 `skip_post_process=True`。
- 分段/错别字参数全部沿用宿主配置（`response_splitter.*`、`chinese_typo.*`、`bot.nickname`），
  改宿主配置无需改插件配置。
- 当时的交付形态：各分段**以换行拼接成一条消息**（宿主分段发送循环锁死在回复工具内部、受总开关门控，
  单靠该 Hook 无法重新触发分段）。
- 加固：`jieba` / `pypinyin` 缺失时只降级"后处理接管"，插件其它功能照常工作，加载时显式报错。

---

## 早期版本（回溯）

| 版本 | 变更 |
|---|---|
| **0.9.0** | 出站文本规则的 `apply_scope`（默认只作用于 planner 拉起的本轮回复，`all_outbound` 可覆盖全部出站文本）；表情包含义库的 `inject_request_types`（默认只注入 `maisaka.replyer`） |
| **0.7.1** | 引用接管生效范围收紧：只处理本插件登记的回复轮中**指向轮目标消息的首条文本分段**，后续分段/非纯文本/其它插件的发送一律不动；补发判定完成后轮记录只标记 `emoji_done`、不立即删除（否则晚到的分段会退化为逐条抽取） |
| **0.7.0** | 新增 `maisaka.planner.after_response` 观察 planner 表情意图（planner 打算自己发表情时不补发）；回复轮必须真的发出消息才算成立（`first_send_timeout_seconds`）；表情包跟风忽略 bot 自身消息；表情冷却计入未入库的表情（`count_unstored`） |
| **0.6.1** | WebUI 配置页全面中文化：所有字段加中文短标签，并为配置节与字段内置英文 i18n |
| **0.6.0** | 引用目标过旧降级：目标发出超过 `stale_age_seconds` 秒后强制不引用直接发送 |
| **0.5.0** | 「对话已推进」调整：目标之后已出现 ≥ `stale_threshold_messages` 条消息时剔除"直接回复"并下调 @ 权重 |
| **0.4.0** | 表情包冷却配置合并到 `[emoji_cooldown]`（原 `[emoji_after_reply] cooldown_seconds` 与跟风各自配置） |
| **0.2.0** | **移除**后处理接管（理由：分段发送循环受宿主总开关门控，插件无法复刻"每段一条消息"；强行绕行会让 planner 观察到"回复工具失败"，有刷屏风险） |
| **0.1.0** | 首个版本：在框架关闭「回复后处理总开关」时于 `maisaka.reply.before_post_process` 复刻宿主后处理逻辑 |


---

## 致谢

- 特别感谢 **[saberlights/smart_segmentation_plugin](https://github.com/saberlights/smart_segmentation_plugin)**
  （作者**久远**）：本插件的**多段发送机制**参考了它的思路（`send_service.after_build_message`
  登记待补发 → `send_service.after_send` 补发 → planner / 新入站消息的时序守卫），0.10.4 起
  又靠它与本插件的**让位机制**和平共存。**感谢久远与这个项目。** 如果你的需求是"更主动、更激进的
  分段"，请直接使用它——本插件的分段只在宿主总开关关闭时兜底。
- 感谢 **MaiBot**（[Mai-with-u/MaiBot](https://github.com/Mai-with-u/MaiBot)）与插件体系
  `maibot_sdk`：本插件的分段与错别字逻辑派生自宿主源码，全部宿主数据访问都走宿主公开能力。
