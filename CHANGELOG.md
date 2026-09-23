# 更新日志

> **留档说明**：0.9.0 及更早的版本没有逐条留档，下面带「回溯」标记的条目是按代码注释与 README
> 里的版本标注整理出来的；**0.9.1 起为逐条记录**。

## 版本索引

按版本从新到旧。**每一行都是一次可追溯的改动**（背景 / 根因 / 验证方式见对应条目）。


- **`0.13.18`** — 评审修复：版本口径统一 / 文档与实现对齐 / 不再为配置文件添加注释
- **`0.13.17`** — bot 自身表情包走"按 hash" + 字面 @别人 的空格规范化
- **`0.13.16`** — 镜像字段的最终代码层兜底改用发布者 config.toml 的值
- **`0.13.15`** — 默认值对齐到实际 `config.toml`（镜像宿主的项除外）
- **`0.13.14`** — 文档开头加「使用前必读」（含宿主开关截图）+ 接管板块标注生效前提
- **`0.13.13`** — 标注 `at_trailing_space` 的宿主版本口径（1.2.5 起关闭 / 1.2.5 以前开启）
- **`0.13.12`** — 修「@ 与正文之间多空格」：不再注入显式空格组件
- **`0.13.11`** — 含义 30 天 TTL 淘汰 / 删除库扫描补录 / 配置节与字段重排 / 去试验性标记
- **`0.13.10`** — 跟进宿主淘汰机制：库扫描纳入"未注册但文件还在"的表情包
- **`0.13.9`** — 注入形态升级（标签/内容双标记 + 写明格式）+ 动图取首帧 + 跟风支持文本形态
- **`0.13.8`** — 保留宿主标签，改为在后面追加 `[内容描述：…]`
- **`0.13.7`** — 改用就地替换标签：把上下文里的 `[表情包: 标签]` 换成插件自己的描述
- **`0.13.6`** — 让模型把"看不到图 / 知道内容"如实说清（而非改口说看得到）
- **`0.13.5`** — 让含义注入"落地生效"：按 id 多级定位 + 入站即回填描述
- **`0.13.4`** — 补全"按需补录 / 含义注入"的静默路径日志
- **`0.13.3`** — 回填"生成含义时宿主还没出描述"的记录
- **`0.13.2`** — 修复「放弃名单被每轮 planner 复活」/ 注入成功打日志
- **`0.13.1`** — 修复含义生成「不受支持的图片格式」/ 上传前统一转格式
- **`0.13.0`** — 表情包含义库改为按哈希精准存储 / 新增 planner 按需补录
- **`0.12.2`** — 精简配置板块的说明文字
- **`0.12.1`** — 新增「过滤假 @」（LLM 手写在正文里的文本 @）
- **`0.12.0`** — 错字纠正改由权重池恒定决定 / @昵称防错字 / 撤回反应时间
- **`0.11.3`** — 新增「强制直发阈值（条）」/ 过旧规则说明与注释梳理
- **`0.11.2`** — 三路审核修复：许可改为 GPL / 两个真机缺陷 / 文档重排与致谢
- **`0.11.1`** — 配置重新排版：按功能板块分（引用回复 / 错别字 / 分段）
- **`0.11.0`** — 配置改版：宿主已有参数照搬宿主 + 留空跟随宿主 + 每项注释
- **`0.10.8`** — @ 空格全来源规范化 + 错字诊断日志
- **`0.10.7`** — 宿主可配置项全覆盖（`[host_override]`）
- **`0.10.6`** — 错字出现概率可调 / 超限不纠错 / 撤回后彻底清理
- **`0.10.5`** — 错字纠正方式（新分支）：直接发送 / 引用 / 撤回重发 / 不纠正 / 最后纠正
- **`0.10.4`** — 与智能分段插件共存（让位机制）与参考实现致谢
- **`0.10.3`** — @ 只挂在首条：轮记录确定化 + 回复方式只取首条
- **`0.10.2`** — @ 空格口径统一与补发健壮性
- **`0.10.1`** — 前置条件门控与模块化拆分
- **`0.10.0`** — 完整复刻宿主后处理框架效果（含每段一条消息）
- **`0.9.1`** — 还原后处理接管（换行拼接版）

> 0.9.0 及更早没有逐条留档；**0.9.1 起**为逐条记录（见下方正文）。

---

## [0.13.18] - 评审修复：版本口径统一 / 文档与实现对齐 / 不再为配置文件添加注释

本轮是**评审修复版**，不含新功能。

> **版本号说明**：`dev` 分支的「插件自带文件日志」与本轮评审修复**同属 `0.13.18`** ——
> 合并 dev 分支时**不另起版本号**，`0.13.18` 同时涵盖评审修复与文件日志两部分改动。

### 一、版本口径统一（评审问题 1）

`_manifest.json`、`SUPPORTED_CONFIG_VERSION`、`CHANGELOG` 三处此前不一致
（manifest 与 CHANGELOG 停在 0.13.17，发布说明写 v0.13.18）→ 现统一为 **0.13.18**。

### 二、README 能力清单数字（评审问题 2）

"manifest 声明 **12** 项能力" → 实际是 **11** 项。已重新用 AST 对账：

- 从 `self.ctx.<代理>.<方法>` 收集到的能力集合与 manifest **完全相同**
  （`ctx.db.query` 即 `database.query` 的代理写法；`ctx.logger` / `ctx.paths` 不是能力代理）；
- 既无"声明了没用到"，也无"用了没声明"。

### 三、README 与实现矛盾（评审问题 3）

原文"**不直接读写**宿主的数据库文件或文件系统"与实现矛盾
（`get_default_config()` 读 `config/bot_config.toml`；`_load_emoji_bytes_by_hash()`
按 `Images.full_path` 读宿主图片文件）。已改为准确措辞：

- **不写**宿主文件系统、**不碰**宿主数据库文件；
- 但**有两处只读的宿主文件读取**，逐条写明防护（只读、根目录包含校验、单文件 8MB 上限、
  失败即降级到 `emoji.get_random` 抽样）；
- 唯一的运行时写文件是**插件自己的 `config.toml` 补注释**，且可关闭。

### 四、**彻底移除"插件写配置文件"的行为**（评审问题 4）

评审要求："配置文件优先改成由模板 / WebUI 生成。**不提供配置项来开关，而是确保插件不再动配置文件。**"

**先查清了"由模板生成"在当前宿主上做不到**（不是偷懒，是没有可用机制）：

| 检查项 | 结果 |
|---|---|
| `runner_main.py:_save_plugin_config` | 用 `tomlkit`，**只能保留**已有注释、**不会新增**（首次生成 `tomlkit.dumps(dict)` 无注释） |
| `_get_plugin_default_config` | 只接受普通 `dict`（`isinstance(default_config, dict)`） |
| SDK 侧 `x-toml-comment` | **完全不处理** |
| 宿主是否有 `template` 机制 | **没有** |

唯一能"预置注释"的办法是在插件目录里**预置一份 `config.toml`**（宿主的
`should_initialize_file = not config_path.exists()` 会跳过生成），但那与三条既有约定直接冲突：
`/config.toml` 已被 `.gitignore` 排除、发布目录要求"只含运行必需文件"、且会把
**每次本地改动都变成发布默认值** —— 因此**不采用**。

**最终处理：把写配置的代码整段删掉，不留开关。**

- 删除上一版新加的 `[plugin] write_config_comments` 字段（**不提供任何开关**）；
- 删除 `_write_config_comments()` 与它专用的 `_toml_values_unchanged()`；
- 删除 `on_load` 里的调用。

**插件现在的文件系统足迹（全量审计）**：

| 操作 | 位置 | 说明 |
|---|---|---|
| `mkdir` | `emoji_meanings.py` | 建**自己的数据目录**（放 `emoji_meanings.db`）—— 唯一的写操作 |
| `open("rb")` | `plugin.py` / `modules/post_process_takeover.py` | **读**宿主 `bot_config.toml`（只读） |
| `open("r")` | `post_processing.py` | **读**宿主字频表（只读） |

`grep "write_text|\.write\(|open\(.*['\"]w"` 在插件内**零命中**；
`config.toml` 由**宿主**生成与维护（首次生成 / 配置版本变化 / WebUI 保存），插件只在加载时**读**它。

**说明文案改由"三处同源"承载**：`config.toml` 的行尾注释（宿主保留）、
WebUI 的字段提示（插件通过 `get_webui_config_schema` 补 `hint`）、README 的字段表。

### 五、文档整理

- **README**：修正 `[plugin]` 字段表里 `config_version` 的过时默认值（原写 `'0.12.1'`）；
  明确写出"插件不写任何配置文件"与"说明文案三处同源"；安装小节补一行指向本文件（版本历史）。
- **CHANGELOG**：新增 **「版本索引」**（把 36 条历史压成一张可跳转的速查表）。
- **`.gitignore`**：补上 `/提交模板.md` —— 该文件是本地提交辅助文档、**不应随插件发布**
  （它自己写着「已用 .gitignore 排除」，但 main 分支此前漏了这行，发布时会连带上传）；
  现已与 dev 分支的 `.gitignore` 完全一致。
- 核对 `提交模板.md` 的自查清单：`version = 0.13.18` 与 `SUPPORTED_CONFIG_VERSION` 一致、
  「已声明全部 11 项 capabilities」与 manifest 一致、发布目录要求含 `docs/` —— 均已对齐。

## [0.13.17] - bot 自身表情包走"按 hash" + 字面 @别人 的空格规范化

### 一、bot 自己发的表情包也走"按 hash"

`handle_inbound_emoji_observer` 原来在 `ignore_self_messages` 处**先 return**，
而 `_remember_message_emoji_refs`（记录 `msg_id → [(hash, 描述)]`）在其后 ——
于是 **bot 自己消息的本地映射永远是空的**，含义库注入时只能退化成"按描述"匹配：

| 匹配路 | 依据 | 群友的表情包 | bot 自己的（改前） |
|---|---|---|---|
| A | 本地映射里的 hash | ✅ | ❌ |
| B | 标签文本 = 宿主 `description` | ✅ | ✅（唯一可用） |

→ **空标签**（`[表情包]`，宿主 description 为空）时两条路都断 → bot 自己的那条标签**注入不了**。

**改法**：把"记录映射"整块**提到 self 过滤之前**（只写缓存，**跟风连击不受影响** ——
连击仍在 self 过滤处 return）。改后 bot 自己的表情包也能按 hash 精确匹配。

### 二、字面 `@别人` 的空格规范化（③ 跳过注入那条路）

排查"@ 后空格时有时无"时用**真实函数**把四条路径全跑了一遍（脚本见归档），结论：

- 插件自己注入/升级/规范化的三条路（① 注入 / ② 字面升级 / ④ 只规范化）在**同一配置下产物完全一致**；
- **唯一不受控的是 ③**：正文开头是"字面 @别人"时插件**跳过注入、原样发出** →
  那条消息的空格数**完全由模型写的文本决定**（实测 0 / 1 / 2 个都可能）。

**改法**：新增 `_normalize_text_mention_spacing()`，把字面 @ 后的空白规范成**恰好一个半角空格**
（半角/全角空格、Tab 都收成一个；后面没有正文时不动）。改后：

| `at_trailing_space` | ①②④（真实 at） | ③（字面 @） |
|---|---|---|
| `false` | 0 个（粘连） | **1 个** |
| `true` | **1 个** | **1 个** |

→ **MaiBot 1.2.3 上应把 `at_trailing_space` 打开**（平台侧 at 段不自带空格），
这样四条路在群里都是**恰好一个空格**。

### 其它

- 回归新增 `test_self_emoji_hash_and_text_mention_spacing()`：
  bot 自身消息**记映射但不计连击**、注入侧可按 hash 命中、非表情包消息不写映射；
  以及字面 @ 空格的 10 种输入（0/1/3 个半角空格、全角、Tab、开头空白、无正文、不匹配、不改入参）。
- README 的「@ 与正文之间的空格」小节补上**四条路径表 + 平台侧口径**。

## [0.13.16] - 镜像字段的**最终代码层兜底**改用发布者 config.toml 的值

### 四级取值链路（明确并落地）

镜像宿主的那 13 项现在有一条写清的降级链，**前一级拿不到才退下一级**：

| 级别 | 情形 | 用哪个值 |
|---|---|---|
| ① | 插件配置**填了具体值** | 用插件填的（最优先） |
| ② | 插件配置**留空**（`""` / 「跟随宿主」） | 用宿主**运行时**配置快照（`ctx.config.get`） |
| ③ | 宿主快照**读不到** | 退到 `_POST_PROCESS_CONFIG_KEYS` **第 3 列** |
| ④ | 第 3 列 = **最终代码层兜底** | 取自**发布者实机在用的 `config.toml`** |

即：**默认留空 → 留空即跟随宿主 → 跟随宿主失败才用代码兜底**。

### 改动的 3 个兜底值

| 兜底项 | 旧（宿主官方默认） | 新（发布者实机值） |
|---|---|---|
| `response_splitter.max_split_num` | `3` | **`4`** |
| `response_splitter.enable_overflow_return_all` | `False` | **`True`** |
| `bot.nickname`（→ `fallback_nickname`） | `"麦麦"` | **`"普瑞赛斯"`** |

其余 10 项本来就与发布者 config.toml 一致，未动；
`typo_enable_correction_quote` / `typo_correction_quote_probability` 是**不暴露给插件配置**的
宿主专用参数（`_HOST_ONLY_PROCESSOR_PARAMS`），保持宿主官方默认。

### 其它

- `plugin.py` 的配置规则第 5 条、`_POST_PROCESS_CONFIG_KEYS` 上方的注释、README「三条通用规则」
  第 3 条都补上了这条四级链路的说明与兜底值表。
- 回归测试新增 `test_host_mirror_fallback()`：锁定 3 个兜底值 + **逐级验证 ①~④**
  （插件填值 / 留空跟随宿主 / 宿主读不到退兜底 / 非法值降级）。
- 代码逻辑除这 3 个常量外**零改动**；配置面、组件数不变。

## [0.13.15] - 默认值对齐到实际 `config.toml`（镜像宿主的项除外）

**只改字段默认值与文档，运行时逻辑零改动。**

### 规则

- **以 `logs/config.toml` 的值为准**，把插件各字段的默认值改过去（共 **20 项**）；
- **例外**：**会主动从宿主镜像取值的配置项不采纳** —— 它们保持"跟随宿主 / 留空"的哨兵默认，
  用户那份 config.toml 里的数值只在其本机生效；插件侧默认仍只是"读不到宿主值时的兜底"。

### 被排除的 13 个镜像项（默认值不变）

`[response_splitter]`：`enable` / `max_length` / `max_sentence_num` / `max_split_num` /
`enable_kaomoji_protection` / `enable_overflow_return_all` / `typing_speed` / `fallback_nickname`
`[chinese_typo]`：`enable` / `error_rate` / `min_freq` / `tone_error_rate` / `word_replace_rate`

> 依据：`modules/post_process_takeover.py` 的 `_HOST_MIRROR_FIELDS`（12 项）+ `typing_speed`
> （`FOLLOW_HOST` 默认，映射 `response_post_process.typing_speed`）。
> 首次生成配置时 `get_default_config()` 会直接读宿主现值，所以这 13 项**必须留哨兵**。

### 改动的 20 个默认值

| 字段 | 旧 | 新 |
|---|---|---|
| `quote_reply.force_direct_threshold_messages` | `0` | **`2`** |
| `quote_reply_weights.weight_direct` | `2` | **`5`** |
| `quote_reply_weights.weight_quote` | `6` | **`1`** |
| `quote_reply_weights.weight_at` | `1` | **`2`** |
| `quote_reply_weights.weight_quote_at` | `1` | **`4`** |
| `quote_reply_private.force_direct_threshold_messages` | `0` | **`3`** |
| `quote_reply_private.stale_threshold_messages` | `3` | **`5`** |
| `quote_reply_private.stale_exclude_bot_messages` | `False` | **`True`** |
| `quote_reply_private_weights.weight_direct` | `2` | **`4`** |
| `quote_reply_private_weights.weight_quote` | `6` | **`2`** |
| `chinese_typo_weights.weight_correction_direct` | `1` | **`2`** |
| `chinese_typo_weights.weight_correction_recall` | `1` | **`5`** |
| `chinese_typo_weights.last_correction_enabled` | `False` | **`True`** |
| `chinese_typo_weights.last_correction_probability` | `1.0` | **`0.2`** |
| `emoji_meaning.enabled` | `False` | **`True`** |
| `emoji_meaning.rewrite_scope` | `['replyer']` | **`['replyer', 'planner']`** |
| `emoji_after_reply.planner_emoji_tools` | `['send_emoji', 'emoji_like', 'emoji_like_list']` | **`['send_emoji']`** |
| `emoji_follow.threshold` | `3` | **`2`** |
| `emoji_follow.probability` | `0.5` | **`0.4`** |
| `emoji_cooldown.seconds` | `300.0` | **`180.0`** |

README 的默认值列/散文说明同步更新。

## [0.13.14] - 文档开头加「使用前必读」（含宿主开关截图）+ 接管板块标注生效前提

**只改文档与随包图片，代码逻辑零改动。**

- **README 开头新增「使用前必读」**：显著告知"不关宿主这几个开关，对应接管能力完全不生效"，
  并按优先级给出 3 项必关开关（**丰富回复能力最优先** —— 不关它所有接管都失效；
  再是 启用引用回复 / 启用回复后处理），每项配**宿主设置截图** + 操作路径。
- **截图打包进插件**（新增 `docs/` 目录，ASCII 文件名便于任何渲染器引用）：

  | 文件 | 内容 |
  |---|---|
  | `docs/host-switch-1-rich-reply.png` | 麦麦设置 → 实验性功能 → 关闭「丰富回复能力」 |
  | `docs/host-switch-2-quote-reply.png` | 麦麦设置 → 聊天 → 如何发言 → 更多 → 关闭「启用引用回复」 |
  | `docs/host-switch-2b-more-button.png` | **「更多」按钮**：点击前显示为「更多」、展开后显示为「收起」 |
  | `docs/host-switch-3-post-process.png` | 麦麦设置 → 聊天 → 后处理 → 关闭「启用回复后处理」 |

- **接管板块加生效前提注释**（`[quote_reply]` 引用回复 / `[chinese_typo]` 错别字 /
  `[response_splitter]` 分段各加一条 `> ⚠️ 生效前提` 引用块，并链回「使用前必读」）。
  **不需要关闭宿主任何功能的板块（回复后表情包 / 表情包跟风 / 聊天流表情冷却 / 文本替换规则 /
  表情包含义库）不加任何额外注释**。
- `[quote_reply]` 小节里额外标注 **「更多」与「收起」是同一个按钮**（折叠时叫「更多」）。
- TOC 与 `## 模块与前置条件（重要）` 均加上通往「使用前必读」的入口。

## [0.13.13] - 标注 `at_trailing_space` 的宿主版本口径（1.2.5 起关闭 / 1.2.5 以前开启）

官方文档（`MaiBot插件开发文档v1.2.5`）明确记载了这条行为变更：

> **1.2.5 起，Maisaka 富回复在拼接 `attach_at` 组件时，会在每个 @ 组件后插入一个空格组件**
> （`src/maisaka/builtin_tool/context.py:428-432`）
> ```python
> at_prefix_components.extend([at_component, TextComponent(" ")])
> items[0].sequence.components = at_prefix_components + items[0].sequence.components
> ```
> 1.2.3 是 `items[0].sequence.components = at_components + items[0].sequence.components`（**无空格**）
>
> —— `06-附录/B-版本差异与插件影响.md` B-5【行为变更】：
> "丰富性回复在每个 @ 组件后增加空格，使提及对象与正文清晰分隔。"
> 另见 `03-能力参考/01-组件装饰器.md`（"1.2.5 富回复 @ 组件后的空格"折叠块）、
> `MaiBot插件开发文档v1.2.3/版本更新提示-请阅读v1.2.5文档.md` §5 版本对照表。

**因此 `[plugin] at_trailing_space` 的口径是版本相关的**：

| 宿主版本 | 宿主自己插空格？ | 本项应设为 |
|---|---|---|
| **1.2.5 及以后** | ✅ 每个 @ 组件后插一个 `TextComponent(" ")` | **关闭（默认）** |
| **1.2.5 以前（不含 1.2.5）** | ❌ 只有 `at_components + …` | **开启** |

本版只**标注口径**（字段 label / description / `config.toml` 行尾注释 / i18n 全部写明），
**默认值不变**（仍是 `False`），代码逻辑与 0.13.12 完全一致。

> 补充说明：宿主渲染出站纯文本用的是 `" ".join(每个组件)`，所以**开启后**纯文本里 `@昵称` 与正文
> 之间会出现三个空格（并跟着 `processed_plain_text` 进消息库、上下文与记忆抽取）——
> 这是「平台侧 @ 与正文不粘连」与「纯文本好看」之间的取舍；1.2.5+ 的宿主把这两件事都接管了，
> 插件不需要再操心。

## [0.13.12] - 修「@ 与正文之间多空格」：不再注入显式空格组件

### 现象与实证

群 634808517 里插件注入的 `@粥粥铺第一自律` 与正文之间出现了多空格。查证（会话 `205a4644…`）：

| 层面 | 实际内容 |
|---|---|
| 插件组件层（自己的日志） | `[at:粥粥铺第一自律, text:' ', text:'千金难买爷开心']` —— **只有 1 个空格** |
| A_memorix 记忆抽取 prompt | `买了 @粥粥铺第一自律   千金难买爷开心` —— **3 个空格** |

### 根因

宿主 `src/services/send_service.py:_build_processed_plain_text()` 构造出站纯文本时：

```python
    return " ".join(part for part in processed_parts if part)
```

**每个组件贡献一个 part，组件之间 join 再补一个空格**；而插件为了让 at 段与正文不粘连，
**显式注入了一个 `{"type":"text","data":" "}` 组件**。于是渲染结果是：

```text
@昵称  +  join空格  +  ' '  +  join空格  +  正文   =  三个空格
```

**为什么一直没发现**：`_build_outbound_log_preview()` 用 `" ".join(preview_text.split())`
把连续空白折叠了，所以**插件自己的日志里永远只显示一个空格** —— 0.10.8 那次"@ 后面依然两个空格"
的复盘看到的就是这个假象（那次修的是组件层，而问题出在渲染层）。

### 修复

- **默认不再注入空格组件**（`[at, 正文]`），与宿主 1.2.3 自己的 `attach_at` 口径完全一致：
  宿主渲染 → `@昵称 正文`（恰好一个空格，来自 join）；平台载荷 → `{at}` + `{text 正文}`。
- `_normalize_leading_at_spacing()` 同步改口径：默认**删掉** at 后面所有纯空白文本段
  （原来是把它们并成恰好一个空格段）。
- 新增配置 **`[plugin] at_trailing_space`（默认 `false`）**：给"某些客户端 at 段后不自动补空格、
  去掉后粘连"的环境回退用；打开即恢复 0.10.8 的旧口径 `[at, ' ', 正文]`。
- 两种口径都仍然**一律抹掉正文自带的前导空白**（否则会和渲染层的空格叠加）。

## [0.13.11] - 含义 30 天 TTL 淘汰 / 删除库扫描补录 / 配置节与字段重排 / 去试验性标记

### 一、含义按 TTL 淘汰（默认 30 天，可配）

- 新增配置 `meaning_ttl_days`（默认 **30**，`0` = 不淘汰）：**超过这么多天没用过**（也没更新过）
  的含义会被删除。
- 新增 `last_used_at` 列（旧库自动 `ALTER TABLE` 补列）：
  - **热路径不写库** —— 每次请求改写标签时只把用到的 hash 记进内存集合
    （`self._meaning_used`），由后台工作循环批量 `UPDATE` 回写（`touch_usage`）。
  - 淘汰判据是 `MAX(last_used_at, updated_at, created_at) < now - ttl`，
    所以**经常出现的表情包不会被误删**。
- 淘汰由工作循环顺带执行，**每小时最多一次**（`_MEANING_PURGE_MIN_INTERVAL_SECONDS`），
  命中时打 info：`已淘汰 N 条 X 天未使用的表情包含义（剩余 M 条）`。

### 二、删除「库扫描补录」

全库扫描既慢又贵，而没出现在上下文里的表情包本来也不影响聊天。删除内容：

- 配置项 `scan_enabled` / `scan_interval_seconds`；
- 方法 `_scan_emoji_library()` 与后台循环 `_meaning_scan_loop()`（含任务注册）；
- `handle_planner_emoji_meaning_scan` 里"库扫描开启时让位"的互斥判断。

补录通道收敛为**两条**：`planner 按需补录`（`maisaka.planner.before_request`）
与 `emoji.register.after_build_description` 即时补录。

> 删除前已确认：这些符号只出现在 `plugin.py` 自身（含 `modules/` 也无引用），
> 且 `database.query` 能力仍被 `_load_emoji_bytes_by_hash` 使用，所以能力声明不动。
> 修改前整份插件已备份到 `备份/cateye_better_post-processing_0.13.10/`。

### 三、兜底取图查询补 `image_type` 过滤

宿主的 `Images.image_hash` **只是普通索引（不是唯一）**，同一 hash 可以同时有 `emoji` 行与
`image` 行。`_load_emoji_bytes_by_hash()` 的兜底查询原先只按 `image_hash` 过滤 + `limit=1`，
可能取到已被清理（`no_file_flag=True`）的 `image` 行，从而误判"读不到图"——
尽管 `emoji` 行还有文件。现在补上 `"image_type": "emoji"`。

### 四、配置节与字段重排

- `[emoji_meaning]` 从配置页末尾移到 **`[response_splitter]`（分段）与 `[emoji_after_reply]`
  （回复后表情包）之间**；其它节 `__ui_order__` 依次后移。
- `generation_prompt` 移到 **`enabled` 的下一个位置**。
- 新的字段顺序：`enabled` → `generation_prompt` → `rewrite_scope` → `model_task` → `max_tokens`
  → `worker_interval_seconds` → `batch_size` → `max_attempts` → `max_meaning_length` → `meaning_ttl_days`。

### 五、去掉"试验性"标记

节标题、节描述、字段标签与 i18n 文案里的"（试验性）/ Experimental"全部移除；
`enabled` 仍默认 `False`（功能默认关闭这一点没变）。

### 其它

- 配置面 79 → **78** 项（删 2 加 1）；有范围 33 项不变；组件 14 不变。
- **库结构变更**：`emoji_meanings` 新增 `last_used_at` 列，**旧库自动迁移**（幂等）。

## [0.13.10] - 跟进宿主淘汰机制：库扫描纳入"未注册但文件还在"的表情包

### 先弄清宿主是怎么淘汰的（源码级）

| 路径 | 机制 |
|---|---|
| **库满替换**（唯一的"取消注册"入口） | `register_emoji_by_filename()`：`_emoji_num >= max_reg_num`（默认 **64**）且 `do_replace`（默认**开**）时，按 `1/(query_count+1)` 加权抽样候选 → **LLM 决策**取消注册哪一个 → `unregister_emoji()` → 注册新的 |
| `unregister_emoji()` | **只把 `Images.is_registered` 置 `False`，文件与描述全部保留**（docstring 原文："取消注册表情包状态，但保留文件和识别结果"） |
| 周期性缓存清理 | `emoji_cache_cleanup`（默认每 6h）：`emoji_file_retention_days`（默认 **30 天**）后删掉**未注册**表情包的文件并置 `no_file_flag=True`；再 30 天后**删记录**。**已注册的永不被该任务删** |
| 钩子 | 宿主**没有**"取消注册/删除"钩子（只有 `emoji.maisaka.before_select` / `after_select` / `emoji.register.after_build_description`），插件收不到通知，只能主动轮询 |

### 问题

`_scan_emoji_library()` 原来把 `is_registered == False` 的行**整个跳过**，理由写成
"未注册成功或文件缺失的行会一直取不到图" —— 但**取消注册并不影响"能不能读到图"**：
文件还在（30 天内），`data/images/<hash>.<ext>` 与 `Images.full_path` 都读得到。

结果是：**一张被取消注册的表情包，只要 30 天内没被补录，就永久失去机会**
（之后文件被清、记录被删、`emoji.get_random` 也抽不到），而它**以后还可能再被发出来**
（历史消息里的 `[表情包: …]` 标签也不会因宿主取消注册而改变）。

### 修复

过滤条件**只看"能不能读到图"**：`is_banned` 跳过、`no_file_flag=True` 跳过；
**`is_registered` 不再是条件**。限流照旧（`known_hashes` / `_meaning_queued` /
`_meaning_abandoned` 三重去重 + `batch_size` 每轮条数上限），不会一次性爆量。

### 明确不做的事

**不跟随宿主删除含义**。插件按 `image_hash` 存含义，而被取消注册的表情包**仍出现在历史消息里**，
含义依然有用。

### 其它

- 配置面（79 项 / 有范围 33 / 组件 14）与库结构**均不变**。

## [0.13.9] - 注入形态升级（标签/内容双标记 + 写明格式）+ 动图取首帧 + 跟风支持文本形态

### 一、注入文本形态

```text
0.13.8：[表情包: 困惑,疑问,不解,吐槽,无语][内容描述：一张聊天截图，…]
0.13.9：[表情包标签: 困惑,疑问,不解,吐槽,无语][表情包图片内容描述：这是一张gif格式的表情包。一张聊天截图，…]
```

- **标签显式叫"标签"**：宿主标签的标记从 `表情包` 改写成 `表情包标签`（**标签文字一字不动**）——
  模型本来就把它当"标签"（推理原话"她看到的是标签字"），那就**顺势明确**，让它不再有歧义。
- **内容块改名并写明格式**：`[内容描述：…]` → `[表情包图片内容描述：这是一张X格式的表情包。…]`，
  其中 X 是**宿主侧那份字节的真实格式**（gif / png / jpeg / webp），与宿主 `Images.image_format` 同源
  （宿主用 Pillow `img.format.lower()`，插件用魔数，两者同值）。
- 形态说明：`[表情包标签：…]` 与 `[表情包图片内容描述：…]` 都**不匹配** `EMOJI_LABEL_PATTERN`，
  所以重入不会重复处理（幂等判据也同步更新为"紧跟其后已有 `[表情包图片内容描述：`"）。

### 二、格式兼容（动图取首帧）

`normalize_image_for_vlm()` 现在：

| 输入 | 处理 |
|---|---|
| 静态 png / jpeg / gif / webp | **原样放行** |
| **动图**（多帧 gif / webp） | **取首帧转 PNG** —— 多数视觉模型对动图只会取首帧、甚至直接报错 |
| 静态但不支持的格式（bmp / tiff / avif…） | 转 PNG |
| 动图取帧失败（Pillow 缺失 / 解析异常） | 退回原图（好过整条放弃） |
| 不是图片 | `None`，跳过（不拿空图去问模型） |

新增 `is_animated_image()`。**入库格式改为"宿主侧原始格式"**（原来记的是"规范化后送给模型的格式"）——
否则动图转 PNG 后库里会记成 `png`，注入文本就会对模型说"这是一张png格式的表情包"，与宿主实际不符。

### 三、表情包跟风支持文本形态

`chat.receive.after_process` 的表情包判定现在是**两条并存**：

1. **组件形态**（旧判定，保留）：`raw_message` 里有 `type == "emoji"` 的组件；
2. **文本形态**（新增）：整条文本以 `[表情包` 开头、以 `]` 结尾
   （取 `processed_plain_text`，缺失时退回拼接文本段）——覆盖宿主把表情包渲染成文本、
   或组件形态不可用的场景，**多模态与文本模式都兼容**。

`[表情包: X]` / `[表情包]` / `[表情包标签：X][表情包图片内容描述：…]` 都算命中；
适配器降级占位 `[emoji]` **不算**（它不是 `[表情包` 开头）；`@阿米娅 [表情包: X]` 这类
"前后有字"的也**不算**（规则就是"整条即表情包"）。

### 四、其它

- 库方法 `meaning_for_hash` / `meaning_for_description` → **`content_for_hash` / `content_for_description`**
  （返回 `(含义, 图片格式)` 二元组）。
- 构造器 `build_emoji_content_suffix(meaning, image_format="")`、新增 `build_tagged_emoji_label(label)`、
  常量 `EMOJI_LABEL_TAG_MARKER = "表情包标签"`、`EMOJI_CONTENT_MARKER = "表情包图片内容描述"`。
- 配置面（79 项 / 有范围 33 / 组件 14）与库结构**均不变**。

## [0.13.8] - 保留宿主标签，改为在**后面追加** `[内容描述：…]`

### 变更

宿主原标签**一字不动**，只在它后面追加一个内容块：

```text
替换前：[表情包: 捏脸,宠溺,害羞,撒娇,亲密]
0.13.7：[表情包: 一只像素风格的暗红色小怪物（类似果冻状史莱姆）…]     ← 覆盖掉了原标签
0.13.8：[表情包: 捏脸,宠溺,害羞,撒娇,亲密][内容描述：一只像素风格的暗红色小怪物（类似果冻状史莱姆）…]
```

### 为什么要换形态（有实测依据）

0.13.7 上线后从请求快照与模型推理里查到两件事：

1. 标签**确实被替换了**（`reply/…` 快照 33 个标签里 30 个已是插件描述），模型也**确实读到了**
   （它的推理里写出了"**小怪物**"——这个词只存在于插件描述里）；
2. 但它仍然回"看不到"，推理原话是：
   > 但表情包内容她其实看不到（她说看不到）。所以不能描述表情包内容。**她看到的是标签字。**
   > 所以别提小怪物。删掉。

即：**它把 `[表情包: …]` 这个"外壳"归类成"标签"** —— 不管里面装的是 `捏脸,宠溺` 还是完整画面描述，
在它眼里都是"标签字"，于是"我只看到标签"这句话在它自己看来仍然成立。
换成**另一个标记** `[内容描述：…]` 后，这段内容在形态上就不再是"标签"了。

### 其它

- 信息**只增不减**：宿主标签保留，其它按 `[表情包: X]` 解析的代码完全不受影响
  （`EMOJI_DESC_PATTERN` 依旧只匹配 `[表情包: …]`，不会把 `[内容描述：…]` 当成描述）。
- **幂等**：追加后紧跟的字符就是 `[内容描述：`，重试时命中该前缀即跳过；
  `[内容描述：…]` 本身不匹配 `EMOJI_LABEL_PATTERN`，不会被重复处理。
- `build_emoji_label()` → `build_emoji_content_suffix()`；新增常量
  `EMOJI_CONTENT_MARKER = "内容描述"` 与 `EMOJI_CONTENT_SUFFIX_PREFIX = "[内容描述："`。
- 配置面（79 项 / 有范围 33 / 组件 14）与库结构**均不变**。

## [0.13.7] - 改用**就地替换标签**：把上下文里的 `[表情包: 标签]` 换成插件自己的描述

### 变更（按用户决策）

1. **删除旧的注入方式**：`maisaka.replyer.before_request` 往 `extra_prompt` 追加
   `【表情包准确含义参考】` 整段参考块的方案**整体移除**（含 `build_injection_block`、
   `_INJECTION_HEADER` / `_INJECTION_NOTE`、`_build_emoji_meaning_block`、`_meaning_request_type_allowed`
   及 `inject_enabled` / `inject_request_types` / `inject_max_emojis` / `injection_note` 四个配置项）。
2. **改为在请求 items 里就地替换**：新增
   `maisaka.replyer.before_model_request` 与 `maisaka.planner.before_request` 两个 **BLOCKING**
   处理器，把 `[表情包: 标签]`（含 `[表情包]` 空标签）的文本**替换为含义库里的描述**，
   再把改写后的 `items` 原样交还宿主。**planner 与 replyer 都已实现，默认只替换 replyer**
   （配置 `rewrite_scope`，可多选；留空表示不替换）。
3. **注入失败保持标签原样**：定位不到 hash、库里没有含义、或含义与现有标签相同，都**不改动**该处文本。

### 定位策略（两条路，先精确后回退）

- **路 A（精确）**：从 item 文本前缀的 `<message msg_id="…">` 取消息 id → 查入站时记下的本地映射
  `message_id → [(hash, 描述)]`（**0 RPC**）→ 按**组件顺序**与文本里第 i 个标签一一对应；
  仅当标签为空、或与本地映射记下的描述一致时才采信（避免错挂）。
- **路 B（回退）**：用标签文本当描述，走含义库的 `description` 索引查含义。
- 钩子内**不做 RPC**（宿主超时 6000ms 且每次请求都调），只用本地映射 + SQLite。
- 改的只是"即将发给模型的请求"，**不动宿主状态、不动数据库** —— 清空 `rewrite_scope`
  或卸载插件即可完全回退。

### 顺带

- 删掉随之成为死代码的 `_lookup_target_emoji_refs`、`_target_emoji_cache` 与
  `_TARGET_EMOJI_TTL_SECONDS` / `_TARGET_EMOJI_MAX_ENTRIES`。
- 新增 `EmojiMeaningStore.meaning_for_hash()` / `meaning_for_description()`
  （后者走已有的 `ix_emoji_meanings_description` 索引，同描述多行取最长含义）。
- 新增 `EMOJI_LABEL_PATTERN`（同时匹配 `[表情包: X]` 与 `[表情包]`，全角冒号也认）与
  `build_emoji_label()`。
- 配置面 82 → **79** 项（删 4 加 1），有范围项 34 → 33；组件（钩子）13 → 14。

### 已知边界

- **适配器降级成 `[emoji]` / `[image]` 的媒体替换不了**：宿主侧根本没有 emoji 组件、
  没有 hash，插件库里也没有这张图的含义 —— 那是更上游（适配器）的问题。

## [0.13.6] - 让模型把"看不到图 / 知道内容"如实说清（而非改口说看得到）

### 诊断（完整请求日志 `1790156789978.json`）

**先说结论：模型说"看不到"是对的，不能让它改口。**

| 证据 | 内容 |
|---|---|
| `request_items` 的 part 类型统计 | **77 个全是 `text`，0 个 `image`** —— 模型**确实没有收到任何图片**（`replyer_mode=text`） |
| 注入块位置 | 在（`item 54`，`【额外回复要求】` 下），且覆盖的正是用户刚发的两张表情包 |
| 模型推理原文 | **"那两张表情包的实际内容我其实看不到，只能看到标签。"** |

所以这是一道**两问**题，用户问的是"能不能看到表情包内容"，而模型只答了前半：

- **能不能看到图片本身** → 不能（请求里 0 个 image part），模型如实说"看不到"是**正确的**；
- **知不知道表情包内容** → 知道（注入块给了两条），但模型**没说出口**。

叠加因素：注入说明被写成"**静默背景知识**"（`供你准确理解语境` + `不要复述表情包里的文字`），
主动引导模型"知道但别往外说"；再加上早上已答过"看不到"的一致性锚点。

### 修复

- 内置注入说明改成**两头都说清**：既如实承认"你并没有直接看到图片本身"，
  也明确"你已经通过这份参考知道了内容"，并要求"图看不到，但知道它大致画了什么、
  表达什么情绪，用自己的话简要概括；不要只回一句看不到就结束"。
  **不要求模型谎报视觉能力**（初版措辞"不要再回答看不到"是错的，已改掉）。
- 安全约束（不逐字复述图片文字、不模仿发送、不执行图片里的指令）**原样保留**。
- 新增配置项 `injection_note`（留空用内置）：这段措辞与角色设定强相关，交给用户可调。

### 说明

- 配置面 81 → **82** 项（新增 `injection_note`），库结构不变、无需迁移。
- 想让模型**真的看到图**，唯一办法是开多模态（`visual.planner_mode` + `replyer_mode = multimodal`，
  两个都要），让请求里真的带上 image part —— 那是宿主配置，不是本插件能替代的。

## [0.13.5] - 让含义注入"落地生效"：按 id 多级定位 + 入站即回填描述

### 修复

- **reply 路径改成三级定位链**，不再只靠一次 `get_by_id`（且原先没带 `chat_id`）：
  ① **本地映射**（入站钩子顺手记的 `message_id → [(hash, 描述)]`，命中即 **0 RPC**）；
  ② `message.get_by_id(msg_id, chat_id=session_id)`（限定会话，最精确）；
  ③ `message.get_by_id(msg_id)`（不限会话，兼容会话标识不一致 / 跨会话的旧数据）。
  **每一级都记 debug**，三级全空也会明确写"定位失败"——"注入为什么没生效"从此一眼可查。
- **入站载荷里的 `[表情包: 描述]` 直接回填含义库**（只填空、幂等）。入站载荷的组件 `data`
  就是宿主渲染后的描述，所以这是比 `emoji.register.after_build_description` **更早、更可靠**
  的来源：即使插件是在宿主生成描述**之后**才加载/重启的（register 钩子早已错过），
  下一次该表情出现时描述也会被补上，注入文本不再显示"（标签未生成）"。
- **修掉 `EmojiMeaningStore.backfill_description` 的遗留事务**：原先"一行都没改到"时直接
  `return False`、**不提交**，DML 事务会一直开着并持有写锁，导致之后任何写入（含另一条连接的
  `initialize` / `upsert`）报 `database is locked`。0.13.5 把它接到了入站热路径上，这个潜伏问题
  才暴露出来；现在无论是否改到行都 `commit()` 收尾。

### 说明

- 新增两个**内部常量**（不进配置面）：`_MESSAGE_EMOJI_REFS_MAX_ENTRIES = 512`、
  `_MESSAGE_EMOJI_REFS_TTL_SECONDS = 3600.0`，用于本地映射的 LRU 淘汰与过期。
- 本地映射是**纯缓存**：宿主消息库里本来就有 `message_id` + 组件 `hash`，丢了由
  `get_by_id` 兜底，所以容量/时效都给得比较紧，不需要落库。
- **配置面不变**（仍是 81 项），库结构不变、无需迁移。

## [0.13.4] - 补全"按需补录 / 含义注入"的静默路径日志

### 修复

- 线上排查"bot 说看不到表情包"时发现：插件的两条链路（planner 按需补录、含义注入）在
  大多数情况下是**静默返回**的，日志里完全看不出"是没找到表情包 / 是找到了但没含义 /
  是媒体压根没送到宿主"。现在每条静默路径都记一条 `debug`：
  - 本轮上下文的媒体是**适配器降级成的文本占位**（`[emoji]` / `[image]` / `[视频]` /
    `[文件]` / `[json]` / `[forward]`）——这类消息在宿主侧没有 emoji 组件、没有 hash、
    没有字节，含义库无从下手，日志里会直接点出是哪种占位；
  - 时间窗内没有带 hash 的表情包；
  - 本轮表情包都已有含义 / 已在队列 / 已放弃；
  - 本条回复没有可关联的表情包引用；
  - 关联到的表情包引用都还没有含义。
- 新增 `_planner_media_placeholders()` 用于识别"适配器降级占位"，只用于日志、不改变行为。

### 说明

- 只加日志，**不改任何判定逻辑、不改库结构、不改配置项**。

## [0.13.3] - 回填"生成含义时宿主还没出描述"的记录

### 修复

- 入站主链 `enable_heavy_media_analysis=False`，新表情首次出现时上下文里只有 `[表情包]` 占位、
  宿主也还没生成描述，于是含义是在"描述为空"的情况下按 hash 算出来的（0.13.0 起设计上允许，
  实测线上 4 条里有 2 条就是这种情况）。**原先描述永远不会被补上**，导致这些记录在注入文本里
  只能显示"（标签未生成）"，LLM 没法把它和自己看到的 `[表情包: 描述]` 对上。
  现在宿主稍后把描述补出来时（触发 `emoji.register.after_build_description`）会把描述
  **回填**进含义库（只填空、不覆盖已有值）。

### 升级提示

- `version` / `config_version` → `0.13.3`；配置项与能力声明都没变，改完即生效。
- 含义库结构没动，无需迁移；已存在的空描述记录会在该表情再次触发描述构建时被填上。

---

## [0.13.2] - 修复「放弃名单被每轮 planner 复活」/ 注入成功打日志

### 修复（0.13.0 引入的回归）

- **放弃名单被自动复活，导致无限重试刷日志**：0.13.0 新增的 planner 按需补录与库扫描都在入队前
  `_meaning_abandoned.discard(hash)`，于是"连续失败 `max_attempts` 次后被放弃"的表情包会在**下一轮
  planner 决策时被重新入队**——线上实测：06:15 放弃 4 个，06:38 又"4 个缺含义已入队"，如此循环。
  现在两条扫描通道都**不再复活**放弃项（与 0.12.x 文档一致：复活的唯一入口是
  `emoji.register.after_build_description`，即该表情包重新注册）。

### 变更

- **含义注入成功时打一条 info 日志**：`表情包含义注入：N 条（会话 …）`——这是"注入到底有没有生效"
  的唯一直接证据，否则只能从 replyer 的输出反推。

### 升级提示

- `version` / `config_version` → `0.13.2`；**配置项与能力声明都没变**，改完即生效。
- 放弃名单是**进程内内存态**，重启即清空，所以修复后重启一次即可让之前被误放弃的表情包重新排队。
- 若你仍在日志里看到 `不受支持的图片格式`，先确认插件版本：日志里
  `插件 github.cateye.better-post-processing vX.Y.Z 加载成功` 必须是 **0.13.1 及以上**
  （该报错是 0.13.0 的取图元组顺序缺陷，已在 0.13.1 修掉）。

---

## [0.13.1] - 修复含义生成「不受支持的图片格式」/ 上传前统一转格式

### 修复（0.13.0 引入的回归）

- **`不受支持的图片格式`**（线上 06:14 报错，4 条含义生成同时失败）：0.13.0 新增的三条取图路径
  返回的元组顺序不一致——`_sample_emoji_images` 是 `(base64, 格式)`，而 `_read_image_file` /
  `_load_emoji_bytes_by_hash` / `_take_inline_emoji_bytes` 是 `(格式, base64)`。于是走"按 hash 读
  宿主图片文件"这条路的表情包会把 **base64 串当成格式**传给宿主，宿主 `ContextImagePart` 校验
  （`SUPPORTED_IMAGE_FORMATS = jpg/jpeg/png/webp/gif`）当场抛错。
  现在三条路**只回传 base64 字符串**，格式一律由单一入口 `normalize_image_for_vlm()` 给出，
  从结构上消除"元组顺序搞反"这类错误（回归测试里加了一个**严格校验的假视觉模型**：
  格式必须在白名单内、且必须与字节魔数一致，否则直接抛错）。

### 变更

- **上传前统一转格式**（`emoji_meanings.normalize_image_for_vlm`）：

  1. base64 规范化（去空白、补 padding），保证宿主 `b64decode(..., validate=True)` 能解；
  2. 按**真实魔数**判定格式：png/jpeg/gif/webp 直接放行（并顺手修正调用方可能传错的格式串）；
  3. 其余可解析图片（bmp/tiff/…）用 **Pillow 转 PNG**（只取首帧）；
  4. 既认不出又转不了的字节**跳过**并记日志——不再喂空图给视觉模型（原先宿主会把这种片段换成
     `[图片内容不可用]` 文本占位，模型照样"编"一条含义出来，属于静默错误）。

- 删掉旧的 `sniff_image_format()`：它对未知魔数**一律回退 "png"**（撒谎）。取而代之的是
  `detect_image_format()`（认不出返回空串）与 `normalize_image_for_vlm()`。
- manifest 依赖补 `Pillow >= 11.3.0`（转格式用）。已用宿主 `PluginDependencyPipeline` 实测：
  `plan.installs=[]`、`blocked=[]`，与主程序 `pillow>=11.3.0` 约束一致、**不会触发 pip 安装**。

### 升级提示

- `version` / `config_version` → `0.13.1`；**配置项与能力声明都没变**，改完即生效。
- 只影响 `[emoji_meaning] enabled=true` 的用户；含义库结构没动，无需迁移。

---

## [0.13.0] - 表情包含义库改为按哈希精准存储 / 新增 planner 按需补录

### 新增

- **`[emoji_meaning] scan_enabled`（启用库扫描补录，默认 `false`）**，位置在「生成 max_tokens」与
  「库扫描间隔（秒）」之间。**默认关闭时改走 planner 按需补录**：planner 每次决策前扫一遍
  **本轮上下文里出现的表情包**，只给其中缺含义的那些生成——token 消耗随实际使用量走、
  不随表情库规模走。开启后按 `scan_interval_seconds` 扫全库（原行为），此时按需通道自动让位。
- planner 按需补录的实现：从 `maisaka.planner.before_request` 的 `items` 里找出带 `[表情包…]`
  的上下文项、收集 `meta.timestamp`（历史项的时间戳就是原始消息时间），再用
  `message.get_by_time_in_chat`（**不带二进制**）拉回该时间窗内的消息——消息里的 emoji 组件
  自带 `hash`，所以补录是"按 id"的、不依赖描述是否已经生成。同会话 15 秒节流，上下文没往后走
  也不重复取消息。

### 变更

- **含义库主键从 `description` 改为 `image_hash`（sha256）**：宿主全链路的图片身份就是
  `EmojiComponent.binary_hash` = `Images.image_hash`（宿主注释写明"亦作为图片唯一ID"），
  而描述只是视觉模型生成的情绪标签——表情库维护会按描述"取消注册旧的、注册新的"，
  同一条描述可能被换到另一张图上，按描述存会把含义错挂给新图。现在 `description` 只是
  展示字段（另建索引，供旧数据的回退查询）。
- **描述缺失也能注入**：入站主链 `enable_heavy_media_analysis=False`，新表情首次出现只有
  `[表情包]`；此时靠组件 `hash` 照样查得到含义，注入文本里的标签由含义库记录补上。
- **取图改为按 hash**，三条路按成本排序：① 入站载荷自带的 `binary_data_base64`（顺手缓存、
  用后即删）；② 按 hash 读宿主图片文件（`data/images/<hash>.<ext>` 或 `Images.full_path`，
  带**根目录包含校验**与 8MB 体积上限，Docker/远程部署读不到就自然失败）；③ 仍拿不到才退回
  `emoji.get_random` 抽样——抽样载荷不含 hash，改在**本地对抽到的 base64 算 sha256** 与目标
  比对（原先按描述匹配，同描述多图时会串味）。
- 注入、入队、失败计数、放弃名单、`emoji.register.after_build_description` 钩子全部改为按
  `image_hash`；会话近期缓存改为存 `(hash, 描述)`，`_recent_emoji_descs`（表情包跟风用）
  改为从它派生，行为不变。库扫描入队同样按 `image_hash`（Images 行本身同时给出 hash 与描述）。
- 修正 0.12.2 及更早版本的一句误判：文档与代码注释写着"宿主公开能力不返回表情包哈希，
  无法按哈希精确取图"——`emoji.*` 能力确实不返回 hash，但**消息组件、`Images` 表与注册钩子
  都带 hash**，按 id 存储与注入一直是可行的。

### 升级提示

- **旧库自动迁移**：0.12.2 及更早的库以描述为主键（`image_hash` 列一直是空串），首次启动会
  重建表并把旧行落到 `desc:<描述>` 遗留键上；查询时 hash 未命中再回退到它。**升级不丢数据、
  不需要人工处理**。
- 本功能是**试验性**且默认关闭（`[emoji_meaning] enabled`）；未开启时以上变化都不生效。
- 新增一项布尔配置，`config_version` → `0.13.0`；**能力声明未变**（仍只用 `database.query` /
  `message.get_by_id` / `message.get_by_time_in_chat` / `emoji.get_random` / `emoji.get_count` /
  `llm.generate`），改完即生效；manifest 版本号有变化，插件列表里想立刻显示新版可重启一次。

---

## [0.12.2] - 精简配置板块的说明文字

### 变更

- **13 个配置板块的「标题下说明」全部压成一句话**（原来 `[插件]` 144 字、`[错别字]` 520 字、
  `[分段]` 161 字、`[引用回复]` 69 字……），WebUI 配置页每个板块标题下不再是一大段文字。
  原因：板块说明取的是配置类的 `__doc__`，而 `maibot_sdk` 只做 `strip()`、**不做 dedent**，
  多行 docstring 会带着源码缩进显示成一坨。
- 被删掉的设计说明**降级为类上方的 `#` 注释**保留（例如「为什么 `filter_fake_at` 放在
  `[plugin]`」「错字概率只有一个，另一半门控已绕过」「分段没有"多条发送"开关」
  「表情包冷却由哪些来源触发」），维护者仍能在源码里看到来龙去脉。
- 顺带修掉一条**过期的英文板块描述**：`[chinese_typo_weights]` 的英文说明还写着
  "only used when correction modes are on"，而那个开关 0.12.0 已经移除。
- 排版原则里补了第 7 条，把"板块 docstring 只写一句话"写成明文约定。
- **没有配置项增删改**，`config_version` 仅随版本号前进（迁移是空操作），改完即生效。

---

## [0.12.1] - 新增「过滤假 @」（LLM 手写在正文里的文本 @）

### 新增

- **`[plugin] filter_fake_at`（过滤假 @，默认 `true`）**：LLM 会在正文里手写 `@某人`，
  那只是**普通文本**——宿主与适配器都**不会**把它转成真实 at 段（宿主里 `str → AtComponent`
  只有反序列化与显式构造两条路，没有"按昵称查用户"的逻辑；适配器的文本段原样下发），
  所以 QQ 里它不会提醒任何人。留着它再叠上「引用回复接管」抽到 @ 时注入的**真实** at，
  一条消息里就会出现两个 @（0.11.3 线上事故）。开启本项后，插件在出站前把**消息开头**的
  「@某人 + 空格」整段删掉，于是真实 @ 成为消息里唯一的一个。
- **两个限定条件同时满足才动手**：① 在**消息正文最前面**；② `@名字` 后面**跟着空格**
  （半角/全角空格、Tab、NBSP 都算，不含换行）。所以 `a@b.com`、`@某人，` 这类写法不会被误伤；
  删完只剩空白时**不删**（免得把消息发成空的）。
- **作用范围与文本规则一致**：只动 **bot 本轮回复自己发出的消息**（含多段发送由插件补发的分段），
  其它插件用 `ctx.send.*` 直接发出的文本一律不动。
- **受「丰富回复门控」控制**（与两个接管同口径）：宿主开着 `experimental.enable_rich_reply`
  且 `[plugin] rich_reply_gate` 开启时，本项不动任何文本；把 `rich_reply_gate` 关掉后照常生效。
- 过滤发生在「文本规则」与「回复方式抽取」**之前**，日志会打一行
  `已过滤正文开头的假 @：'@凯特艾 ' → '行吧多想了'（会话 …）`。
  关掉本项时，0.12.0 的 @ 判重仍然兜底（正文开头的字面 `@提及` 命中本轮目标就升级成真实 at
  组件、@ 的是别人则跳过注入），所以一条消息里也不会出现两个 @。

### 升级提示

- `version` / `config_version` → `0.12.1`；新增一项布尔配置（默认开启），其余值全部保留。
  没有删除或改名任何配置项，**改插件配置即生效、不需要重启**；manifest 版本号有变化
  （能力声明未变），宿主插件列表里想立刻显示新版可重启一次。

---

## [0.12.0] - 错字纠正改由权重池恒定决定 / @昵称防错字 / 撤回反应时间

本版按需求重排错字纠正链路：**接管生效后纠正方式完全由权重池决定**（不再是"宿主原逻辑 / 插件分支"
二选一），并修掉两个线上问题。**配置项有增删，升级后请过一眼 `[chinese_typo]` 与
`[chinese_typo_weights]`。**

### 新增

- **`[chinese_typo] recall_delay_seconds`（撤回反应时间，单位秒，支持小数点，默认留空）**：
  某处错字按「撤回重发」纠正时，先等这么久再撤回（模拟"发出去才发现打错"）。
  留空 = **按打字速度自动**：拿被撤回那条消息的正文按宿主的打字公式算一遍
  （中文 `0.3s/字`、其它 `0.15s/字符`，乘生效的【打字速度】）；`0` = 立刻撤回。
  实际等待时长会打进日志：`撤回反应时间 2.40 秒后撤回消息 xxx（会话 …）`。
- **`[chinese_typo_weights] last_correction_probability`（最后纠正概率，默认 `1`，范围 `0~1`）**：
  取代原「权重 · 最后纠正」，语义见下面「变更」。

### 变更

- **「错字纠正方式」开关 `correction_mode_enabled` 移除**：只要后处理接管生效，纠正方式就一律由
  `[chinese_typo_weights]` 的四个权重（直接发送 / 引用纠正 / 撤回重发 / 不纠正）决定；四个都设 `0`
  即"完全不纠正"。宿主镜像项 `enable_correction_quote`（错别字纠正时引用原消息）与
  `correction_quote_probability`（错别字纠正引用概率）**一并移除**——引用与否也改由权重池决定，
  否则"抽到引用纠正"会被那两个开关悄悄降级成直接发送。
- **「最后纠正」从"一种纠正方式"变成"一种时机"**：`last_correction_enabled`（默认 `false`）打开后，
  抽到的纠正按 `last_correction_probability` 决定是否推迟到**本轮全部分段发完之后**执行；
  推迟后**依然按抽到的那个方式纠正**（直接补发 / 引用纠正 / 撤回重发），**不再强制引用**；
  抽到「不纠正」则什么都不会发生。
- **修复：打了错字却既不纠正也不撤回**（线上 02:32:47 实测）。根因是宿主那句硬编码的
  「50% 才给纠正建议」门控——没抽到建议就**不会产生任何纠正动作**，权重池设成什么样都没用。
  接管分支现已绕过该门控（`create_typo_sentence(force_suggestion=True)`）：**每处真正出现的错字
  都一定会抽一次纠正方式**。错字是否可见仍沿用宿主另一半 0.5 门控（未改动），
  所以 `error_rate` 依旧是"错字总频率"的唯一开关。
- **修复：`@昵称` 被错字污染**（线上同一条消息里出现 `@凯特艾 @凯特爱` 两个 @，像同时 @ 了两个人）。
  正文里的 `@某人` 现在**不参与错字生成**（占位符保护，见 `post_processing._protect_at_mentions`）；
  同时 `quote_takeover` 会识别正文开头的**字面 @提及**：命中本轮目标就把它**升级为真实 at 组件**
  （不再额外补一个 @），@ 的是别人则**跳过 @ 注入**——一条消息里不会再出现两个 @。
  这是本插件与宿主**有意不同**的一处（第二处，第一处是整词同音替换的组合数上限）。
- **移除 `[chinese_typo] recall_purge`（撤回后清理原消息）**：实测**无效**——宿主发出的第 1 段由宿主
  自己写历史、插件拦不住，删库也改不了内存上下文。连带删掉 `Messages` 表清理代码与 manifest 里的
  `database.delete` 能力声明；插件自己补发的分段在"即将被撤回"时**照旧**不写进 bot 自己的历史
  （让上下文只留修正句），这部分本来就能生效。
- 诊断日志口径更新：`后处理接管统计` 不再打印已失效的"抽到纠正建议 / 宿主原逻辑"，
  改为 `真出现错字=N 句、判定纠正=N 处（纠正方式=权重池）`；
  `后处理接管参数` 也不再打印那两个已移除的引用开关。

### 升级提示

- `version` / `config_version` → `0.12.0`。被移除的 4 个字段（`correction_mode_enabled`、
  `enable_correction_quote`、`correction_quote_probability`、`recall_purge`）与旧的
  `weight_correction_last` 会由迁移逻辑丢弃，其余值全部保留。
- manifest 能力声明有变化（去掉 `database.delete`），**建议完整重启 MaiBot**。

---

## [0.11.3] - 新增「强制直发阈值（条）」/ 过旧规则说明与注释梳理

### 新增

- **`[quote_reply]` / `[quote_reply_private]` 新增 `force_direct_threshold_messages`（强制直发阈值，条，默认 `0` = 关闭）**：
  回复目标之后已出现的消息数**未达到**该值（对话还很新）时，本次回复**强制直接回复**——
  既不引用也不 @（私聊本就不 @）；达到该值后回到正常权重抽取。与按时间判定的
  `stale_age_seconds`（目标超时后强制直接回复）互补，两条"强制直发"规则都优先于
  「对话已推进」调整。
- **让步规则**：强制直发阈值必须**小于**「对话已推进阈值」（`stale_threshold_messages`，
  它决定消息是否被剔除直发）。配置值 ≥ 旧阈值时自动为旧阈值让步，压到
  「对话已推进阈值 − 1」生效（例如旧阈值 `3`、本项填 `4` → 按 `2` 生效；旧阈值为 `1` 时压到
  `0` = 关闭）。旧阈值为 `0`（未启用对话已推进判定）时不设上限。首次压制时打一条 info 日志，
  后续降为 debug。
- 两条条数阈值（强制直发 / 对话已推进）共用同一次消息数统计，查询上限取
  `max(两个阈值) + 1`；群聊与私聊阈值仍各自独立。

### 变更

- 重新梳理引用回复板块既有配置项的翻译与注释，消除歧义：`stale_threshold_messages` 的
  字段说明/`x-toml-comment`/英文 hint 明确为"≥N 条才算对话已推进：不再直发、@ 权重按权重板块调整"，
  并注明与「强制直发阈值」的互补关系；`stale_exclude_bot_messages` 注明两条条数阈值共用同一次
  统计；`stale_age_seconds` 注明"按时间判定的过旧规则，优先级高于按条数判定的两条阈值"。
- README 配置表同步：取值范围表、`[quote_reply]` / `[quote_reply_private]` 字段表补入新字段，
  过旧规则相关说明改为三条规则口径（超时 > 强制直发条数 > 对话已推进）。

---

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
