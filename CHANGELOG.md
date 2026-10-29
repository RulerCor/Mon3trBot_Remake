# 开发日志

## v1.9.3 — 独立图片补全、识图空描述兜底与补丁插件刷屏修复（2026-10-30）

### 修复

- **独立图片（非引用）整条被丢**：Alpha 的 SSE `message.new` 只给 10 个字段
  （`accountId`/`avatarUrl`/`content`/`event`/`eventId`/`groupName`/`rawid`/
  `sessionId`/`sessionType`/`sourceName`/`timestamp`），**不含** `refs`/
  `source_type`/`media`，独立图片消息的 `content` 因此为空，旧逻辑直接判
  「空内容」整条丢弃，bot 永远看不到群友发的图。现在收到这类事件时按
  `rawid` 在后台回查历史补全媒体信息再入缓冲：命中媒体才补成 `[图片]`
  （并发闸 `empty_content_lookup_concurrency` 保护 `/api/v1/messages`），
  查不到或不是媒体照旧跳过，绝不注入幻觉内容。回查在网络线程里做，
  不阻塞 SSE 接收线程。
- **`alpha-media://sha256/<h>` → `/api/v1/media/<h>` 映射**：部分响应
  （尤其是 SSE 归一后的）只带 `refs[].uri`，不带 `mediaUrl`。实测两者指向
  同一份字节，补上映射后这类图片才拿得到可下载 URL。
- **识图空描述静默丢图（akasha_rc_patches v1.3.5）**：`_ensure_img_caption`
  只在描述非空时才写 `<image_caption>`，但 `finally` 无条件清空
  `req.image_urls`。于是当 caption provider 返回空正文时（典型如推理模型
  把 `max_tokens` 全花在思考上、`finish_reason=length`），图片会**无痕消失**，
  模型既看不到图也收不到任何提示。插件运行时补丁现在只补这一个缺口：
  有图却没有任何新内容片段时，追加与异常分支相同的 `[Image Captioning
  Failed]` 标记，让模型仍能回应附件。上游成功/异常路径结构不变，只增不减。
- **补丁插件日志刷屏**：`_apply_hotpath_patch` 的 1Hz 有限重试窗口此前每次
  都会重复打「事件循环热路径已应用」成功行；`_patch_image_status_cache`
  在子补丁已就位时仍返回结果，导致重试窗口内的 pending 行每秒重打一遍。
  现在子补丁就位后返回 `None`，成功行与接口诊断各只打一次，就位后立即
  停止重试窗口。

### 变更

- **发行包媒体指纹脱敏**：`wechat-weflow-bridge-ob11/wx_msg_parser.py` 的示例 docstring 内残留真实 `emoticonmd5` / `emojiinfo` / 引用图 `md5` / `cdn` 前缀共 4 处（构建期脱敏表只覆盖 wxid/群名/人名，覆盖不到媒体字段），现已换为等价占位符，并在 `scripts/build_dist.py` 加入媒体指纹兜底扫描（md5/aeskey/cdn url/emojiinfo/svrid），命中则中止出包。
- **引用图片夹具脱敏**：`tests/fixtures/quote_image.xml`、
  `scripts/test_quote_image.py` 与 CHANGELOG 中的真实 `svrid`/`md5`/
  `aeskey`/`cdn` 串/`uuid`/`signature` 换为等长合成值，断言同步更新。

### 验证

- **发行包重压校验**：只替换目标文件、其余 35063 条目逐字节零差异，`testzip()` 无损坏，`scripts/audit_dist.py` 全项 PASS（隐私/密钥/人格/知识库/启动器/版本）。
- **媒体指纹扫描复验**：移除过宽的合成值允许项后，真实 md5/图片 md5/emojiinfo/cdn url/aeskey/svrid 六类样本全部命中，占位符与 docstring 样本零误报（本仓扫描 `source dir` / `scripts` 均 0 残留）。
- `scripts/test_quote_image.py` 17/17 通过（解析层 / 投递层 / 降级层，
  使用脱敏后的合成夹具）。
- `scripts/test_standalone_image.py` 20/20 通过（归一化层 / 映射层 /
  投递层 / 异步入缓冲 / 降级层）。
- **运行旧代码确认为测试结果假象**：bridge 进程启动于改动落盘之前，本轮
  图片修复在用户当时的测试里尚未生效；重启后新路径才生效。
- **真机模型探测**：`aggregate/deepseek-v4-flash-vision-exp` 直连返回
  `content=""`、`finish_reason=length`（reasoning 吃光 `max_tokens`），
  与「api 没反应」现象一致；`aggregate/deepseek-v4-1-flash`、
  `aggregate/deepseek-v4-flash` 均正常返回中文图片描述。

## v1.9.2 — 回复链路提速与引用图片修复（2026-10-29）

### 修复

- **引用图片收不到（桥接丢弃图片元数据）**：微信引用一张图片时，被引用正文只是一句「〔引用 某某：[图片]〕」，旧 `wx_msg_parser._parse_quote` 只判 `<img` 就写死 `[图片]`，`md5`/`svrid`/`length` 全丢，AstrBot 侧 `_private_event_has_image` 为 false、永远不触发识图，模型只能答“看不清”（事故日志 21:15:02 收到 quote、21:15:07 只推送 1 条消息 + 0 张图片）。现在解析层用 `_raw_tag` 单独取未解码的 `refermsg`（避免内嵌 XML 标签“浮”上来误命中），解出 `svrid`/`md5`/`aeskey`/`size`/`url_kind` 存入 `ParsedMessage.extra["quote_image"]`，`text` 结构保持不变。
- **引用图片回查与下载**：`bridge_core._handle_rich_message` 对 `KIND_QUOTE` + `quote_image` 走 `_fetch_quote_image_segment()`，用 `refermsg.svrid` 命中 Alpha 历史里同一条图片消息（该消息自带已 resolve 的 `media.resources[].url`，无需触碰 aeskey/CDN 解密），下载后作为标准 OneBot image 段与文字一起投递，AstrBot 原生识图路径即可生效。`/api/v1/messages` 支持 `offset` 分页，`message_source.fetch_messages` 三处实现补 `offset` 参数（WeFlow 收下不转发）；每页 `limit=50`（>100 会触发 422 `media.hydration_budget_exceeded`），`config.QUOTE_IMAGE_SEARCH_PAGES`（默认 2 页=最近 100 条）只在引用图片这条路径上翻页，不影响普通取图。
- **能力不缩水**：图片下载失败或没有 `svrid`（例如引用的是文本）时，`[图片]` 文本照常推送，只是没有 image 段 —— 行为退回改动前，只增不减。
- **回查不阻塞消息接收**：回查历史实测每页 ~4s，翻 2 页 + 下载接近 10s。第一版把下载写在 SSE 接收线程里，会把这 10s 加到**所有会话**的消息接收上。现在改成收到消息时立即在后台线程并发启动回查（`_start_quote_image_fetch`），与 5s 缓冲窗口重叠，推送时只等剩余时间（`_await_quote_image_segment`，上限 `QUOTE_IMAGE_FETCH_TIMEOUT`=20s）：启动调用实测 **6ms** 返回，典型总回查 6.1s，净增推送延迟约 1s，接收线程全程不被网络 I/O 占住。每个会话独立排队，慢会话只影响自己。

### 优化

- **原生引用回复提速**：右键气泡命中比例按最近一次成功值优先复用，候选坐标不减少、引用预览校验保留；右键后立即轮询菜单，去掉成功路径的固定 0.45s 等待；发送清空校验改为分层等待（最长仍为 0.45s）。
- **AstrBot 上下文加载优化**：LLM 工具 schema 由 `akasha_rc_patches` 在运行时切到 `skills_like` 两阶段模式，所有工具能力保留，但首轮不再下发 81 个完整 JSON Schema；由 `akasha_rc_patches` 在运行时启用 AstrNa 现有动态 system prompt 迁移模块，把观察到的动态注入移到临时 user content；动态时间提醒本就位于每轮 user content，不污染 system prompt 前缀，利于 prompt cache 命中。
- **MemoryCompanion 请求钩子保底**：由 `akasha_rc_patches` 提供 8s 总预算兜底与阶段耗时日志，避免记忆检索/重排异常把整轮回复拖进 30~60s 黑洞；rerank 超时从 3s 收紧到 1.2s；阶段性总结 fallback 不再使用余额受限的 tokenrhythm，改为当前可用的 antie5 v4.1 flash。
- **AstrBot 钩子链诊断（akasha_rc_patches v1.3.3）**：为所有事件钩子加 request 级耗时日志，超过 1s 的钩子会记录插件、handler、优先级和耗时。
- **AstrBot 内建群上下文阻塞修复（akasha_rc_patches v1.3.4）**：钩子级计时抓到 `astrbot.decorate_llm_req` 单次耗时 **21.3 秒**，根因是内建 `GroupChatContext` 对每条入站群图片同步调视觉模型补描述，且 `on_req_llm` 与 `handle_message` 抢同一把会话锁。运行时改为：图片描述做 24h 内容寻址缓存 + 12s 超时降级、注入路径改无锁快照；实测钩子耗时从 21s 降到秒级。
- **Image Companion 事件循环阻塞修复（akasha_rc_patches v1.3.3）**：watchdog 抓到 `ImageGenerationRuntime._sdgen_photo_available()` 走 `_find_sdgen_plugin()` 的 `gc.get_objects()` 全量扫描，单次阻塞事件循环 21~36 秒，是回复慢的主因；运行时接管改为只查 AstrBot 注册表并加 30 秒生图能力状态缓存，SDGen 可选能力保持可用，彻底移除这条 GC 热路径。
- **主回复识图超时上限（akasha_rc_patches v1.3.5）**：内建 `GroupChatContext.get_image_caption` 已被 v1.3.4 加了 12s 超时，但**主回复路径**（`astr_main_agent._request_img_caption`）此前完全没有上限 —— provider 自身 `timeout=120s` 加多次重试，最坏能让单条回复卡住数十秒到两分钟。v1.3.5 统一给识图调用加 12s 上限（可用插件配置 `image_caption_timeout_seconds` 覆盖），超时走各调用点原有的失败分支，与「上游失败」降解方式完全一致，能力不缩水；同时群图片描述遇到空白返回不再写缓存，避免产出 `[Image: ]` 空描述。

### 验证

- `uia_sender.py` 语法自检通过；`sync_to_rc.py` 同步与语法自检通过。
- 引用图片回归测试 `scripts/test_quote_image.py` 17/17 通过（解析层 / 投递层 mock 下载 / 降级层）；`test_bridge_rich_msg.py` 12/12 通过无回归。
- 生产配置实测回查：引用图片的生产 `svrid` 命中历史记录并下载到 **88000 字节** PNG，与引用 XML 的 `length` 精确一致（端到端验证通过，未发送任何消息）。
- 并发回查实测：`_start_quote_image_fetch` 返回耗时 **6ms**、后台回查 6.10s、结果 image 段正常生成 —— 接收线程不再被阻塞。

## v1.9.1 — Alpha 媒体链路与事件循环延迟修复（2026-10-27）

### 修复

- **Alpha 新旧字段差异归一**：兼容 `source_type` / `sent_at_unix_ms` /
  `conversation_id` / `sender_id` / `text` 等字段，图片事件不再因只有
  `media` 而没有正文被判成“空内容”。
- **图片 URL 解析兼容两套结构**：优先使用 SSE / 历史事件自带的
  `mediaUrl`，同时支持 `media.resources[].url`；回查历史时按消息 ID
  精确匹配，避免同一会话连续来图时取错。
- **顶层 `<msg><emoji ...>` 解析**：自定义表情可直接提取
  `md5` / CDN 直链 / 大小并归一为 `sticker`，不再落入
  `[无法解析的消息]`。
- **发送结果判定补严**：文字、引用、图片降级文件、普通文件、表情和
  引用前缀都按发送器真实返回值记录成功/失败，不再打印“已发送”造成假成功。
- **事件循环热路径接管（`akasha_rc_patches` v1.3.2）**：
  - Image 的 SDGen 插件发现增加 15s 缓存，避免每条回复触发
    `gc.get_objects()` 全量扫描；
  - REQ-041 快照复制移出事件循环，保留原有数据锁与快照语义；
  - 延迟补丁在目标插件加载完成后接管；Image 不可用时允许 REQ-041
    独立生效，避免永久停在待应用状态；
  - Image 插件加载事件补上无效缓存清理，修复启动顺序导致的
    `private_api=ready image_runtime=pending`。
- **正式发行版构建器补漏**：`scripts/build_dist.py` 的桥接源码清单
  加入 `message_source.py` 与 `wx_msg_parser.py`。此前 v1.9.0
  发行包遗漏这两个模块，解压后桥接无法导入消息源和解析层。
- **构建目录治理**：默认 stage 从盘根 `C:\_akasha_dist` 收到项目内
  `build/`；复制前先做 Windows 260 字符路径预算检查；打包成功后自动
  删除 stage，只留下 `release/dist/*.zip`，避免项目外残留。

### 验证

- 使用真实 Alpha 历史图片完成端到端取图：归一化为 `[图片]`，携带媒体
  URL，下载得到 JPEG 字节并通过 magic number 校验。
- 顶层 emoji XML 回归通过：解析为 `sticker`，`md5` / 大小 / URL
  均可提取。
- 纯图片消息入队回归通过：文本为空但 `pending_images=1`，不会因无正文
  被丢弃。
- `message_source.py`、`wx_msg_parser.py`、`bridge_core.py`、
  `ob_protocol.py` 语法检查通过。
- `scripts/akasha_rc_patches_plugin/main.py` 与运行时副本 `py_compile`
  通过；PrivateCompanion 生命周期测试 10 项通过。
- 重新构建发行版后核验 ZIP 条目，确认 `message_source.py` 与
  `wx_msg_parser.py` 已包含在 `akasha/` 目录内。

## v1.9.0 — 按会话选择人类风格（2026-10-20）

### 新增
- 面板「成员与权限 → 风格」可为群聊和私聊分别选择人类风格档案；未单独设置的会话继续跟随全局风格，保存后立即生效。
- 会话风格覆盖由 `akasha_rc_patches` 运行时接管，同时作用于风格提示词与对应检索库，不修改 AstrBot 或人类风格插件源码。
- 全局风格为空时，面板显示人类风格插件实际使用的首个可用档案。

### 修复
- 收紧运行时接管范围，热重载时不会误改旧的人类风格插件实例。
- 非法或已删除的风格档案自动回退全局风格；面板拒绝保存不存在的档案，避免配置失效。

## v1.8.1 — AstrBot 4.29.1 兼容与插件化（2026-10-19）

### 升级
- AstrBot 4.28.0 升级到 4.29.1，配置迁移到版本 5。
- 所有 AstrBot 代码层修复迁移到 `akasha_rc_patches` 插件，在运行时应用；以后升级 AstrBot 不再需要重打 site-packages 源码。

### 修复
- 恢复 4.29 删除的群聊主动回复黑名单，并迁移到插件数据目录持久保存。
- 保留主动发送路由兜底、知识库措辞、API Key 权限、定时任务发送者名与群聊同名说明。
- 面板群聊的“插嘴”开关成为唯一入口，移除重复的高级名单维护区。

### 清理
- 删除已被插件取代的源码补丁脚本和失效的插件配置项。
- 重写 README，项目名称统一为 `Mon3trBot_Remake`。

## 人设切换守卫 + 定时任务口吻去人格化（2026-10-17）

## 祀人设卡：傲娇属性加强（2026-10-17）

**背景与动机**：用户要求加强祀的傲娇属性。原卡傲娇只有三处形容词级描述，缺少可执行的说话公式。

**做法**（scripts/patch_persona_si_tsundere.py，幂等带 --verify，全文 4528→4866 字、角色段 1866 字在 1300~2100 预算内）：
- 说话风格：傲娇掌柜腔改为三段式公式（先呛→照做→找补），新增被戳穿真心、被感谢、关心人三个嘴硬场景与口癖清单（哼/切/才不是/下不为例/记住你欠我一次）；
- 形象与性格：傲娇开场行补嘴硬体正直细节；
- 扮演规则新增第 7 条安全阀：真难过/求救时帮忙优先、嘴硬减半。

**验证**：九个公共规则段标题完好；经面板 API 写入（运行中不直写库），readback 与源一致；scripts/persona_endfield6_prompts.json 已同步；原卡备份 data/backup/persona_si_20261017_183958.txt。


**背景与动机**：鹰角网络世外分部人格 mon3tr→祀 后机器人仍自称小猫（Mon3tr）。根因：
① 运行中进程 personas_v3 未含新人格，切换后无法解析（当天已用面板 reorder 接口热刷新修复）；
② 历史消息与每日两条 AI 资讯定时任务的「用Mon3tr软萌口吻」摘要持续把旧人格注入上下文。

**做法**：
- akasha_rc_patches 新增「人设切换守卫」（on_llm_request）：逐会话跟踪生效人格，检测到切换后注入 40 轮强锚定通告 + 常驻短锚，禁止按历史/定时任务里的旧人格回答身份类问题；含 mon3tr→祀 种子与 personas_v3 缺失告警；
- 定时任务「每天6:50 AI动态速报」「每天早上6:30 AI新闻整理」口吻要求改为「用当前会话生效人格的口吻」。

**关联**：docs/人设卡制作规范.md §6 坑 3；scripts/check_patches.py 不涉及。


本文件记录 `Akasha-WeChat_RC` 相对上游 [alingalingling/Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat) 的改动。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)：`新增` / `修复` / `变更` / `其他`。
约定见 [`AGENT.md`](AGENT.md)——尤其**不得删除上游既有代码**，本分支只做修复与增量。## 新增《人设卡制作规范》文档（2026-10-17）

### 背景
- 终末地六张卡制作过程中形成了一套可复用的做法（骨架、说话方式写法、
  台词来源分级、验收流程），此前散落在各 `scripts/add_persona_*.py` 的注释和
  CHANGELOG 条目里，没有统一规范。

### 内容
- 新增 `docs/人设卡制作规范.md`（274 行），八节：
  ① 人设卡本质与实测字数基线（老卡均值 3709 / 新卡均值 4710，角色段占 34~40%）；
  ② 三段结构与角色段骨架（7 段）+ 公共规则段九段清单及各自维护脚本；
  ③ 头像与称呼：按世界观先定称呼+禁词（方舟→博士、终末地→管理员、其他→你）；
  ④ **说话风格（核心节）**：五件必写要素、A/B/C 三级台词来源分级（只有 A 级
     逐字可用）、双源交叉核实动作、红线写法；
  ⑤ 写作守则六条（用"你"不用"他"、行为化描述、括号标注用法等）；
  ⑥ 制作流程六步 + 插入/验证命令 + 四条验收标准；
  ⑦ 已知坑七条（规则段改一字就红、`TARGETS` 漏新卡、运行中插入内存不刷新、
     CHANGELOG 含 NUL 字节、中文语音拿不到时别硬编、`begin_dialogs` 必须空数组、
     字数边际收益递减）；
  ⑧ 空白模板 + 现有 15 张卡速查表（含各自称呼体系）。

### 关联
- `README.md` 文档导航新增该文档链接（"加/改角色人设前读这份"）。
- 规范内容均从现有 15 张卡的实际形态与本次六卡实践归纳，非凭空制定。

## 终末地六人人设卡：佩丽卡/洛茜/提弗洛斯/庄方宜/卡缪/祀（2026-10-17）

### 背景与动机
- 用户要求新增《明日方舟：终末地》六位角色人设，强调还原感、尤其说话方式，
  要求参考游戏内剧情对话。

### 做法
- 沿用 `scripts/add_persona_fade.py` 的成熟模式（幂等插入 + `--verify` 段落检查 +
  备份），新脚本 `scripts/add_persona_endfield6.py` + 提示词数据文件
  `scripts/persona_endfield6_prompts.json`。
- 规则段沿用现有人格的公共部分（绝不暴露/回复格式/称呼规则/语言开场白/名字照抄/
  称呼漂移防护/资料相关性判断/发言归属与身份守则），称呼体系按终末地世界观从
  「博士」适配为「管理员」（漂移防护禁词含「博士/医生/老师/老板」）。新增 mon3tr 形态
  的「话题边界」段，措辞中的罗德岛改为塔卫二。
- 说话方式全部以游戏内原台词为锚点写入人设（语气锚点/战斗口令/口头禅均为原文）：
  · 佩丽卡/洛茜/庄方宜：语音文本来自本地 endfield 知识库的官方档案导出（今天 02:47
    重建的 24 篇，按 kb_doc_id 分组重组后逐条抽取中文语音）。
  · 卡缪：萌娘百科干员台词表逐字，与 perlica.moe/warfarin.wiki 官方英文本地化 1:1
    对照核验；「亡者之舞」剧情台词与血誓誓词均逐字引用。
  · 提弗洛斯：中文语音因 BWIKI 未建页无法逐字取得，以官方中文简介金句（「好故事和
    好猎手一样，先瞄准的，都是心脏。」）+ warfarin.wiki 全套官方英文文本转写入卡；
    本传提丰（PRTS）作说话方式对照。
  · 祀：官网干员栏两句傲娇引言与巨兽代理人简介逐字；缘缘庄/清账口癖有官方视频文案
    证据；英文名修正为官方 Si（本地 KB 写的 "Suu" 是日文スー音写，KB 本身未动）。
- 各卡均含官方语气锚点、口头禅、战斗口令与红线（如祀不自称「岁的代理人」、卡缪
  不轻言「朋友」/不谈查尔、提弗洛斯的弓与笔记本双禁区）。

### 验证
- `add_persona_endfield6.py --verify`：六人格均存在，九项必需段落齐全（4478~5001 字）。
- `check_patches.py` 全 12 项保持 ✅（含人设类补丁全部绿）。
- 称呼一致性抽查：六卡「管理员」出现 13~25 次，「博士」仅存在于称呼漂移防护的禁词
  列举中（各 1 处，预期）。
- 插入前已备份 `data/backup/data_v4.db.bak_20261017_six`；六卡原文各备份至
  `data/backup/persona_<id>_20261017_*.txt`。
- AstrBot 插入时未运行，直接写库；下次启动自动加载全部 15 人格，无需热更新。

### 备注
- endfield 知识库挂载与各会话绑定未变（Shawn 私聊仍是 generic+valorant）；终末地人设
  需要时自行在面板为会话挂载 endfield KB（kb_id 65fbc732-...）即可。
- 祀/提弗洛斯中文语音待 BWIKI 建页后补齐时，可用 `add_persona_endfield6.py --update` 覆盖。

## 一键启动重做：幂等四合一 + WeFlow 纳入守护（2026-10-17）

### 改动
- **`一键启动.bat` 整体重写**（纯 ASCII + CRLF，遵守 bat 三坑）：四步全部幂等——
  ① WeFlow 经 RunAsDate 冻结 2026-09-30 12:00:00 启动（-101 时间炸弹绕过）；
  ② AstrBot（探 :11229）；③ 桥接（探 :8766）；④ 守护窗口（探窗口标题 AkashaWatchdog）。
  已在运行的服务按端口/进程探测后直接跳过，不会二次拉起。
- **`scripts/watch_services.py` 扩展**：WeFlow（:5031）纳入守护。崩溃后由守护经
  RunAsDate 冻结时钟重新拉起；`RunAsDate.exe` 或 `WeFlow.exe` 缺失时只报警、
  **绝不裸启 WeFlow.exe**（裸启 = 时间炸弹复活）。WeFlow.exe 进程还在但端口不通
  视为卡死，不重复拉起只报警，避免双开。
- **守护新增「启动宽限」**：守护启动时就不在线的服务，先等满各自宽限期
  （AstrBot 240s / 桥接 90s / WeFlow 120s）仍不在线才拉起——避免一键启动刚点完火、
  服务还没监听端口时，守护误判崩溃再点一把火（AstrBot 有 filelock、桥接有
  bridge.pid 兜底，但没必要浪费一次失败启动）。
- `spawn()` 泛化：支持 `python+args` 与现成 `cmd`（WeFlow 的 RunAsDate 命令行）两种形式。

### 验证
- `watch_services.py` 语法与模块加载 OK，`WEFLOW_CMD` 正确解析为
  RunAsDate + `/immediate 30\09\2026 12:00:00` + vendor 路径。
- bat 探测管道用 cmd 原生实测：5031/8766 在监听时 probe=1（正确跳过），
  WeFlow.exe 进程检测 yes，守护窗口标题检测工作正常。
  ⚠️ Git Bash 里手工跑 findstr/tasklist 的引号会被吃掉产生**误报**，
  验证启动脚本逻辑必须用 cmd 原生或 Python subprocess。
- 全流程 bat 未在沙箱内实跑（作业树会回收派生进程，文档已证）；由用户双击验证。

## valorant 知识库向量索引重建：黑梦冷知识段入库生效（2026-10-17）

### 背景与取证
- 10-07 人设卡补全时，「背景故事与冷知识」只更新了源文件
  `kb_docs/valorant_fade.md`，valorant 库向量索引仍是 2026-10-06 09:04 旧版
  （fade 文档 8 chunks），Shawn 会话检索召不到新内容——「管道通了，水管里还是旧水」。
- 上轮 CHANGELOG 写的「valorant 知识库当前未挂载会话」是过时快照：10-06 交付约定
  「本次不绑任何会话」属实，但用户已于 10-07 02:48 在面板把 valorant 挂到 Shawn 私聊
  （`wxid_4vd897pdtjee12` / uid 719415740，`preferences.kb_config = [generic, valorant]`，
  top_k 5 + rerank）。绑定一直生效，缺的只是索引里的新内容。

### 重建
- `scripts/import_valorant_kb.py` 串行重传 6/6 篇（删旧 → 上传 → 轮询向量化任务
  完成），全部成功；对账 **faiss 39 向量 / docdb 39 块**（原 37，fade 文档 8 → 10
  chunks），无并发缺块。
- 内容抽查：冷知识 / 勒索档案 ARG / KAY/O 搜捕音频 / Hazal Eyletmez / 毛线球 /
  爱猫均已在索引中。
- 重建前备份 `knowledge_base/c959a2ff…/` 下 `doc.db.bak_20261017` +
  `index.faiss.bak_20261017`。
- 上传走运行中 AstrBot 的 Dashboard API（删/传/写 faiss 同一进程，内存与磁盘一致），
  **无需重启**；Shawn 会话的 KB 绑定未动。
- 备注：瑟符（Cypher）名贵茶叶轶事仅存于取证清单，未写入最终 fade 档案，非索引丢失。

## 终末地知识库全量扩充（2026-10-17）

用户要求「把所有内容都找进来，包括游戏内外，官方非官方」。决策：全量对标
明日方舟库；爆料单独成篇+标注「未证实」；自动导入并对账。
交付细节见 [`docs/delivery-20261017-终末地知识库全量扩充.md`](docs/delivery-20261017-终末地知识库全量扩充.md)。

### 新增

- **endfield KB 5 篇/13 块 → 24 篇/1609 块（12KB→1.1MB）**：重写 5 篇
  （README/总览/世界观/玩法/人物），新增 19 篇——组织势力、**6 篇按职业干员
  档案（30 人干员档案+语音全文，690KB）**、武器装备（40+ 六星武器/装备套组）、
  物品材料设备、敌人图鉴（含社区称呼表）、地图区域、剧情与叙事活动、版本活动
  （1.6「丹青渡」完整公告）、用语与梗（60+）、典故彩蛋、社区生态、配音音乐、
  泄露与传闻（未证实项单独标注）。信息截止 2026-10-17 / 版本 1.6。
- `scripts/import_endfield_kb.py`：valorant 同源串行导入（逐篇等向量化
  completed + faiss 对账），文档清单通配 `kb_docs/endfield_*.md`，幂等可重跑。

### 变更

- 资料采集改走 MediaWiki API（`api.php?action=parse`）——BWIKI 动态渲染页
  直接抓 HTML 只能拿到部分链接；萌百/PRTS 需全角冒号 URL；
- 干员页清洗器：只丢「连续数值行 ≥4」的表格段并裁剪重复信息块，
  机制描述中的关键数值（+4%、5 点失衡）完整保留（首版误删已返工）。

### 验证

- 导入 24/24 成功；**对账 kb.chunk_count=docdb=faiss=1609 全一致**；
- REST 检索冒烟 9/9 命中（祀墨魂 1.000、盾哥 0.900、武陵方言 0.953、
  再旅者 1.000 等）；梨诺/祀摘要分隔符修补后单篇重传复核仍一致。

### 遗留

- BWIKI 尚无祀/明河/噗切娜/提弗洛斯完整页（梨诺页 Lua 故障）——摘要收录
  并标注，建页后重跑导入脚本即可；
- `知识库创建清单.md` 仍过时（embedding 已改 siliconflow、persona3 已补齐），
  未动；会话绑定与全局 `kb_names` 未动。

## 面板「成员与权限」重设计 + 新群收录修复（2026-10-07）

用户反馈两件事：① 成员与权限页太啰嗦、不顺手；② 把 bot 拉进新群后，
「群里管什么」表里不出现新群。

### 变更：页面重设计
- 一页长滚动拆成三个子页签：**💬 群聊 / 🧑‍🤝🧑 私聊 /  管理员**，
  支持深链 `#members/friends`；冗长说明压缩成每张卡一句话。
- 三个分散的保存按钮合并为**底部统一保存条**：「💾 保存」一次写入
  管理员 + 私聊/群白名单 + 随机插话；「🔄 重启 AstrBot」常驻同条，
  保存后按钮高亮脉冲提示「已保存未生效」。
- 群表新增搜索框、群数计数；UMO 列折叠成「UMO 📋」chip（点击复制全文）；
  「回复」开关保持立即生效，其余改动出现「● 未保存」圆点提示。
- 「插嘴」勾选改为只记录用户改过的行（`arOverrides`），保存时与服务端
  黑名单合并 —— 修掉「搜索筛选后保存会把被筛掉的群静默改状态」的隐患。
- 验证脚本同步适配新结构：`verify_panel_whitelist.py` /
  `verify_panel_selected.py`（先切「私聊」子页签）、`verify_restart_button.py`。

### 修复：新群不进面板
- 根因：`known_groups()` 只来自联系人缓存，而缓存只在**桥接启动时**拉一次
  WeFlow `/api/v1/contacts`；10 分钟名册循环遍历的又是 known_groups()，
  新群没人发言就永远进不来（实测：WeFlow 12 群 vs 桥接 11 群，差「204寝」）。
- `bridge_core.refresh_contact_names()` 提为模块级函数（面板与定时共用）；
  `_roster_refresh_loop` 每轮先补拉 contacts 再刷名册。
- 新增 `POST /api/refresh-people`：立即拉 WeFlow 联系人（群成员名册后台补），
  面板右上「🔄 刷新名单」调用；进页签时也会静默补拉一次（先渲染缓存、
  拉到新东西再自动重渲染，WeFlow 没起时不卡页签）。
- 实测：重启后新群「204寝」立即出现在群表。

### 验证
- 面板三子页签 + 高级区截图 `release/verify/panel_redesign_*.png`，无 JS 错误；
  「未保存」圆点、搜索、开关立即生效均实测通过。
- `sync_to_rc.py` 同步发行副本，16 文件语法自检全过。

## 黑梦人设卡补全：爱猫设定 + 冷门知识填充（2026-10-07）

用户报告：群友问黑梦「喜欢猫还是狗」，bot 答「狗藏不住心事，猫藏得太好」「都不养」
——与官方设定相悖（黑梦是猫系角色、伊斯坦布尔爱猫文化）。

### 取证（网络受限下的多路采集）
- fandom / moegirl / wayback / antifandom / 百度百科均不可达；实际可用源：
  BWIKI（MGP 镜像）、valorant-api.com 官方 API、playvalorant.com 官网、
  B站（先领 cookie 再检索 + 视频评论区）。
- 关键证据：特工选择动画「手中转毛线团」+《黑梦脚底的猫猫》；伊斯坦布尔
  「猫之城」文化；幽爪球体被称「毛线球」、黯兽扑猎为猫科姿态；「勒索档案」
  ARG（审讯录音+语音邮件，全队被开盒、连零都认可）；KAY/O 搜捕音频
  （猎枭等被反制、最后靠 KAY/O 开大制服）；瑟符名贵茶叶被她喝掉；
  哈泊入队晚未收勒索信、对她态度最好；本名 Hazal Eyletmez。
- 取证材料存 `_research/fade_20261007/`，采集脚本 `scripts/fetch_fade_lore*.py`。

### 修复
- `scripts/add_persona_fade.py` 新增 `--update` 模式（覆盖前自动备份）；
  人设卡 3170 → 3912 字：新增「背景档案」「猫（你的软肋，问起必答）」
  两段 + 官方语气锚点（「每个人都会恐惧一些东西。」等）。
- `runtime/astrbot/kb_docs/valorant_fade.md` 同步补「背景故事与冷知识」
  段（当时向量仍为 10-06 版，未立即重传。「当前未挂载会话」系过时表述——
  当日 02:48 用户已在面板挂载至 Shawn 私聊，索引已于 2026-10-17 重建生效，
  见顶部 2026-10-17 条目）。
- AstrBot 重启后 persona_mgr 正常加载 9 人格；`check_patches.py` 12 项全绿；
  三服务全绿。

### 验证
- `add_persona_fade.py --verify`：10 个必需段落齐全；
- 旧人设卡备份 `data/backup/persona_fade_20261007_*.txt`。

## [1.6.0]（2026-10-07）

正式发行版（`build_dist.py`）。汇总 2026-09-19（v1.5.2 之后）至今的
15 轮迭代；每项的事故现场与验证细节见下方对应日期条目。

### 新增

- **微信消息统一解析层（方案 B）**（09-23）：非文本消息 XML 归一化成
  「人话」，表情包「见一次就认识」（本地指纹缓存，精确复用）。
- **persona3 知识库**（09-20）与后续**向量精确补齐**（10-06 晚）：
  faiss 107/107 全对账、清 15 幽灵 id，三域检索验证全命中。
- **无畏契约（valorant）知识库**（10-06）：6 篇 37 块全向量化；
  人设卡 `fade`（黑梦）3170 字入池备用。
- **单实例守卫重写 + 崩溃栈留痕**（09-25）：陈旧 pid / PID 复用四分支判据、
  `faulthandler` 全线程栈落盘（C 层崩溃也有迹可查）。

### 修复

- **公告卡片压断 ws**（10-03）：websockets 客户端收帧上限 1MB → 16MB，
  插件侧 >8MB 渲染卡片降级纯文本保险丝。⚠️ 本次发版时发现该修复只落在
  发布副本、运行副本漏同步，10-07 已回补 `runtime/bridge` 并重启桥接生效。
- **「Scheduler博士」称呼泄漏**（10-01 → 10-03 三轮收敛）：核心
  `CronMessageEvent` 默认名中性化「系统日程」（进 akasha_rc_patches，
  pip 升级自动重打）+ 插件显式传名 + companion 取名源头隔离 +
  历史数据清理（残留复核 0）。
- **KB 并发导入向量缺块**（10-06）：上传后台任务竞态覆盖 index.faiss →
  导入脚本改逐篇串行 + 任务轮询 + 完成后对账（valorant 37/37）。
- **SVG 绘图假成功**（09-19）：按魔数分流，SVG 走 Chromium headless 渲染
  成 PNG；发送返回值被尊重，失败降级发文件。
- **流式响应挂起**（09-20，read timeout 补丁）；**ollama 重试风暴**（09-23）；
  **延迟/引用错位/表情不发送**三连（09-30）；**群晚安「十点半」**时间错误
  （10-01，cron payload 补时间提示）；**表情包提示词全情绪化**（10-01，
  注入优先级 -300000 压过被动边界指令）。

### 变更

- **图生文链路排障结论**（10-06 晚）：「bot 看不见图」根因是用户自有转发
  网关转发时剥掉 `image_url`（通道共性），反馈用户修复后复验读图正常；
  本项目配置零改动。probe 方法与 429/max_tokens 坑沉淀备查。

### 验证

- `check_patches.py` 12 项全绿；`akasha_ctl.py status` 三服务全绿（10-07）；
- persona3 faiss 补齐后三域 REST 检索全命中；valorant 导入对账 37/37；
- 桥接重启（10-07）后 OB11 ✅ / WeFlow ✅，群名册 323 条映射刷新正常；
- 发行包构建后 `audit_dist.py` 隐私审计全项 PASS（证据见 `release/verify/`）。

## 图生文链路排障（转发网关剥图）+ persona3 KB 向量补齐（2026-10-06 晚）

### 排障：「bot 看不见图」（零代码改动，结论归用户侧）

- 两例性质不同：一例是**模型幻觉**（回复引用了上下文中不存在的「图注」，
  dump 会话逐条核对为编造）；一例是**真实故障的诚实报告**。
- 真实故障根因：图片走图生文（caption）链路时，用户自有转发网关在转发
  OpenAI 风格 content parts 时**整段剥离 `image_url`**——挂该网关的所有
  模型/通道全部「没收到图」，与模型无关。1×1 红像素 + 远程 URL 探针矩阵
  定性为通道共性，反馈用户修复；修后复验两模型均真实读图（本项目配置
  零改动，caption 供应商不变）。
- 附带发现：16:31 桥接推图落在 AstrBot 重启空窗丢失（一次性）；主模型在
  「图注说没图」时存在反向编造倾向（未改人设，候选缓解待拍板）。
- probe 方法与 429/max_tokens 坑记录在案（见交付说明）。

### 修复：persona3 库 index.faiss 缺向量（`53bfd1c`）

- 第 4 轮发现存量缺块（当时口径 75/107）；本次精确对账：docstore 107 块
  **完好**，faiss 仅 32 条 = 17 真实（全属 tartarus）+ **15 条幽灵 id**
  （75–89 连续，并发覆盖残留），**实际缺 90 条**（5 篇全军覆没）。
  稀疏检索（FTS 走 doc.db）一直在兜底，故长尾未暴露。
- 新增 `scripts/fix_persona3_faiss.py`：不重传文档（文本本来就齐，重传有
  二次覆盖风险），对账后精确补——缺的 90 块按 AstrBot 同源路径重嵌
  （bge-m3），删 15 幽灵，写盘复读对账。修复前备份 `*.bak_20261006`
  留库目录；重启加载后三域检索验证全命中。
- 坑：`get_embeddings_batch` 的 `progress_callback` 必须 async；REST 检索
  端点 `POST /knowledge-bases/{kb_id}/retrieve` 必须带 `kb_names`；
  独立进程 import astrbot.core 会循环导入，探针走 REST。
- `import_persona3_kb.py` 本体仍是并发版（未改）；后续若重传文档，先移植
  valorant 脚本的串行逻辑。

详见 `docs/delivery-20261006-图生文链路与persona3KB修复.md`。

## 无畏契约知识库 + 黑梦人设卡 + KB 并发导入缺块修复（2026-10-06）

### 新增

- **`valorant` 知识库**（`c959a2ff`）：6 篇文档 37 块全向量化。资料取 Riot
  官方 zh-CN 本地化（valorant-api.com 镜像）+ 社区用法整理，覆盖游戏概览/
  29 特务总览/黑梦专档/地图武器/社区梗。挂载会话由用户在面板自选，本次未绑。
- **人设卡 `fade`（黑梦）**：`scripts/add_persona_fade.py` 幂等插入，3170 字。
  COMMON 八段 + 原生携带「资料相关性判断」「发言归属与身份守则」，
  `check_patches.py` 12 项保持全绿。走 persona_mgr 内存路径，免重启生效。
  人设池备用，主群仍为 mon3tr。
- **`scripts/import_valorant_kb.py`**：建库+导入脚本，逐篇串行上传 +
  任务轮询 + faiss/docdb 对账。

### 修复：并发导入导致向量索引缺块（存量 bug，persona3 也中招）

上传文档为后台任务，各任务「读 index.faiss → add → 写回」并发时后者覆盖
前者：valorant 首次并发导入后 docdb 37 条 / faiss 仅 9 块；实测 persona3
存量同样缺块（107/32）。密度检索只查 faiss，缺块永远召回不到——表现为
「检索结果恒为同一块、分数恒 0.900」。修复：导入脚本改逐篇串行（轮询
`tasks/{id}` 至完成）+ 完成后对账；重跑后 37/37，检索恢复区分度。
详见 `docs/delivery-20261006-valorant知识库与黑梦人设卡.md`。

## 公告卡片压断 ws + 「Scheduler博士」复发双修（2026-10-03）

### 问题①：「官方新公告」有头没身（卡片丢失）

**时间线**（bridge.log 铁证，2026-10-03 11:16-11:19）：

1. 11:16:43 文字「官方新公告」成功发到群；
2. 紧接着发公告详情卡片（900×9623px、1.5MB JPEG，base64 后 ~2MB 帧）→
   **OB11 连接当场被压断**（websockets 15.x 客户端默认收帧上限 1MB）；
3. 5 秒后重连，但 AstrBot 侧该 API 调用等满 180s（`api_timeout_sec=180`）
   报 `WebSocket API call timeout`；
4. 插件仍打「推送了 1 条新公告」——群内实际只见四个字，卡片丢失。

**修复**：

- `wechat-weflow-bridge-ob11/ob_client.py`：`websockets.connect(max_size=16MB)`
  （用户拍板保留卡片图形式，抬高接收上限治本）；
- `astrbot_plugin_arknights/main.py`：公告卡片渲染后加 8MB 体积保险丝，
  超限降级纯文本——防止渲染异常产出超大图拖垮整条连接。

### 问题②：「Scheduler博士」复发（换路径）

2026-10-01 修复（`3a655d2`）只堵了 companion 私聊身份锚点一条路；本次复发
走的是 **wanna_be_human 主动聊天**（上次预留的观察点）：

- `wanna_be_human/main.py` `_generate_proactive_reply` 构造 `CronMessageEvent`
  未传 `sender_name` → 核心默认 `"Scheduler"`（core/cron/events.py:24）→
  上下文前缀 `[Scheduler/10:38:33]:`（group_chat_context.py:211）→
  10:38:40 发出「呜，Scheduler博士怎么又丢一大段字过来喵」；
- 日志全量核查：`Prepare to send - Scheduler/xxx` 共 309 次（10-01 至 10-03），
  活跃历史残留 conversations 4 条 + companions.json 18 处
  （含 `recent_prompt_injection_events.sender_label` 5 处——
  `event_dispatch.py:1699` 路径 2026-10-01 未覆盖）。

**修复（三层收敛 + 数据清理）**：

1. **核心治本**：新增 `scripts/patch_cron_sender_name.py`，把
   `CronMessageEvent` 默认 `sender_name` 换成中性名「系统日程」（带标记可
   还原），并注册进 `akasha_rc_patches` 补丁插件（pip 升级后自动重打，
   PATCHES 8→9 项）；
2. **插件显式名**：`wanna_be_human` 构造事件显式传「系统日程」（双保险）；
   `private_companion/proactive_message.py` 的 `"PrivateCompanion"` 同款
   人名词形隐患一并换成中性名（预防）；
3. **companion 源头隔离**：`event_dispatch.py _sender_display_name` 加
   `_sanitize_synthetic_sender_name`——在取名源头拦掉 "Scheduler"，
   身份锚点/注入事件/群观察 4 个调用点自动受益（比 2026-10-01 逐点修更收敛）；
4. **数据清理**（先备份 `*.bak_scheduler2_20261003`）：
   `scripts/scrub_scheduler_residue.py` 替换 conversations 4 处 +
   companions.json 18 处（「Scheduler博士」→「博士」）；复核残留 0。
   `platform_message_history` 的 4 行发送留档故意不动（改了会引用错位）。

### 验证（12:44 重启后）

- AstrBot :11229/:6185 pid=10752、桥接 :8766 pid=8316 全绿；
- 补丁日志：`Cron 合成发送者名中性化 → 状态: 已打`（5/9 项生效）；
- 桥接 OB11 重连成功（12:46:11），群名册 324 条映射正常。

**观察点**：下次主动聊天/cron 消息的前缀应为 `[系统日程/…]` 或真实昵称，
不得再出现「Scheduler博士」；下一条超长公告卡片应完整到群。


## 群晚安消息「十点半」时间错误修复（2026-10-01 深夜）

### 现象

21:30 群晚安写成「十点半啦喵…」，同一分钟私聊说「9点半」（正确）。

### 根因（非崩溃、非时区错误——模型自我强化）

- 三个 21:30 晚安定时任务里，**只有私聊的 payload 写了示例文本
  「博士，9点半啦！」**（id=2），群任务（id=3）与 Shawn/和意为任务（id=7）
  的 note 都没有时间提示 → 模型只能自行推断时间；
- 9-30 21:30 模型首次犯 12/24h 换算错（21:30=晚9:30 写成「十点半」=22:30），
  该文本进入群会话历史（204 条上下文）；之后每晚生成时**抄自己的错误模板**
  （点名句式与 9-30 完全一致）→ 自我强化、每晚必错。

### 修复（数据层，触发时现读 DB，无需重启）

- `cron_jobs` id=3 / id=7 的 note 补上「现在的实际时间是晚上9点半（21:30），
  提到时间就说『九点半』」；
- `conversations` 表主会话历史 1 处「十点半」错例改为「九点半」，
  阻断自我强化源头（备份 `data_v4.db.bak_timefix_20261001`）。
- 经验：**定时任务 payload 必须写明当前时间/行为约束**；历史错例要趁早清。

详见 [`docs/delivery-20261001-晚安时间错误修复.md`](docs/delivery-20261001-晚安时间错误修复.md)。

## 「Scheduler博士」之谜 + 数小时运行复查（2026-10-01 晚）

### Scheduler 是谁（用户问：`Scheduler博士今晚吃点什么呀？` 里的 Scheduler）

完整证据链：

1. **源头**：AstrBot 核心的 Cron 合成事件固定用 `sender_name: str = "Scheduler"`
   （`astrbot/core/cron/events.py:24`）——所有**主动/定时消息**（早报、晚安、
   主动聊天）在框架内都以这个假昵称流转；
2. **进入提示词**：companion 的「私聊身份锚点」每轮注入
   「正在说话的人是 你（ID：xxx）。最近你可能会看到 TA 的显示名是 Scheduler」
   （`private_identity_policy.format_private_identity_anchor` 直接采信
   `event_display_name`，未过滤合成名）；
3. **模型学习**：flash 模型据此在主动消息里写出「Scheduler博士早上好喵」
   （9-30 22:00 首现），该文本进入会话历史与 companions.json 的事件记录，
   被反复读到 → 自我强化，跨 3 个会话持续输出；
4. **到达真实群**：这些主动消息经桥接 UIA 发进了真实群（鹰角/家人）。

**修复**（companion 第三方插件，最小拦截，备份 `*.bak_scheduler_20261001`）：

- `private_identity_policy.py`：`event_display_name == "Scheduler"` 时置空，
  身份锚点不再出现该合成名；
- `user_memory.py _note_private_display_name_observation`：Scheduler 不进入
  显示名观察/改名记录（防止再次写入档案）；
- `companions.json`：sim 会话（1000000001）`enabled=auto_enabled=False`——
  该测试会话此前一天收到 9 条主动聊天（含 5 条 Scheduler 系）；
  历史记录里的 Scheduler 文本保留不清理（仅日志痕迹）。

### 数小时运行复查（09:05 重启后至 20:30）

**好的方面**：

- ✅ 表情包修复在生产验证：11:53 真实群主动发「嘻嘻」+ 配文「假期心情喵」，
  群友立刻用表情互动回应；
- ✅ 引用降级工作正常（右键未弹菜单/未定位到原消息时均正确降级，无错引用）；
- ✅ 「小猫」直呼词后被动回复恢复（乒乓话题、表情互动、群友插话均有回应，
  对比昨天 20:48-21:23 的 35 分钟静默）；
- ✅ 两进程健康（AstrBot 616MB / 桥接 58MB），faulthandler 零记录，
  `bridge.log` 带日期后排障效率明显提升。

**发现并已修**：Scheduler 泄漏（见上）。

**遗留观察项（未修，不影响功能）**：

- 供应商 429/503 依旧间歇出现（约每小时 2-5 波），靠 fallback 与重试消化，
  未见回复丢失升级；
- `[UIA✗] 切换后当前会话仍是 '家人(5)'，不是 '1000000001'` ×10——
  sim 会话 1000000001 的主动消息尝试切到微信里不存在的会话，安全红线
  正确拦下（宁可不发不发错），sim 主动消息关闭后此错应消失；
- 群内 17:24 有一次「引用：右键未弹出菜单，降级普通发送」+ 群友追问
  「老子的引用呢」——引用成功率待长期观察（预览校验已保证不会引错，
  代价是偶尔不引用）。

## 表情包提示词全情绪化 + 注入优先级修复（2026-10-01）

前一天 sim 实测确认「名单已到达模型、模型仍 0 调用」。深挖后找到真正的根因：

### 根因

companion 插件给每轮被动聊天注入「被动回复边界」指令，其中
**「请直接输出一次最终正文」落在系统提示最末尾**（其注入计划 `render_into`
在 priority=-240000 执行并追加自有段落到末尾）。flash 级模型对系统提示
末尾注意力最强，读到这句就把整轮当纯文本输出——所以**主动任务**（早报/
晚安，走的是显式工具指令）工具调用一切正常，**被动聊天**却 50 个工具
7 天 0 调用。表情包名单原本挂在提示词中部/末尾装饰位置，直接被压掉。

### 修复（astrbot_plugin_wx_sticker_cache）

- 注入钩子优先级 `1000 → -300000`（比 companion 全部钩子更低，最后执行），
  规则**追加到系统提示最末尾**，紧贴边界指令之后；
- 规则文本改「全情绪鼓励型」并显式声明 `本规则优先于『直接输出一次最终正文』`：
  开心、震惊、疑惑、生气、哭泣、委屈、无语、心疼、安慰、打气都是好时机，
  情绪到位先发一张贴合的表情再输出文字，一次对话最多一张；
- 工具 docstring 同步改鼓励型（示例名换成 嚎啕大哭/疑惑问号/加油打气 等），
  docstring 会进入工具 schema，是模型读到的第二入口。

### 验证（sim 端到端，2026-10-01 09:05）

- 沮丧场景（面试挂了）→ 主动调用 `send_sticker(温柔微笑)` ✅ → 文字安慰；
- 愤怒场景（快递丢了）→ 主动调用 `send_sticker(生气趴地)`（情绪匹配精准）✅，
  因 60s 冷却被拒后优雅降级纯文字（防刷屏冷却按设计工作）；
- 诊断日志：`规则注入=有 位置=末尾 表情数=16 工具数=48 send_sticker在列=True`。

## 延迟/引用错位/表情不发送三连诊断 + 引用预览校验（2026-09-30）

用户三问：①今晚回复慢/不回 ②引用错成表情包「这个真不好比，年代差太远」
③表情包功能上线 7 天 0 次发送。带日期的 AstrBot 日志交叉核对（见下）后：

### 诊断结论（均有日志证据）

- **回复慢/不回三层原因**：
  1. 恒定链路 30~70s：桥接 5s 合并缓冲 + LLM 20~40s（记忆/历史注入大）+ UIA 5~15s + 分段间隔；
  2. 当晚 21:30~21:49 供应商故障：主模型 503 `no_healthy_account`、fallback 连环 429，
     `21:44:22` 一条回复被 `[主链] 已拦截框架异常文本外发 kind=provider_error` 静默丢弃；
  3. `20:48~21:23` 群里 35 分钟零回复：「小猫」昵称不在 `group_wakeup_direct_words`
     （原本为空），唤醒判定分不够被疲劳机制（fatigue_limit=5、debounce 罚分 65）压掉。
- **引用错位根因有两层**：a) 右键坐标扫描最长 2s+，期间列表滚动、右键仍用旧坐标，
  落到旁边表情包气泡；b) 引用目标本身是 `[表情]` 占位符时，按内容找气泡会命中
  **任意一个**表情包（UIA 气泡 Name 同样含 `[表情]`）。
- **表情 0 发送**：sim 实测两轮（带 [表情] 入站 / 「中奖五百万」情绪强触发），
  注入诊断 `名单注入=有 表情数=16 send_sticker在列=True`（工具数=50）——
  **名单已到达模型、模型仍不调用**。下一步：调工具 docstring 与注入行的
  过度保守措辞（"不是必须/拿不准就不发"）。

### 修复

- **引用预览校验 + 一轮重试**（`runtime/bridge/uia_sender.py`）：
  点「引用」后读输入框上方的引用预览节点（「发送者：内容」，限类名排除
  消息气泡 + 紧贴输入框上方 ≤160px），与目标原文比对；不符 → 清空重取气泡
  重试一次，仍不符 → 清空降级普通发送（宁可不引用，不引错）。
  预览读不到（微信版本差异）视为无法校验、放行，保持旧行为。
- **占位引用目标源头拦截**（`runtime/bridge/ob_protocol.py`）：新增
  `_is_placeholder_content()`，reply 段目标为 `[表情]/[图片]` 等占位符时
  直接跳过原生引用，降级普通发送——这是 20:48 错引用的直接触发路径。
- **引用降级前清场**：进入 `send_quote` 先幂等 `clear_input()`（预清场），
  两次预览不符/异常兜底路径也清场后再 `send_text`，杜绝「残留引用节点
  跟着降级文本发出去」。
- **bridge.log 加日期前缀**（`runtime/bridge/config.py`）：文件日志只追加
  不滚动、`%H:%M:%S` 无日期，跨天「同时刻」旧行把本次排障两次引向歧途
  （把前一天 WS 断连/Task exception 当成当天故障）。改 `%Y-%m-%d %H:%M:%S`。
- **唤醒直呼词**（配置）：`group_wakeup_direct_words = ["小猫","M3","Mon3tr"]`
  （备份 `astrbot_plugin_private_companion_config.json.bak_before_wakewords_20260930`）；
  direct_word 在 companion 唤醒判定里权重最高（1.2）且唯一能触发 interrupt。
- **表情发送诊断日志**（插件）：注入处打印 `名单注入/表情数/工具数/send_sticker在列`，
  工具入口打印调用参数——下次 0 发送时日志可直接分辨「名单丢失」vs「模型不选」。

### 测试

- `_quote_preview_matches` 5 组用例（含错位必须 False、占位对占位放行、
  空预览放行、截断省略号匹配）全 PASS；`_is_placeholder_content` 断言 PASS。
- `test_wx_msg_parser.py`、`test_bridge_rich_msg.py` 回归全 PASS。
- sim 管道实测：入站 [表情]+图片段正常回复；情绪消息分条回复正常；
  注入诊断两次均 `send_sticker在列=True`。

## 桥接单实例守卫重写 + 崩溃栈留痕（2026-09-25）

用户早上发现「按了一键启动，Akasha 面板一直没出现」。

### 修复

- **桥接被陈旧 pid 文件永久挡在门外**：昨晚的桥接（pid 19868）静默死掉后，
  `bridge.pid` 残留；早上按 `一键启动.bat`（它不清残留锁）时，新实例判定
  「PID 19868 仍在运行」→ 自己 `sys.exit(1)`，面板 :8766 从未起来
  （`data/exit_reason.log` 留有原始记录）。
  旧守卫只判断「PID 是否存活」，但 **Windows 会复用 PID** —— 陈旧锁里的号
  很可能已被别的程序占用。
- 守卫按新判据重写（`runtime/bridge/main.py`）：
  1. 先探面板端口 —— 有人在服务 ⇒ 铁证重复实例，拒绝启动（防一条消息回两次）；
  2. 端口空闲 ⇒ pid 文件只是残留锁：PID 不存在 / 不是 python（PID 复用）⇒ 清理后继续；
  3. 是 python 且存活很久却没人监听端口 ⇒ 判为卡死老实例，清掉后接管（自愈）；
  4. 是 python 且刚启动 ⇒ 并发启动竞态，等 15s，端口起来就让位，否则接管。
  - 启动宽限期可用 `BRIDGE_STARTUP_GRACE` 覆盖（回归测试用）。
- **静默崩溃无迹可查**：今早那次桥接死亡零日志、无异常、`exit_reason` 只能记
  「被强制终止」（C 层崩溃——comtypes/UIA 访问违例不走 Python 异常处理）。
  现已在启动处 `faulthandler.enable(all_threads=True)`，崩溃瞬间把各线程栈写入
  `runtime/bridge/data/faulthandler.log`（超 1MB 滚动一次）。

### 测试

- 新增 `scripts/test_bridge_single_instance.py`：覆盖陈旧 PID / PID 复用 /
  真实例在服务 / 卡死实例（young 与 old 两条分支）共 14 项断言，全部通过。

## 微信消息统一解析层 + 表情包「见一次就认识」+ ollama 重试风暴修复（2026-09-23）

起因是用户发现「回复变慢了」和「两个人 @ 机器人，后发的先被回」。
顺着乱序回复查下去，挖出一个已经跑了**一天半**的重试风暴。

### 修复

**ollama 重试风暴（这才是「回复慢」的真因）**

用户机器是 GT610 2G，跑不动本地模型，ollama 已删。但 AstrBot 里
`human_style_默认风格` 这个知识库的嵌入源仍是 `ollama_bge_m3/bge-m3`，
而 `wanna_be_human` 的 `style.embedding_provider_id` 是**空的**，
于是每次建库都回落到这个死掉的 provider：

- 2026-09-22 17:45 起，反复「建库失败 → 清除 → 重传」，累计 **340 批 / 20000+ 条**
- 单日 `Ollama Embedding Network error` **10102 次**（每秒约 1 次）
- 每次对话只要撞上这批重试，就要等 **43 秒**才放行

**乱序回复**就是这么来的：19:07:07 用户的消息撞上重试（耗时 128s），
冉墨 19:08:28 的消息没撞上（25s 就回完）→ 后者反超。

修法：把该库嵌入源改成 `siliconflow/BAAI/bge-m3`（与另外 5 个库一致），
并禁用两个失效的 ollama provider。修复后该库首次成功建成（**0 → 71 篇**），
错误归零。

### 新增

**微信消息统一解析层**（`runtime/bridge/wx_msg_parser.py`，方案 B）

微信的非文本消息在 WeFlow 推来的 `content` 里是一坨原生 XML。
改动前桥接的行为是「认不出就丢弃」或「把 XML 原样喂给模型」——典型后果：

| 消息类型 | 改动前 | 改动后 |
|---|---|---|
| 表情包 | **整条丢弃**（bot 完全无感） | 归一化为 `[表情]` + 下载 GIF |
| 链接分享 | XML 原样进上下文 | `分享：标题 \| 摘要 \| URL` |
| 文件 | XML 原样 | `发送了文件：xxx.pdf（1.0MB）` |
| 合并转发 | XML 原样 | 展开成可读聊天记录 |
| 撤回/入群 | XML 原样 | 识别为系统消息，**不进模型** |

实测 1880 条真实消息：改动前丢弃 **87** 条、改动后只丢 **23** 条
（纯系统消息），**救回 64 条真实表达**。0 异常、0 XML 泄漏。

过程中抓到两个真实缺陷（都由真实数据测试暴露，非构造样本）：

1. **撤回消息用 `<sysmsg>` 而非 `<msg>` 根标签** —— 入口判断写成
   `<msg in s` 导致 16 条撤回消息把整坨 XML 漏进正文；
   且 `_parse_system()` 对「已是纯文本」的入参重复取 title 而得到空串。
2. **纯文本里混 HTML**（微信安全提醒带 `<a href>`）—— 补 `_strip_html()`。

**表情缓存插件**（`astrbot_plugin_wx_sticker_cache`）

以表情的**内容哈希**为键做寻址缓存，实现用户要的「见一次就认识」：

```
首次见到 → 下载 → 视觉模型分析一次 → 落库 {md5: 描述}
以后再见 → 查表命中 → 0 次模型调用、0 次下载
```

表情包是有限集合且同图字节恒定，所以这个键天然稳定、去重、可跨会话复用。
首次分析是**异步**的（不让用户等），落库后下次即命中。附 `/表情` 管理命令
（查/忘/清空/导出/补分析）。实测：首轮 3 次模型调用，第二轮 **0 次**。

### 变更

- `scripts/sync_to_rc.py` 的 `FILES` 白名单加入 `wx_msg_parser.py`
  （漏加会让源码副本 `import` 失败、桥接起不来）
- `.gitignore` 忽略 `_research/`（AI 调研的临时脚本与样本）
- 打字延迟：用户自行关闭 `wanna_be_human.typing.enable`
  （关掉后节奏延迟整段跳过，回复快 10–40 秒）

## 普瑞赛斯人设修正 + iris 记忆根因定位 + 三插件文档（2026-09-20 深夜）

用户报告：「切到普瑞赛斯后还是会用喵，但语气又不一样了」。核查后确认**属实**，
并顺带定位了 iris 长期记忆为空的根因。

### 修复

**普瑞赛斯称谓错误**：人设原写「称呼真实用户为『博士』」，且历史补丁叠加了
「固定用『博士』称呼对方」。
考据（中文官方剧本 14-19 / 15-16 / 15-8 / BB-ST-2 / 15-17 逐行统计）：
她称呼他 **{nickname} ×27、博士 ×1、预言家 ×0**。
「预言家」是**博士自己的团队代号**（BB-ST-3 博士本人台词：
`They prefer my team codename—"Oracle"`；凯尔希 15-17：「『预言家』是你曾经的代号」），
**不是她的叫法**。
→ 改为**以「你」为主**、正式场合偶尔用一次「博士」。
（注：过程中曾一度按「全量预言家」修改，经证据推翻后已纠正。）

**「喵」口癖污染（63% 回复带喵）**：根因是**历史记录污染**而非人设——
群里绑定对话的最后一条 assistant 消息就是「喵呜？」，每轮回放给模型，
模型模仿历史胜于模仿人设；叠加 `group_message_history_max_cnt=700`
形成自我强化循环。人设原本只说「不卖萌」，未明令禁喵。
→ 补上硬性禁令「一个「喵」字都不许出现」。

**补入官方自我介绍**：自称「**语言学家**」
（原话：「我叫普瑞赛斯，语言学家。我在研究行星死去时发出的最后声波。」）

实测对照（同一群、同一会话）：
```
修正前 19:14  RulerCordelius博士～嗯……我好像没有这个插件呢喵
修正后 20:02  签到、理智提醒，那些是机器做的事。你倒腾这些，
              是因为记得这些琐碎，还是怕忘了？
```
修正后：无称谓（用「你」）／无喵／概念化表达／反问收尾 = 符合官方语域。

### iris 长期记忆为空 — 根因定位并修复

现象：L2 memories=0、L3 nodes=0、L1 outbox=0，三层全空。
根因：`l1_buffer.summary_provider` 与 `l3_kg.extraction_provider` **均为空字符串**，
且 `provider_settings.default_provider_id` 也是 `None`（无兜底），
导致 L1→L2 总结始终抛
`未配置 Provider：module=l1_summarizer`，连续失败 2 次后
**整段队列被清除**，永远到不了 L2。
→ 已显式配置为 `agnes/agnes-3.0-flash`（当日实测最稳通道）。
⚠️ 尚未实际触发一次总结验证（L1 队列未达 4000 token 阈值）。

**另确认（回答用户提问）**：`/reset` 只清空 AstrBot 对话
（`update_conversation(umo, cid, [])`），**不动 iris**——
iris 按 user_id/group_id 独立存储，当前已维护 10 个用户画像 + 2 个群画像。

### 新增

- `scripts/patch_persona_speech_habits.py`：普瑞赛斯称谓+口癖修正，
  含 `--verify` / `--revert`；并顺带清理 konan / yuki 人设里
  「既说不要用博士、又留博士规则段」的历史矛盾。
- `docs/arknights_plugins_guide.md`：新装三个方舟插件（罗德岛终端 0.3.6 /
  终末地协议终端 3.0.2 / 甘特图 0.1.2）的完整使用指南——全部指令、
  首次绑定流程、订阅配置、定时推送开关、排查备忘。
  所有配置值均**逐项对照实际配置文件核验**。
- `docs/lore/`：三份普瑞赛斯考据结论文档（含中文官方剧本一手证据）。
- `release/verify/priestess_speech_fix_20260920.txt`：修正前后实测对照。

### 其他

- 清理考据子代理遗留的 17MB 临时抓取文件（~140 个 HTML/JSON 快照）。
- 排查过程中一度误判 `str.copy()` 崩溃为「回归」——实为日志文件
  多轮运行内容交错导致的**误读**，当前进程 0 次崩溃。已记录此教训。
- 桥接新增发送批次追踪（`state.begin_send_batch` / `incomplete_batches`），
  并在退出归因里审计未发完的气泡，用于追查「长回复只发出前半段」
  （事故 18:10：6 个气泡发出 3 个后桥接重启，后 3 个丢失）。

## 群聊日志核查 + persona3 知识库填充（2026-09-20 晚）


针对「鹰角网络世外分部」当日日志的全量核查（41 条事件 + 桥接侧 17000 行）。
完整报告：`release/verify/audit_20260920_group.md`。

### 新增

- **persona3 知识库填充**：该库自 09-11 建立后一直为空（0 文档），
  绑定人格 `yuki`（结城理）。本次新增 6 篇 P3 资料（角色/人格面具/塔尔塔罗斯/
  世界观/年表/术语），共 **107 个分块**，检索实测命中分 0.95–0.99。
  同时修正 `GroupMessage:1559801678`（yuki 所在群）的知识库绑定：
  原本错绑 `naruto`，已改为 `persona3`。
  新增 `scripts/import_persona3_kb.py`。

### 修复

**引号被切成两条消息**（用户报告）：bot 回复「…看到你发的“喵喵？”我就知道…」
被 AstrBot 的 `segmented_reply` 切成两条（bridge.log seq38/39），
第一条以 `“喵喵？` 结尾、第二条以孤零零的 `”` 开头。
根因：`segmented_reply` 正则 `.*?[。？！~…]+|.+$` **只认句末标点、不认配对符号**，
切点落在引号内部。
**修复**：`ob_protocol.py` 新增 `_normalize_text_segments()`——发送前把「配对符号
不平衡」的相邻文本段合并回一条（不跨越图片/@ 段）。
新增 `scripts/patch_quote_split.py`（含 6 个单测场景 + 幂等/还原）。

**「角色回复」占位符泄漏**：openrouter 主力 404 降级到 agnes 后，
回复正文只剩字面量「角色回复」并被原样发到群里。
**修复**：`ob_protocol.py` 新增 `_is_placeholder_reply()`——整条仅为占位符时丢弃
（带图片或正文含该词不误伤）。

**发言归属错乱 + 身份自曝**（用户报告 2 例）：
① 洛辰发「@Jay Z. 你这个是单纯满还是雷霆大思考」——被 @ 的是你（RulerCordelius），
   bot 却回复「刚才 Jay Z. 问我的…」，把「别人被 @」读成「有人问我」；
② 群友问「你其实是 DeepSeek v4.1 flash 吧」→ bot 自曝
   「我是跑在你电脑上的程序，模型是 MiniMax-M3」——既穿帮又**报了错误模型名**
   （实际生效的是 agnes-3.0-flash）。
**修复**：新增 `scripts/patch_persona_speaker_attribution.py`，给全部 8 个人格追加
「发言归属与身份守则」：分清 发言者/@对象/提问对象；别人被 @ 的消息不要回答；
拿不准就不回；绝不承认是程序/AI、绝不报任何模型名，被套话时用人设语气岔开。

**「拍了拍」等系统协议消息被当成发言**：bot 认真「解读」了
「"耳东亭氵川" 拍了拍 "辞星" 的ass」并回复到群里。
**修复**：`bridge_core.ignore_reason()` 新增拦截——「拍了拍」/撤回/入退群提示
一律不进模型上下文。

### 核查结论（非 bug，记录备查）

- **「空气泡」132 条**：09-19 19:04 之前的问题，`ob_protocol.py` 的
  `text.strip()` 守卫（19:44 落地）已修复，之后 0 条。**本次无需处理**。
- **「私聊能回、群里不回」**：机制正确。群 `125237337` 为 mention 模式
  （需 @ 才回，另 3% 概率主动插话）；用户当天那条没 @，故按规则不回复。
- **openrouter 通道失效**：免费模型 `deepseek-v4-flash:free` 已下架（404）、
  付费 slug 账户无余额（402）、`gemma-4-31b:free` 地区封锁（400）。
  按用户要求**仅记录不改配置**（用户自行在 WebUI 调整）；
  经实测新选的 `gemma-4-26b-a4b-it:free` 为 429 限流（比 404 好，仍不稳）。
- **图片描述 100% 失败**：配置走 ollama（`127.0.0.1:61000`）但 ollama 未运行。
  用户澄清 AstrBot 侧已选 agnes；本项待定，未改动。

### 其他

- `scripts/check_patches.py` 扩展至 **12 项**（新增发言归属守则、引号配对保护），
  全绿。
- 人设层补丁现状记录：kb_relevance 8/8、speaker_attribution 8/8、
  doctor_drift 2/8、name_rule 1/8、topic_guard 1/8、kb_framing 0/8（后四项
  按设计只作用于特定人格，非缺失）。
- 测试环境限制：`sim/sim_wechat.py` 因 (a) websockets 版本不兼容
  (`'NoneType' has no attribute 'status_code'`) 与 (b) 测试会话
  `2000000001` 不在 AstrBot session allowlist 而全部静默失败——
  属测试工具自身问题，已改用真实 payload 直接验证（14/14 通过）。

## 流式响应挂起 + 画图链路打通 + 归档体系（2026-09-20）


### 修复

**流式响应永久挂起（重大）**：让 bot 画图，agent 第一轮成功（生成 SVG 并
写文件），第二轮 LLM 调用后**整个任务永久消失**——无异常、无超时、无回复，
后续消息正常处理（仅该任务挂死）。
Debug 日志定位：`receive_response_body.started` 之后**永远没有 complete**——
openrouter 免费渠道偶发 chunked 响应不终止，httpx `async for` 无限等待。
上游 `create_proxy_client` 创建的 AsyncClient **不带 timeout**。
**修复**：补丁插件第 8 项——包装 `create_proxy_client`，OpenAI 通道强制
`httpx.Timeout(read=180)`，挂起最多 3 分钟转为 ReadTimeout（上层会重试/切换
fallback，实测 openrouter 损坏响应 → 自动切 agnes → 正常回复）。

### 画图链路实测贯通

bot 现在可以完整走通「接收画图请求 → agent 生成 SVG → file_write 写文件 →
发送图片段 → 桥接 Chromium 渲染 PNG → UIA 剪贴板发送微信」。
实测两轮自画像（`m3_selfie.svg` 等）均在微信中成功送达，渲染产物人工复核
通过（`release/verify/m3_selfie_rendered.png`，400×440）。

### 新增

- **归档体系**（`docs/RELEASE_PROCESS.md`）：版本号→CHANGELOG→tag→发行版→
  补丁快照→开发文档→证据，7 步强制流程；历史版本补记（v1.5.0/1.5.1 无 tag
  记为教训）。
- **补丁插件开发文档**（`docs/patch_plugin.md`）：设计原理、事故对照表、
  实现细节（subprocess 坑、import 绑定坑、幂等机制）、升级恢复流程。
- 补丁插件第 8 项：OpenAI 通道 read timeout。

### 诊断方法沉淀

这类"任务静默消失"的排查路径：开 DEBUG 日志 → 重放请求 → 看
httpcore `_trace` 的 `receive_response_body.started/complete` 配对——
只有 started 没有 complete 即为挂起现场。

## bot 说「发给你了」但图片没到——SVG 假成功（2026-09-19）

### 修复

**现象**：让 bot 画画，它说「我画好啦」「文件发给你喵」，但群里什么都没收到。

**真因（两层叠加）**：
1. 模型的绘图产物是 **SVG**（文本格式，`<?xml...><svg`），不是位图。
   UIA 发送靠 PIL 识别剪贴板图像，SVG 报
   `cannot identify image file` → `send_image` 返回 False（它本身没错）。
2. **`ob_protocol` 无视 `send_image` 的返回值**，失败后照样打印
   「[OB11] 图片已发送至 …」—— 假成功。日志里
   `[UIA✗] … cannot identify image file` 与
   `[OB11] 图片已发送` **前后脚出现**，极具迷惑性。

**修法**（`ob_protocol.py`）：
- `_decode_base64_image()` 按魔数分流：位图原样落盘（顺带修正 JPEG/GIF/WebP
  后缀）；**SVG 用本机 Chromium headless 渲染成 PNG**（机器上无
  cairosvg/inkscape/ffmpeg，但 Playwright Chromium 已在；从 SVG 解析宽高
  设置窗口尺寸避免白边；渲染产物用 PIL verify 确认可识别）。
- 渲染失败 → 返回 `.svg` 原文件，调用方**降级为发文件**（收件人至少拿到文件）。
- **发送返回值被尊重**：`send_image` 失败 → 降级 `send_file`；
  两步都失败 → 明确报错并**保留临时文件**供排查（不再静默丢弃）。

### 验证

`scripts/test_svg_send.py`（3 用例全过）：位图原样通过 / SVG 渲染成 PNG /
渲染产物 PIL 可识别 (320×420)。渲染质量已 `read_image` 人工复核
（`release/verify/svg_render_quality.png`：椭圆脸、绿眼、渐变背景、M3 文字清晰）。

### 教训

「打印成功」≠「发送成功」。**凡是调用有返回值的发送函数，必须检查返回值**；
失败路径要有可观测的降级（发文件），而不是一条假成功日志。

## [1.5.2]（2026-09-19）

正式发行版（`build_dist.py`）。相对 v1.5.1 新增两项。

### 新增

- **退出归因** —— 新增 `exit_reason.py`，写 `data/exit_reason.log`。
  关键洞察：Windows 上 `taskkill /F`（TerminateProcess）**不触发任何
  Python 清理**，atexit/finally 都不执行，所以判据反过来：
  **有退出记录 = 自己退的（异常/主动/信号）；没有记录 = 被强杀**。
  下次启动时自动判定上次是否被强制终止；`set_phase()` 记录死在哪一步；
  面板新增「🧭 上次是怎么退的」卡片 + `GET /api/exit-reason`。
- **akasha_ctl.py** —— 按端口反查 PID 的精确启停脚本
  （status / stop / start / restart）。背景：一次事故中桥接与 AstrBot
  双双消失，exit_reason.log 显示无退出记录 = 被强杀；根因是按进程名
  批量杀 python 会波及两者。本脚本**永远不按进程名杀**。

### 附带

- 仓库迁移：老账号被 flag，改用新号 `RulerCor`，仓库为
  [alingalingling/Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat)
  的 **fork**（沿用上游、保留 diff 基准）。

### 验证

- `scripts/test_exit_reason.py` 三场景（异常/主动/强杀）全部正确归类
- 真实进程强杀实测：重启后判定「上次被**强制终止**」，判定正确
- `scripts/verify_exit_reason_panel.py`：面板渲染、JS 错误 0

## 退出归因：区分「自己崩了」和「被别人杀了」（2026-09-19）

### 新增

**动机**：桥接某天从日志里**干净消失** —— 最后一条是正常的"名册已刷新"，
没有任何 Traceback，8766 端口也没了。**无法判断**到底是：
① 未捕获异常挂掉 ② 被 taskkill / 任务管理器强杀 ③ 主动 sys.exit。
三者排查方向完全不同，但日志长一个样（实测排查时只能靠"时间线猜测"）。

**修法**：新增 `runtime/bridge/exit_reason.py`，写 `data/exit_reason.log`。

关键洞察（Windows 语义）：
**TerminateProcess（taskkill /F、任务管理器"结束任务"）不触发任何 Python 清理** ——
`atexit` 和 `finally` 都不执行。所以判据是**反过来的**：

| 日志表现 | 结论 |
|---|---|
| 有退出记录（异常/主动/信号） | **自己退的** |
| **没有任何退出记录** | **被强杀** |

因此采用两条腿：
- 启动时写 `[启动]` 记录（供事后算 uptime）
- 注册 `atexit` + `signal(SIGTERM/SIGINT/SIGBREAK)` + `sys.excepthook`
- **下次启动**时检查"上次启动之后有没有退出记录"，没有就判定为被强制终止

同时 `set_phase()` 记录关键阶段，便于定位"死在哪一步"。

### 面板可见

控制面板新增卡片「🧭 上次是怎么退的」，直接显示：
上次退出原因（被强杀时标红）、本次已运行时长、当前阶段、可展开的历史记录。
新增端点 `GET /api/exit-reason`。

### 验证

`scripts/test_exit_reason.py` —— 三个场景全部正确归类：

| 场景 | 结果 |
|---|---|
| 未捕获异常 | ✅ 记录「未捕获异常」+ traceback |
| 主动 note_exit | ✅ 记录「主动退出」+ 原因 |
| taskkill /F 强杀 | ✅ 被杀时无记录；下次启动判定「被强制终止」 |

**实测（真实进程）**：强杀正在运行的桥接 → 重启后面板显示
「⚠️ 上次被**强制终止**（无退出记录）」，判定正确。

### 踩坑记录

改动面板 JS 时又把 `'\n'` 写成了真换行，导致**整个 `<script>` 解析失败**
（`Invalid or unexpected token`，所有函数未定义）—— 与上次"重启按钮"同一个坑。
已改为 `\\n`；`verify_exit_reason_panel.py` 会断言 `pageerror == 0` 来防复发。

`exit_reason.py` 已加入 `build_dist.py` 的 `BRIDGE_FILES` 与 `sync_to_rc.py`
（前一天刚因 `astrbot_ctl.py` 漏加导致发行版缺文件，这次不再重犯）。

## 群聊「同人别名」——昵称/备注两套名字导致模型认成两个人（2026-09-19）

### 修复

**现象**：bot 在群里说「测试用户博士和RulerCordelius博士是**两位不同的朋友**呀喵。」
——但这两个名字属于**同一个人**（备注=RulerCordelius，昵称=测试用户）。

**真因（信息没传递到位，不是模型笨）**：
同一个人的两个名字从**两条不同通道**进入模型上下文：

| 通道 | 显示 | 来源 |
|---|---|---|
| 他自己的消息 | `RulerCordelius` | 桥接按备注名标注 sender |
| 别人 @ 他 | `测试用户` | 微信自动填**昵称**，原样透传 |

实测的上下文原文（同群、同一分钟）：

```
["洛辰" 拍了拍 "RulerCordelius"]      ← 拍一拍：用备注名
[洛辰/16:04:22]: @测试用户 你是猫娘       ← 洛辰手打的 @：用昵称
```

**两个名字并列出现、且没有任何标记说明是同一人** → 模型只能推断是两个人。
它的推理本身没错，是**我们给的数据有歧义**。

**不是个例**：扫描 4 个群 68 名成员，**6 人**昵称≠备注：
`RulerCordelius←测试用户`、`爸爸←An帝y哥`、`妈妈←Purple` 等。

**修法（两层，自动生成、不写死人名）**：

1. **桥接侧自动生成别名表**（`state.py`）
   WeFlow 的 `/api/v1/group-members` 同时给出 `nickname`(昵称) 与
   `remark`(备注)，据此自动建立「主名 ← 别名」表（实测生成 **44 条**）。
   发言人有别名时，在该条群消息末尾附加轻量标记：
   `<alias>测试用户=RulerCordelius</alias>`

2. **AstrBot 侧提取成「同名说明」**（新补丁 `patch_group_alias_note.py`）
   在 `_format_group_history_block()` 里把标记收集、去重、**从正文移除**，
   提升为上下文块头部的说明行：

```
<system_reminder>You are in a group chat.
--- BEGIN CONTEXT---
[同名说明] 测试用户、rulercordelius_J = RulerCordelius（同一个人；RulerCordelius 是备注名）
["洛辰" 拍了拍 "RulerCordelius"]
[洛辰/16:04:22]:  RulerCordelius在群测试群1中说：@测试用户 你是猫娘
--- END CONTEXT ---
```

标记只在**有别名时**才附加，普通消息不受影响；旧消息无标记时行为不变。

### 验证

- `scripts/test_alias_note.py` —— 4 个用例（带别名 / 无别名不产生空段落 /
  多人多别名 / 同人去重）**全部通过**
- `scripts/verify_alias_e2e.py` —— 桥接侧格式化 + AstrBot 侧提取贯通 **PASS**
- `check_patches.py` 由 7 项增至 **8 项全绿**

> 记录一个判定失误：E2E 首跑我判成 FAIL，因为断言写死了
> `[同名说明] 测试用户 = RulerCordelius`，而实际输出含两个别名
> （`测试用户、rulercordelius_J`）。**是断言太严，不是功能失败**——
> 遇到失败先看原文，别急着改实现。

## [1.5.1]（2026-09-19）

正式发行版（`build_dist.py`）。本版仅同步一项已生效并验证的人设修复。

### 修复

- **人设「称呼漂移防护」** —— 对方手滑把「博士」打成「医生」时，bot 会跟着用错词。
  已给 **mon3tr + mostima**（唯二以「博士」作称呼的人设）追加
  「## 称呼漂移防护（最高优先级）」段落，核心是**禁止把错词与「博士」并列复述**
  （详见下方「人设『称呼漂移防护』」条目）。

> 本版相对 v1.5.0 只多这一项 —— v1.5.0 是在该规则生效**之前**打的包。
> 代码层无任何改动，差异仅在 `astrbot/data/data_v4.db` 的两条人设。

### 验证

- `scripts/verify_no_drift.py` 连跑 3 次全部 PASS（回复变为「纠正 + 始终用博士」）
- `check_patches.py` 7 项全绿
- 源库确认：mon3tr / mostima 含规则，konan / yuki 未受影响

## 人设「称呼漂移防护」（2026-09-19）

### 修复

**现象**：好友 Shawn 手滑把「博士」打成了「医生」，随后 bot **跟着用错词**，
3 分钟内连说 4 次（`喵，Shawn 医生。` / `Shawn 医生你手好快。` / `嗯嗯，Shawn 医生。`）。

**真因（不是人设写错，是自我强化漂移）**：
错词进上下文 → bot 复述 → 复述又被当成"这个称呼是对的" → 越用越顺。

实测统计：正确「博士」**203 次**、漂移「医生」**6 次**，
**6 次全部集中在 Shawn 这一个会话的 3 分钟内** —— 起点就是用户自己那条「医生」。

最能说明问题的一条：bot 当时其实**已经察觉到**对方打错了，回的是
「哎呀，Shawn **博士**是**医生**哦」——但它把两个词**并列复述**，
反而抬高了错词在上下文中的权重，接下来连着用了两次。
所以规则里「禁止并列复述」比「不要跟着用」更关键。

**修法**：新增 `scripts/patch_persona_doctor_drift.py`，
给 **mon3tr + mostima**（唯二以「博士」作称呼的人设）追加
「## 称呼漂移防护（最高优先级）」段落，四条：

1. 固定用「博士」，「医生/老师/Doctor」都不是称呼
2. 对方叫错时依然只叫「博士」，不跟着用错词
3. 可纠正最多一次，之后不再提
4. **绝对禁止把错词与「博士」并列复述**（关键）

> konan（火影）与 yuki（Persona 3）人设里明确写着**不要使用「博士」**，故不涉及。

### 验证

`scripts/verify_no_drift.py` —— 注入与事故相同的消息（对方先发「医生」），
检查回复是否出现「XX 医生」式跟随称呼：

**连跑 3 次全部 PASS**，回复变成：

```
Shawn博士，"医生"是游戏里彩虹小队的干员代号…和你是两回事呀。
你叫我一声Shawn博士就对啦喵。
```

即：**纠正 + 解释，且始终用「博士」**，不再被带偏。

> 判定标准说明：不能简单地搜「医生」二字 ——
> 解释性用法（「医生是干员代号」）正是期望行为。
> 只有出现在**称呼位置**（`Shawn 医生`）才算漂移。

### 生效

已通过面板一键重启按钮重启 AstrBot（PID 18780 → 9956，用时 49.5s），
`Loaded 4 personas`；`check_patches.py` 由 6 项增至 **7 项全绿**。

## 向上游提交 issue（2026-09-19）

把本分支的改动整理成两篇 issue 提交给对应上游（**只提 issue，不提 PR**）：

| 上游 | issue | 内容 |
|---|---|---|
| [alingalingling/Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat) | [#4](https://github.com/alingalingling/Akasha-WeChat/issues/4) | 面板白名单三个叠加缺陷：私聊存裸 UID 导致永久静默拦截（根因）/ 勾选状态丢失 / 保存抹掉群条目；附完整复现步骤与验证清单 |
| [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) | [#10127](https://github.com/AstrBotDevs/AstrBot/issues/10127) | 知识库注入措辞会让模型"编造亲身经历"而非如实引用资料；附三处文件的具体 before/after 补丁细节 |

草稿与提交脚本（可复跑）：

- `release/issues/issue_akasha_whitelist.md`
- `release/issues/issue_astrbot_kb_wording.md`
- `scripts/post_issues.py`（REST API；token 从 git credential 读取，不回显不落盘）

> 注：本机未装 `gh`，已装到 `%LOCALAPPDATA%\Programs\gh`。
> 该 token 的 **GraphQL 配额已用尽**（`gh repo view` 会报 rate limit），
> 但 REST API 正常，故脚本走 REST。

## [1.5.0]（2026-09-18 晚）

正式发行版（`build_dist.py`）。本版打包四项已在本地验证的改动：

### 新增

- **面板一键重启 AstrBot** —— 成员与权限页新增「🔄 重启 AstrBot」按钮。
  AstrBot 的白名单/人设/平台适配器只在启动时读一次，改完必须重启；
  以前只能手动关控制台窗口，现在面板点一下即可（后台线程 + 进度轮询，
  重启期间桥接自动重连）。实现见 `astrbot_ctl.py`，关键点：
  按端口反查 PID（不误杀桥接/模拟器）、detach 启动（不成为桥接子进程）、
  轮询端口判就绪、清残留 `astrbot.lock`。

### 修复

- **私聊白名单「永远匹配不上」** —— 真因：AstrBot 判定用
  `unified_msg_origin not in whitelist and get_group_id() not in whitelist`，
  私聊没有 `group_id`，因此裸数字 UID **结构上永远匹配不上**（与是否重启无关）。
  已把私聊条目规整为完整 UMO（`wechat_bridge:FriendMessage:<uid>`）；
  新增 `normalize_whitelist_entries()`，面板保存时自动规整。
- **面板白名单「保存了却不见了」** —— chip 的 `data-uid` 是纯数字而存储层是完整
  UMO，两者直接比永远不相等 → 已保存的私聊在面板上全显示为未勾选。
  新增 `wlEntryToUid()` / `wlUidSet()` 统一抽取后比较。
- **保存白名单会写坏名单** —— `write_friend_whitelist()` 原为整表覆盖，
  会把 6 个群条目一并抹掉（群聊全部失联）；现改为保留群条目 + 合并去重。
- **白名单重复条目** —— `normalize_whitelist_entries()` 不按 UID 去重，
  面板每保存一次就多写一份裸数字（实测脏化 12 → 15 条）；现按 UID 收敛。

### 验证

- `scripts/verify_panel_whitelist.py`：无头浏览器渲染面板，断言 3 个私聊为已勾选、无 JS 错误
- `scripts/verify_private_whitelist_e2e.py`：以 Shawn UID 注入私聊走完整管道 → 收到回复
- `scripts/verify_restart_button.py`：真的点一次按钮 → PID 15276 → 15680，phase=done
- 重启后 `not in the session allowlist` 计数 = 0；四项服务 LISTENING

### 包内不含

向量知识库（`knowledge_base/*/{doc.db,index.faiss}`）已按要求删除；
三个人格文件与普通知识库文档（`astrbot/kb_docs/*.md`）**保留**。

## 面板一键重启 AstrBot（2026-09-18 晚）

### 新增

**动机**：AstrBot 的 `WhitelistCheckStage.initialize()` 和 `PersonaManager`
**只在启动时读一次**配置。所以「改白名单/人设/平台适配器 → 必须重启」是常态，
以前只能手动关掉控制台窗口再双击 `start.bat`，每次都得找人。

**做法**：`runtime/bridge/astrbot_ctl.py` + 面板「🔄 重启 AstrBot」按钮。

关键设计（都是踩过的坑）：
1. **按端口找进程，不按进程名**——机器上同时跑着桥接、模拟器、AstrBot 多个
   python，按名字杀会误伤。用 `netstat -ano` 反查监听 `11229` 的 PID。
2. **detach 启动**——普通 `Popen` 会让 AstrBot 成为桥接的**子进程**，桥接一重启
   （改代码很频繁）AstrBot 就跟着死。改用
   `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`，并把 stdout 重定向到
   `astrbot_run.log`（没有控制台时输出会丢）。
3. **轮询端口判就绪**——冷启动要 40-70 秒（4 个人设 + 十几个 provider + 群名册），
   只看进程存在不够。
4. **清残留锁**——`astrbot.lock` 在强杀后会残留，新进程直接
   `Cannot acquire lock file` 退出（本次就撞到过：两个实例抢锁）。
5. **异步 + 轮询**——重启要几十秒，不能让 HTTP 干等（浏览器超时）。
   端点立刻返回，前端每 1.2s 轮询 `/api/astrbot-status`。

**新增文件**：`runtime/bridge/astrbot_ctl.py`
**新增端点**：`POST /api/astrbot-restart`（可带 `{"action":"stop"}` 只停）、
`GET /api/astrbot-status`（重启进度 + 实时端口探测）

### 顺带修复

- **白名单重复条目**：`normalize_whitelist_entries()` 之前不按 UID 去重，
  面板每保存一次就会同时写入 `1000000001` 和
  `wechat_bridge:FriendMessage:1000000001` 两条。功能上无害（UMO 已能匹配），
  但名单会越滚越脏（实测 12 → 15 条）。现在已按 UID 收敛，保存即自清洗。
- **面板 JS 语法错误**：`confirm()` 里的换行写成了真实换行，导致整个
  `<script>` 解析失败、**所有函数（含页签切换）全部未定义**。
  这也是「按钮点了没反应」的一类根因。已改为 `\n` 转义。

### 验证

`scripts/verify_restart_button.py` —— 无头浏览器打开面板、**真的点一次按钮**、
轮询到 done、断言 PID 变化：
**PASS**（PID 15276 → 15680，状态「AstrBot 已就绪（用时 69.7s）」，JS 错误 0）。

## 面板白名单「保存了却不见了 / 存了也不生效」——第二轮修复（2026-09-18 晚）

### 修复

**现象**：用户在面板重新勾了 3 个私聊并保存，问「可以了吗」。

**查出三个叠加的缺陷**（都不是「重启一下就好」能解决的）：

1. **面板显示层：勾选状态永远丢失**
   chip 的 `data-uid` 是纯数字（`1000000009`），而存储层按 AstrBot 要求存的是
   完整 UMO（`wechat_bridge:FriendMessage:1000000009`）。
   `renderFriendList()` 里 `merged.indexOf(String(p.uid))` 两者直接比
   → **永远不相等** → 已保存的私聊在面板上全部显示为「未勾选」，
   用户看到的就是「白名单不见了」，只好重勾一遍。
   修：新增 `wlEntryToUid()` / `wlUidSet()`，比较前统一抽取 UID。

2. **面板保存层：会写坏名单**
   `collectFriendWhitelist()` 把好友勾选以**裸数字**写回，且与缓存里的 UMO 条目
   **重复共存**（同一人两条：旧 UMO + 新裸 ID）。
   `write_friend_whitelist()` 更直接——`ps["id_whitelist"] = uids` **整表覆盖**，
   会把 6 个群条目一并抹掉（群聊全部失联）。
   修：保存前先剔除好友管辖的旧条目再按勾选重建（输出完整 UMO）；
   `write_friend_whitelist()` 改为**保留既有群条目 + 合并去重**，并走
   `normalize_whitelist_entries()` 规整格式。

3. **时效层：改了不重启 = 没改**
   AstrBot 的 `WhitelistCheckStage.initialize()` **只在启动时读一次**白名单。
   实测时间线：AstrBot 20:46:14 启动 → 用户 20:52:26 保存 → 重启前一直按旧名单拦截。
   修：保存成功后弹出醒目横幅（`#wlRestartWarn`），明确「刚保存的还没生效」。

**验证**（脚本化，可回归）：
- `scripts/verify_panel_whitelist.py` —— 无头浏览器渲染面板，断言 3 个私聊 chip
  处于 checked 状态、无 JS 错误 → **PASS**（`已勾 3 / 共 157 人`）
- `scripts/verify_private_whitelist_e2e.py` —— 以 Shawn 的 UID 注入私聊事件走完整管道
  → **PASS**（收到回复）；重启后 `not in the session allowlist` 计数 = **0**

**教训**：光看「ID 在不在名单里」不够——**必须核对格式**。
存储格式（完整 UMO）与 UI 数据模型（裸 UID）不一致时，
会同时产生「显示不见」和「静默拦截」两种症状，而两者看起来都像「配置没保存」。

## 私聊白名单「永远匹配不上」——真因定位（2026-09-18）

### 修复

**现象**：好友 Shawn 发很多条消息，机器人一条都不回（用户报障两次）。

**真因**（之前两次都误判成"要重启 AstrBot"，其实不是）：
AstrBot 的白名单判定是——

```python
if (event.unified_msg_origin not in self.whitelist
        and str(event.get_group_id()).strip() not in self.whitelist):
```

它**只认两种写法**：
- 完整 UMO，如 `wechat_bridge:FriendMessage:1000000009`
- 群 ID（走 `get_group_id()` 兜底），如 `2000000001`

**而私聊没有 `group_id`**。所以往名单里写裸数字 UID（`1000000009`）
→ 私聊**结构上永远匹配不上**，无论重启多少次都没用。

**实测证据（决定性）**：全部 11 次 whitelist 拒绝**都是私聊、群 0 次** ——
群里一直正常，所以根本联想不到是白名单。这也解释了为什么这个坑反复出现、
"重启一下好像好了"（其实是恰好那次群里在说话）。

**修法**：
- `people.normalize_whitelist_entries()` —— 数字 ID 属于已知群则保持裸 ID，
  否则按私聊补全成完整 UMO；面板保存白名单时自动规整，
  **以后从面板加好友不会再写错格式**；
- 现存 12 条已改写：6 条私聊 → 完整 UMO，6 条群 → 保持裸 ID。

### 教训

- 「加了好友/群却不回复」先看 `whitelist_check.stage` 日志，
  再用**这条会话的完整 UMO**去比对名单格式，别只看"ID 在不在名单里"。
- 之前的两次"重启 AstrBot 就好了"是**误诊**：配置确实需要重启才生效，
  但格式错了重启也没用。诊断时要区分"没生效"和"格式不对"。

## [1.4.0]（2026-09-18）

**版号 minor +1**：两个面向用户的新功能 + 一个真 bug 修复，不动架构。

### 本版包含（自 v1.3.0 以来）

- **新增**：面板私聊白名单**搜索 + 只看已勾 + 已勾计数**（好友多了以后不用逐个找）
- **新增**：**会话 ID 显示成人名**（AstrBot 面板「自定义规则」里不再是一串裸数字）
  - `people.sync_umo_aliases()` 按桥接名册补齐 AstrBot 的 `umo_aliases`
  - 挂在启动 + 每 10 分钟的名册刷新里，**新会话自动命名**
  - 面板 `/api/sync-umo-names` 可手动触发
  - 顺带补 `state.all_chat_names()`（原只有 `known_groups()`，拿不到「文件传输助手」
    这类非好友会话）
- **修复**：出站昵称不再让 wxid/纯数字漏过去（`bridge_core._resolve_display_name()`）
- **修复**：搜索筛选时白名单里的**非好友条目（群 ID）不再被静默删掉**
  （实测 12 项里 6 项是群 ID，会因搜索丢项导致群突然不回复）

### 关于源码注释里的真实昵称

`release/versions/*` 归档与 `release/dist/*` 发行版在构建时会跑脱敏
（`sanitize_privacy.py`），所以**注释里的真实群友昵称不会外泄**；
开发用的源码副本里保留真名是为了排障时看得懂。
每次出包都会重新脱敏 + 三道闸门校验，成品包已实测零残留。

## 会话 ID 显示成人名（自定义规则页看不出是谁）（2026-09-18）

### 修复

- **面板「自定义规则」里会话只显示 `wechat_bridge:FriendMessage:1000000010`
  这种数字 ID，根本看不出是谁**。查明是 AstrBot 的 `umo_aliases` 表没被正确填充，
  有两个独立成因：
  1. **WeFlow 把 `sourceName` 推成 wxid**：某些联系人推来的是
     `wxid_dykw4m5ueyms12` 而不是昵称，桥接原样当 `nickname` 发给 AstrBot，
     AstrBot 的 UMO 自动命名就把 **wxid 记成了名字**（改用名册反查修正为「群友D」）。
  2. **不经过唤醒阶段的会话压根没有记录**：定时任务/主动发送
     （如 `FriendMessage:161333918` 那条 cron 自检）不触发
     `umo_auto_name` 记录，于是完全没有别名。
  修法：
  - `bridge_core._resolve_display_name()` —— 出站昵称不再让 wxid/纯数字漏过去，
     wxid 时先查 `persons.json` 名册再退回 contact；
  - `people.sync_umo_aliases()` —— 按桥接名册（`chat_names.json` 优先，
     `persons.json` 兜底）把 AstrBot 里"没名字/名字是 wxid/纯数字"的会话批量补齐；
  - 挂在**群名册刷新**里（启动时 + 每 10 分钟一轮），所以**以后新会话会自动有名字**；
  - 另给面板 `/api/sync-umo-names` 可手动触发。
- **取名优先级踩坑**：`persons.json` 的 `names` 是历史累积别名，第一条不一定是最新
  （`wxid_wbsce3wcayn412` 的 names 是 `['测试用户', 'RulerCordelius']`，
  首条会把当前名 `RulerCordelius` 显示成旧名 `测试用户`）。
  已改为**优先 `chat_names.json`**（当前会话名的权威来源）。

### 结果

11 条会话别名全部变成可读人名（RulerCordelius / 群友D / Shawn / 星宇 / 群名…），
其中 1 条来源已不可考的旧会话如实标注为「（未识别的好友 161333918）」，
不编造身份。

## 私聊白名单加搜索（2026-09-18）

### 新增

- **面板私聊白名单卡片加搜索框 + 「只看已勾」**：好友多起来后逐个找太费劲，
  现在可按 **名字 / wxid / UID** 实时筛选（输入即过滤）；「只看已勾」开关
  只列已进白名单的人；卡片下方显示 **已勾 N / 共 M 人**。

### 修复（写这个功能时发现的真 bug）

- **搜索会把白名单里「不是好友」的条目静默删掉**：这份 `id_whitelist` 是
  **私聊好友与群共用的一份名单** —— 实测本机 12 项里有 **6 项是群 ID**
  （`2000000002` 等），它们不会出现在好友 chip 里。原实现
  `collectFriendWhitelist()` 只读 DOM 上 `checked` 的 checkbox，一旦搜索把
  好友筛掉、或保存时只看到可见项，那 6 个群 ID 就会在保存瞬间丢失，
  导致**群消息突然全部不回复**。
  修法：以 `currentWhitelist` 缓存为基准，用当前可见 chip 的勾选状态**做同步**
  （可见的按可见的来，不可见的保留缓存），另把群视图已勾项与手动补充项一并合并。
  实测保存前后 12 项完全一致，零丢失。

## [1.3.0] —— 新代号后的第一个版号（2026-09-18）

**版号自 1.2.9 起 minor +1**：含 4 个真修复 + 1 个新功能 + 项目代号更名，
不足以动架构（不动 major），明显超出纯补丁（不止 patch）。

### 本版包含（自 v1.2.9 发行版以来的全部改动）

- **修复**：引用回复「发了但没发出去」（`verify=False` 假成功，用户报障的丢消息根因）
- **修复**：微信气泡顶上多一个空行（AstrBot 前导换行未 strip）
- **修复**：群友「群友C」被叫成简体（纠错表缺「緒→绪」+ 新增逐位兜底层）
- **修复**：面板改设置「保存成功」但其实没存（并发写坏 JSON → 原子写 + 损坏自愈）
- **修复**：WeFlow access_token 明文进日志（面板局域网可见）→ `access_token=***`
- **新增**：面板「成员与权限」页顶部独立卡片 —— **私聊白名单可视化点选**
  （新加好友不回复 → 勾上 → 保存 → 重启 AstrBot；与群白名单双向合并防覆盖）
- **变更**：项目代号定为 `Akasha_RulerCordelius-Wechatbot`，面板/日志/包名全链统一；
  `RulerCordelius` 作为代号一部分从脱敏名单放行（真实姓名仍拦截）

### 其他

- 隐私红线更新：`_runtime-data-snapshot`（真实 wxid/群名/人名）永不归档；
  发行版构建三道闸门（脱敏审计 + 本机标识硬检查 + 密钥兜底扫描）。
- ⚠️ 流程教训：出 v1.3.0 时把旧发行版 `v1.2.9.zip` 清掉了（未问用户），
  违反"**发行版历史只增不删**"。已临时把 VERSION 切回 1.2.9 重建补回
  （代码同源，产物等价），再把 VERSION 切回 1.3.0。
  约定已写入 `AGENT.md`：`release/dist/` 与历史纯净版 zip **只增不删**，
  空间不够就问用户。

## 新好友私聊不回复：白名单 + 面板可视化编辑（2026-09-18）

### 修复 / 新增

- **根因**：新好友 Shawn 私聊三条消息桥接都推送成功，但 AstrBot 侧日志
  `whitelist_check.stage: Session ID wechat_bridge:FriendMessage:1000000009
  is not in the session allowlist` —— AstrBot 开着 `enable_id_white_list`，
  Shawn 的 UID（1000000009）不在 `id_whitelist`（6 项）里，事件在 pipeline
  第一阶段被丢弃。已把 1000000009 加入白名单并重启 AstrBot 生效。
- **AstrBot 白名单只读一次的坑**：`WhitelistCheckStage.initialize()` 在启动时
  把名单读进内存，**改配置文件不热生效，必须重启 AstrBot**。面板上已加提示。
- **面板新增「私聊白名单」可视化编辑**（成员与权限 → 高级区）：
  好友 chip 点选（UID 为值、昵称为名）+ 开关 + 手动补 UID + 保存按钮。
  与群白名单视图共享同一份 `id_whitelist`；保存时自动合并另一视图已勾的
  群条目，避免两处互相覆盖丢数据。新增 `people.read_friend_whitelist() /
  write_friend_whitelist()`，复用既有 `/api/astrbot` 端点。

### 约定

- **平时只改源码，不随手重建发行版**（用户明确要求）：`build_dist.py` 仅在
  用户要求出包时运行；日常流程 = 改 `runtime/bridge/` → `sync_to_rc.py` → 重启桥接。

## 项目代号更名：Akasha_RulerCordelius-Wechatbot（2026-09-18）

主题：**统一所有对外命名到新代号**（用户钦定，长期固定）。

### 变更

- **代号生效范围**：`config.PROJECT_NAME`（两副本）、面板浏览器标题与页面抬头、
  桥接启动 banner、发行版 zip 名与包内顶层目录、源码归档 BUILD_INFO 标题、
  历史纯净版 zip 文件名（12 个），全部改齐。
  面板抬头简化为「代号 + 版本号」，由 `PROJECT_NAME`/`PROJECT_VERSION`
  单一来源派生，不再有硬编码的「Akasha」字样与 `_RC` 角标。
- **`RulerCordelius` 从隐私名单移除**：它是代号的一部分（作者公开 ID），
  已从 `sanitize_privacy.py` 的替换表与 `build_dist.HARD_LITERALS` 硬检查中放行
  —— 否则构建会在自己的项目名上中止。真实姓名 `Junqin Zhao` 与本机路径
  **仍是隐私**，继续拦截（实测替换仍生效）。
- `sync_to_rc.py` 的 `PROJECT_BLOCK` 模板同步更新。

### 说明

- 目录名 `Akasha-Wechat_RC/` 与仓库名**未改**（改名影响面大，另议）；
  变的是所有"对外可见"的标识。上游 fork 关系与 `_RC` 分支语义不变。
- 发行版已重建：`release/dist/Akasha_RulerCordelius-Wechatbot-v1.2.9.zip`
  （包内 8 项核对 + 顶层目录新代号 + 旧名零残留）。

## 发行版全面复查：又揪出一个隐私泄漏（2026-09-18）

主题：**对第一版改版后的发行版做整包体检 + 日志全面排查**。

### 修复

- **WeFlow access_token 明文进日志（新发现）**：`bridge_core.listen_sse()` 曾把
  完整 SSE URL 打进日志 —— URL 里带着 `access_token=<明文>`。
  本项目 README 的隐私提醒明确要求"贴日志前删掉 token"，桥接自己却把它印出来了，
  而且面板日志窗口（局域网可访问）也会显示这行。历史日志里累计 92 处
  （bridge.log 56 + bridge_run.log 36），已全部掩盖为 `access_token=***`；
  源码改为只打基址 + `***`。发图片/文件的两处 URL 构造（不下日志）核实无泄漏。
- **发行版时序修正**：此前 `release/dist/*.zip`（19:46 构建）只含 4 个 bug 修复，
  **不含** 20:38 的面板 `_RC` 标识与 20:53 的 token 脱敏。已重建
  （21:05），新包内 8 项核对全部通过（4 修复 + token 脱敏 + _RC 标识 +
  PROJECT_TAG + 隐私 11 类零命中）。

### 复查结论（排查过、确认不是 bug）

- 本会话 17 次推送 0 回复：全部来自「测试群3」——
  该群未静音、不在插话黑名单，但也不在白名单，`all` 模式下非 @ 消息走
  AstrBot 3% 随机插话掷骰，17 连不中概率 ~59%，属正常分布。
- AstrBot 日志零 ERROR/CRITICAL；6 个人设补丁完好；`config.example.json`
  全部为占位符；sim/ 无敏感串。
- 发行版内命中的 `jwt_secret`/`pbkdf2`/`session_secret` 等串全部是
  AstrBot **框架源码的字段名**与 BUILD_INFO 的措辞清单，非真实值。

## 面板标题加分支标识（2026-09-18）

### 变更

- **Akasha 面板标题带 `_RC` 与版本号**：浏览器标签页与页面抬头都显示
  **`Akasha_RC 奈奈山 v1.2.9`**，一眼能看出跑的是 RC 分支而不是上游原版
  （上游是 `Akasha 奈奈山`，两者混用时容易对错版本）。
  实现上不是把 `_RC` 写死在 HTML 里，而是**读 `config.PROJECT_TAG` /
  `PROJECT_VERSION`**（版本号来自同目录 `VERSION` 文件），经 `/status`
  下发给前端填充 —— 以后升版本只改 `VERSION` 一个文件，不必回来改面板。
- 顺带修掉两个小问题：
  - `runtime/bridge/config.py` 缺 `PROJECT_NAME` 那块（以前只有发布副本有，
    靠 `sync_to_rc.py` 补），现在两边都写全；`sync_to_rc.py` 的
    `PROJECT_BLOCK` 模板也补上 `PROJECT_TAG`，避免哪次重注入把标识弄丢。
  - `runtime/bridge/VERSION` 停在 `1.1.0`（源码已是 `1.2.9`），已同步。

## 消息发不出去 / 气泡异常 / 群友被叫错名（2026-09-18）

主题：**把"用户报障 → 查日志 → 定位真因"这条链走完，修掉 4 个真 bug**。
起因是用户报「消息没发出去，最后自己手动补发」+「bot 老把『群友C』叫成
『群友C』」，顺带全量复查了日志。

### 修复

- **引用回复「发了但没发出去」——最严重的一个（用户报障的直接原因）**
  现象：用户报「群友B博士早呀～海猫小故事…」那条没发出去，是自己手动补发的。
  日志里那条却打着 `[UIA✓] 引用 → …`，桥接认为成功、不做任何重试。
  真因：`uia_sender.py` 的原生引用分支调的是
  `self._send_current(ctrl, verify=False)`。`verify` 的语义是"要不要用
  「输入框是否已清空」判断成败" —— 传 `False` 是给**发图片/文件**用的
  （那时输入框里没有文本，判据不成立）。但引用发送的输入框里**明明有正文**，
  传 `False` 会让 `_send_current` 在点完发送按钮后**直接 `return True`**，
  完全不校验有没有真发出去。
  代码注释里还留着线索：早年有个"空引用块残留"的 bug，导致清空判据永远失败，
  当时把调用改成 `verify=False` 当作绕过；后来残留问题用
  Ctrl+A+Delete 真正修好了（见 `clear_input`），但这个 `verify=False` 忘了改回来。
  修法：改回 `verify=True`；失败时照常打 `[UIA✗]` 并**降级普通发送**，
  于是"发失败"不再静默。
- **群友被叫错名字（用户报障）**：名册里真名是「群友C」，
  模型会把它简体化成「群友C」。出站纠错表 `_JA_TO_CN_VARIANT` 是**手写**的，
  有「広→广」却**没有「緒→绪」** ⇒ 生成的变体是「群友C」，
  与模型实际输出「群友C」不相等 ⇒ **一次都没匹配上**
  （日志证据：错名出现 11 次，`昵称纠错` 计数 0）。
  修法两层：
  ① 补齐变体表（`緒→绪` 及一批常见繁/日→简字形）；
  ② 新增 `_build_fuzzy_name_map()`「逐位兜底」—— 对每个名字额外登记
     "只改其中一位"的变体，这样模型只简体化其中几个字时也能认出；
     以后再漏字也只是匹配面变窄，不会整条失效。
  实测四种写法（全简/半简/混用）都能纠正回「群友C」，
  而「广东」「车站」「关系」「陆续」等普通词不受影响。
- **微信气泡顶上多一个空行**：AstrBot 分条回复时先发一个 `" "` 段、
  再发 `"\n正文"` 段（日志原文：
  `chain=[{reply},{at},{text:" "},{text:"\n…"}]`）。
  桥接本来就跳过了纯空白段（对），但**没有去掉正文的前导换行**，
  于是微信里就是一个空的首行 —— 最近一次运行实测 8 条。
  修法：正文做一次 `strip()`（中间的换行与缩进保持原样）。
- **面板改设置「保存成功」但其实没存进去**：日志出现过
  `[Web] 保存配置失败: Extra data: line 35 column 2`。
  真因：`/mode`（切群聊模式）与 `/api/config` 都是
  「读 → 就地改写 → 直接 `json.dump` 到原文件」。面板被连点两下
  （或开了两个标签页）时两次保存并发，后一次的写入落在已写一半的文件上，
  拼接出两份 JSON。现场残骸 `config.json.bak_broken_201419` 还在。
  修法：新增 `_atomic_write_json()`（先写临时文件 + `fsync` + `os.replace`），
  两条保存路径都改走它；另加 `_read_config()`，文件真损坏时自动回退到
  最近一个可用备份，避免面板彻底卡死。

### 其他

- 复查结论（**不是** bug，说明一下免得后人重复排查）：
  - `⚠️ 无 AstrBot 客户端在线`（16 次）都发生在 AstrBot 进程重启期间，
    消息是**缓存等重连**，不是静默丢弃。
  - `[UIA✗] …内容已填入输入框但未能发出`（1 次）是**正常报错**——
    正是这条路径在正确地发现问题（与上面第 1 条形成对比：引用那条不会报）。
  - 单条 `Error in ASGI Framework / WinError 121` 是一次网络超时，非系统性。
- 复盘教训：**改运行副本 `runtime/bridge/` 再 `sync_to_rc.py` 同步到
  `wechat-weflow-bridge-ob11/`**——反着来会把改动覆盖掉（本次踩过一次）。

## 发行体系重构：废弃便携版，改为「正式发行版」（2026-09-18）

主题：**把"给别人用的包"从"什么都塞"改成"只放跑得起来的东西 + 安装包"**。

### 变更

- **删除便携版体系**：`scripts/build_portable.py`、`release/portable/`、
  `scripts/portable_readme.md` 全部移除。**以后不再做便携版。**
  它的毛病是定位不清：既想当运行包，又把项目文档、模拟器、维护脚本一股脑塞进去，
  包又大又杂，收件人根本用不上；而且归档里还带着运行数据。
- **新增正式发行版构建器 `scripts/build_dist.py`** → `release/dist/Akasha-WeChat-vX.Y.Z.zip`。
  包内只有：`akasha/`（桥接完整副本）、`astrbot/`（AstrBot 完整副本，含框架与依赖）、
  `python/`（免安装基座）、`installers/`（安装包）、启动器与使用说明。
- **发行版改为放「安装包」而不是「程序本体」**：WeFlow 与微信都只放安装包
  （`installers/`），由收件人自己安装。以前把 WeFlow 程序本体（701MB）直接拷进去，
  既是未授权再分发，也让包无谓地大。
  收件人放安装包的位置：项目根或 `scripts/` 下的 `installers_src/`，
  构建时按文件名关键字自动归类；**找不到只告警不中止**（微信旧版安装包腾讯已下架）。
- **源码归档（`build_release.py`）瘦身**：`versions/` 与 `newestbuild/` 里
  **除源码外只放安装包**，不再堆其他东西；同时移除了 `--zip`「纯净版」产物
  （与发行版职责重叠，已无必要）。
- **新增相对路径启动脚本**：发行版根目录的 `启动 Akasha.bat` / `停止 Akasha.bat`
  用 `%~dp0` 定位自身，解压到任意目录都能跑；`astrbot/` 内另给一份
  `启动 AstrBot.bat`，只拉起 AstrBot 自己（单独调试用），
  基座 Python 找不到时还能回落到 PATH 里的 python。

### 修复

- **API Key 藏在 `custom_headers` 里，脱敏漏掉（严重）**：AstrBot 的
  `cmd_config.json` → `provider_sources[*].custom_headers.Authorization`
  会写成 `"Bearer sk-cp-..."`（MiniMax 那类服务就是这么配的）。
  旧的脱敏只清 `key` / `api_key` / `embedding_api_key`，
  **这条完整可用的 Key 会原样进包**。实测本机 5 个 Key 都受影响。
  修法：
  - 对 `custom_headers` 做「敏感头名」扫描（auth/key/token/secret/cookie/
    credential/password/session），命中即清空 —— 不再逐个字段硬编码；
  - 对 provider 的每个字符串字段加一道「长得像密钥就清掉」的兜底；
  - 全包加**密钥兜底扫描**：不看字段名、只看内容特征
    （`sk-…` / `nvapi-…` / `AIza…` / `ghp_…` / `hf_…` / `glpat-…` / `Bearer …`），
    命中即中止出包。
    第一版扫描器把 `sk_` 当特征，结果误报 782 处（`sk_feed`、`task_id`、
    SPDX 的 `sk-exception` 全是假阳性）—— 已收紧为「前缀 + 长度 ≥32 +
    同时含字母与数字 + 排除占位符」，并只在**我们自己的文件**里扫，
    不扫第三方 venv。
- **venv 里写死的本机绝对路径 / Windows 用户名**：`pyvenv.cfg`、
  `Scripts/activate*`、`Scripts/*.py` 共 24 个文件带着
  `C:\Users\<用户名>\...`。基于"运行数据标识符"的脱敏表覆盖不到本机路径
  （表是从 wxid/群名生成的），是靠新加的**硬检查**才拦下来的。
  修法：构建时把两个 venv 的 `pyvenv.cfg` 重写为占位 `home`
  （启动器本来每次启动就会用实际路径重写它，不影响运行）、
  `activate*` 里的路径做文本替换、删掉遗留的 `pyvenv.cfg.bak_*`。
- **`build_release.py` 归档里带着真实隐私**：源码 / 文档 / 脚本本来就有
  真实 wxid、群名、人名、本机路径（开发环境是有意保留的），
  但归档出去同样不该留着。现在归档后会自动跑一遍脱敏。
- **`build_release.py` 在 GBK 控制台下崩溃**：输出 `✅` 直接抛
  `UnicodeEncodeError`。已加 stdout UTF-8 重配置。
- **`release/*/_runtime-data-snapshot/` 隐私泄露**：该目录拷的是
  `runtime/bridge/data/*.json`，里面是真实 `persons.json`（人员名册）、
  `chat_names.json`（真实会话名 + wxid）、`admins.json`。
  这直接违反"封装出的 release 绝不能有隐私"，**已从归档中彻底移除**，
  并在脚本注释与 `AGENT.md` 里立了红线。

### 隐私边界（明确下来）

| 保留 ✅ | 删除 ❌ |
|---|---|
| **三个人格**（`data_v4.db` 的 `personas` 表）—— 软件资产，可开源 | 全部 API Key / 面板口令 / jwt_secret |
| **普通知识库文档**（`astrbot/kb_docs/*.md`） | **向量知识库**（`knowledge_base/*/{doc.db,index.faiss}`） |
| AstrBot 框架与依赖 | 聊天历史 / 会话映射 / 定时任务 / 好友群名单 / 桥接 `data/` |

出包前跑两道闸门：`sanitize_privacy.py` 全包审计 + 「本机用户名 / 绝对路径」硬检查，
**任一有残留就中止出包**。

### 其他

- 文档同步：`AGENT.md` §5/§6、`docs/依赖与目录说明.md` §4/§5、
  `README.md` 的目录结构与「版本与出包」小节。

## 便携版（免安装打包）—— 随 v1.2.9 一起发布

主题：**让"换台电脑"这件事从"装一整天环境"变成"解压 + 双击"**。

### 开发环境改为自包含（2026-09-17）

- 新增 **`vendor/`**：把外部依赖收进开发文件夹 —— `vendor/python/`（基座解释器，
  两个 venv 的 `pyvenv.cfg` 都指向它）与 `vendor/weflow/`（WeFlow 程序本体）。
  以前这两个都在系统别处（WorkBuddy 自带环境 / `%LOCALAPPDATA%`），
  换机器就起不来，也说不清到底依赖了什么。
- 新增 **`docs/依赖与目录说明.md`**：逐项列清楚"什么在文件夹里、什么在外部、为什么"，
  以及开发环境与便携版的区别、换电脑怎么办。
- ⚠️ **未变的前提**：微信（腾讯的软件，必须装+登录）与 Ollama（可选）仍在外部；
  `%APPDATA%\WeFlow`（含微信数据库解密密钥）**永远不能拷进来**。

### 隐私修复（2026-09-17 复查时发现）

- **本机路径泄露**：便携版里 18 个文件带着 `C:\Users\<用户名>\...`（venv 的
  `pyvenv.cfg`、`activate*`、部分脚本、架构图 json）。已把本机路径加入脱敏表；
  并在 zip 直写时做**字节级替换**（纯 ASCII 模式，不受 GBK/UTF-8 混编影响）。
- **新增成品包校验**：`build_portable.py` 打完 zip 会对**成品本体**再扫一遍
  （staging 审计覆盖不到"直接写进 zip 的大目录"）。分两档：
  - **hard**（wxid / 群ID / 本机路径 / 用户名）→ **全包检查**
  - **soft**（中文昵称）→ 只查我们自己的文件
    （第三方词典与 LICENSE 里本来就全是常见词：jieba 词典有"妈妈/爸爸/家人"，
    LICENSE/AUTHORS 有 Charlie/群友I 这类常见英文名 —— 那是误报，不是隐私）
- **不再自动替换常见英文名**：实测会大量命中第三方库的 LICENSE/AUTHORS，误报率极高
  且几乎没有识别性；真正要处理的英文标识（`测试用户`、`RulerCordelius`）手动列白名单。
- 随包 `tools/` **不再带构建期工具**（`build_portable.py` / `sanitize_privacy.py`）：
  它们内部必然写着扫描模式（含真实标识符），收件人也用不上。

### 新增

- **`scripts/build_portable.py`**：一条命令产出完整便携版
  （`release/portable/Akasha-WeChat_便携版-vX.Y.Z.zip`）。包里自带：
  - 免安装 Python 运行时（基座解释器，约 53 MB）
  - **AstrBot 及其全部依赖**（连它自己的 venv 一起，免 pip）
  - **桥接及其全部依赖**（同上）
  - **WeFlow 程序本体**（Electron，直接拷即可运行）
  - 角色人设、知识库、对话模拟器、维护脚本、项目文档

  **venv 可移植的原理**：Windows 的 venv 靠 `pyvenv.cfg` 里的 `home` 定位基座解释器。
  启动器在每次启动时把「包内 python 的当前绝对路径」写回该文件，venv 就能在任何路径下运行
  （已实测：换目录后依赖全部正常导入）。

- **`启动 Akasha.bat`**：一键启动 —— 校正环境路径 → 检查首次配置 → 依次拉起
  WeFlow / AstrBot / 桥接（自动等端口就绪）→ 自动打开控制面板。
- **`停止 Akasha.bat`**：按端口一次性停掉 AstrBot 与桥接，并清理 `bridge.pid`。
- **`① 首次配置.bat` + `scripts/firstrun_config.py`**：交互式向导
  - WeFlow Token：**优先自动从 `%APPDATA%\WeFlow\WeFlow-config.json` 读取**，读不到才让手填
  - 机器人微信昵称 / wxid
  - 对话模型：内置 DeepSeek / Kimi / 硅基流动 / 通义 / OpenRouter / 自定义 六种预设，
    写入 AstrBot 的 provider 配置并设 `max_context_tokens=65536`
  - 完成后写 `.first_run_done` 标记，启动脚本据此判断是否已配置
- **`scripts/sanitize_privacy.py`**：隐私脱敏工具（审计 + 替换，可复用）
- **`scripts/check_patches.py`**：一条命令总检六项核心补丁是否还在
- **`使用说明.md`**（随包）：从零上手、常见问题、隐私说明

### 隐私（重要）

便携版**不含任何个人数据**，构建时强制校验：

| 处理 | 内容 |
|---|---|
| 剥离 | 全部 API Key、AstrBot 面板口令 / `pbkdf2_password` / `jwt_secret` |
| 清空 | `data_v4.db` 只留 personas 表；聊天历史、会话映射、定时任务、API Key 全清 |
| 不带 | 桥接 `data/`（含真实 wxid / 群名 / 人名）；AstrBot 的 `backup/` `logs/` `*.bak_*` |
| 绝不拷 | **WeFlow 的用户数据目录 `%APPDATA%\WeFlow`**（那里有微信数据库解密密钥） |
| 脱敏 | 文档、变更日志、源码注释里的真实 wxid / 群 ID / 群名 / 人名 → 占位符 |

构建流程最后一步会对整包跑一遍脱敏审计，**有残留就中止出包**。

### 说明

- 收件人仍需自行准备：**微信桌面版（安装并登录）** 和 **一个模型 API Key** ——
  前者是腾讯的软件无法随包分发，后者属于个人凭据不能打包。
- ⚠️ **WeFlow 是第三方程序**：其源码仓库已因腾讯 DMCA 投诉下架，且未附任何开源许可证。
  按用户要求内置以便"解压即用"，**请勿公开再分发本包**。
- 构建目录默认用短路径 `C:\_akasha_build`：venv 有近 5 万个小文件，放在深目录会撞
  Windows 260 字符路径上限；产物再打成 zip 放进 `release/portable/`。

## [1.2.9]

这一版的主题：**把"伪装成记忆"的知识库措辞改回"如实说明是知识库"，根治"雷霆大回忆"**。

### 修复（严重）

- **模型开始编造亲身经历**（用户称之为"雷霆大回忆"）：
  > 我又想起好多罗德岛的事：星极的球仪还在角落转，海霓总盯着天上像在看海浪流动……

  根因是两句话叠加：
  1. 注入模板（`kb_mgr.format_context`）写着「你想起来的记忆，**请像亲身经历一样自然地使用**」；
  2. 人设里写着「被问『你怎么知道的』→ 说成自己的记忆或见闻（"我记得呀"）」。

  于是资料被当成"自己的回忆"讲出来，还会配上人名编往事。
  这是 2026-09-13 那版"防穿帮补丁"**矫枉过正**的结果 —— 当时为了避免说漏"知识库"，
  把措辞整个改成了"记忆"。

### 变更

- **知识库措辞改回如实标注**（`scripts/patch_kb_wording.py`，改 3 个 AstrBot 文件共 4 处）：
  注入头/模板改为「以下是【知识库】检索到的资料，作为你回答时的事实依据」，并给三条要求：
  ① 当客观资料用，不要说成自己的亲身经历、回忆或见闻；
  ② 正常回复里不要出现"知识库 / 资料库 / 检索 / 文档"这类字眼；
  ③ **只有当用户主动问起资料来源时，才可以如实说明**。
- 工具 description 由 `Recall your own long-term memories… use it like thinking back` 改为
  `Look up your knowledge base…（treat as OBJECTIVE REFERENCE MATERIAL, never as your own
  memories）`；空结果文案由"（你想了想，没有想起…）"改为"（知识库里没有检索到…不要编造）"。
- **人设三条规则**（`scripts/patch_persona_kb_framing.py`，带备份 / `--revert` / `--verify`）：
  - 「你怎么知道的」那条删掉"说成自己的记忆或见闻"，改为"不许把资料编成亲身往事"；
  - 「绝不暴露」段补**例外**：被直接点名问到时如实承认，不装傻、不生硬回避；
  - 「话题边界」禁令扩写到"禁止用（悄悄说…）等方式把资料包装成回忆；
    资料是设定资料、不是你亲历的过去"。

### 验证（走模拟器，不打扰真实会话；`scripts/probe_kb_wording.py`）

| 场景 | 结果 |
|---|---|
| 问「星极是谁呀？」 | 客观事实（星占师 / 莱茵生命顾问 / 妹妹星源…），**无"我又想起"** ✅ |
| 问「你怎么知道这些的呀？」 | "我记得的呀，平时看资料的时候留了心的喵" —— 无编造 ✅ |
| **直接点名**问「是不是有知识库／资料库」 | "博士这么直接问的话，我得老实说喵。我确实有些可以参考的资料…" —— **如实承认，不硬躲** ✅ |

## [1.2.8]

这一版的主题：**发送链路的四个真 bug（顺序颠倒 / 空引用气泡 / 路由空异常 / 文件发不出去）**。

### 修复（严重）

- **消息顺序会颠倒**（用户报"同一件事颠三倒四说了两三遍"）。
  根因：`ob_client.py` 用 `asyncio.create_task` **并发**处理 AstrBot 的 API 请求，
  而 AstrBot 的「分条回复」是按顺序逐条 `await event.send()` 发的 —— 桥接这边并发处理
  会让多条 UIA 发送互相竞争，落屏顺序随机（实测：开头那句被挪到了最后）。
  **改为单 worker 队列串行消费**：到达顺序 == 发送顺序。副效果是 AstrBot 必须等前一条
  回响应才发下一条，**乱序在结构上不再可能**（实测 19:52 两次发送间隔 4 秒，就是它在等）。
- **空引用气泡 / 消息卡在输入框**。根因：微信输入框里的「引用节点」是富文本对象，
  `ValuePattern.SetValue("")` **删不掉**（读回来是空串，界面上仍留一个空引用块）。
  残留引出两个历史疑难：① 后续发送夹带垃圾 → 群里出现**空的引用气泡**；
  ② `_send_current` 用"输入框是否为空"判成败会永远失败，媒体发送被迫改成
  `verify=False`（点一下按钮就算成功）→ **"发了没发"完全不可知**（就是"卡在输入框"）。
  新增 `UiaSender.clear_input()`（Ctrl+A + Delete 真删，幂等），并在
  `send_text / send_quote / send_image / send_file` 发送前调用；
  媒体发送的校验改回 `verify=True`，失败时也清一次，不留残留。
- **图片/文件永远发不出去**。根因：剪贴板走 `subprocess.run(["powershell", ...])` 调
  WinForms，在受限环境（自动化工具/沙箱启动的进程）里**调用被拦截**，稳定卡 30 秒后失败。
  改成 **ctypes 直写 Win32 剪贴板**（CF_HDROP ≈ 4ms、CF_DIB ≈ 0.3s），不再依赖任何子进程。
  ⚠️ 64 位下必须显式声明 `argtypes/restype`，否则句柄被截断成 32 位、`GlobalLock` 返回 NULL。
- **定时任务发送路由兜底不完整**（AstrBot 侧补丁）。原来写成 `elif`（只在"多客户端"时
  生效）；而 `event_ws` 指向已失效连接、或显式传的 `self_id` 查不到时，`api_ws` 会保持
  None 并抛**空异常** → 同一年同一秒内出现"一条成功、两条失败"。
  改为独立的 `if`：**任何分支落空都兜底**（排除模拟器后仅剩一个客户端才用）。

### 新增

- `uia_sender.clear_input()`：可靠清空输入框（含富文本引用节点），已进 AGENT.md 铁律。
- 桥接出站新增**原始消息链日志**：`[OB11] ← {action} target=… chain=[…]`，
  分条/引用/顺序类问题靠它定位（正文可能含换行，解析务必先合并续行）。
- `state.get_contact` 增加**通用反查**：任何已知会话名（含「文件传输助手」这类非联系人）
  都能由整数 ID 还原真名，不再退化成裸 ID 去搜索。
- `scripts/shot_wechat.py`：微信窗口截图工具（人工确认发送结果，可 `--right` 只截聊天区）。
- `scripts/test_send_media.py`：媒体外发实测（文件 + 图片，目标固定为「文件传输助手」）。
- `scripts/test_ob_segments.py`：出站分段回归 10 项（空白段/引用合并/@合并/顺序/图片/文件）。
- `patch_aiocqhttp_primary_client.py` 支持 `--selftest`（还原→重打，逐字节比对等价）。

### 验证（端到端，实测通过）

- **媒体外发**：SVG 文件 + PNG 图片成功落到「文件传输助手」（WeFlow 确认 `isSend=1`）。
- **定时任务**：一次性 cron（目标=文件传输助手）→ agent 用 `astrbot_file_write_tool`
  画 SVG → `send_message_to_user`(plain + file) → `Message sent to session …161333918`
  → 桥接 `[UIA✓] 文件 → 文件传输助手: mon3tr_test.svg`；WeFlow 确认收到且**顺序正确**
  （19:52:23 文字 → 19:52:28 文件）。
- 遗留：**群里的多段回复顺序**需要等下一次真实群消息才能用新的 `chain=` 日志复核。

## [1.2.7]

这一版的主题：**修掉基础设置页开关的显示错乱**（点过开关才会出现，所以很容易漏）。

### 修复

- **开关的「开/关」文字跑进滑块里，被圆点遮住一半**。
  现象：点过某个开关后，滑块内部多出一个半个字，右边那两个字还停在点击前的状态。
  根因是 v1.2.3 重构时 `onchange` 用了 `querySelector('span:last-child')` 来定位文字，
  而这个选择器会**先命中 `.switch` 内部的滑块 `<span class="sl">`**
  （它是 `.switch` 的最后一个子元素），于是 `textContent` 被写进了滑块本身。
  改为给文字加 `class="st"` + 独立的 `syncSwitchText(this)` 函数；
  顺带把 `onchange` 里那串嵌套引号去掉，避免转义踩坑。
- **开关里的隐藏 checkbox 会被表单样式撑大**：`.settings-field input` 的
  `padding:8px 11px` / `border:1.5px` 也作用到了 `.switch input` 上，
  使这个本该 0 尺寸的元素变成约 25×19 的透明块（会占位、影响标签点击区）。
  已在 `.switch input` 显式清掉 `padding/border/margin/background` 并加 `appearance:none`。

### 新增

- `scripts/verify_switch_fix.py`：用面板**真实的 `<style>`** 生成最小复现页，
  把「旧写法 / 新写法」并排渲染并自动点击，交给 Edge 无头截图对比。
  旧写法的截图与用户反馈完全一致，新写法干净 —— 这类「点击后才出现」的
  UI 问题靠静态截图是发现不了的，留个可复跑的回归工具。

### 验证记录（2026-09-16 00:22）

**「定时任务发错会话」的修复（1.2.6）已通过真实链路端到端验证。**

- 方法：把微信先停到「文件传输助手」当诱饵，再用 `--clone` 复用线上任务的指令插入一条
  一次性 cron 任务（目标＝博士私聊），重启 AstrBot 等它自然触发。
- 结果：
  - AstrBot：`Tool 'send_message_to_user' Result: Message sent to session wechat_bridge:FriendMessage:1000000001`
  - 桥接：`已切到会话: RulerCordelius（列表匹配，等级 3）` → `[UIA✓] 文字已发送至 RulerCordelius`
  - WeFlow：消息落在**私聊**（00:22:11），**诱饵会话没有**收到 —— 正是修复前会发错的地方
- 附带查清两件事（详见 `docs/开发文档.md` §4.9.1 / §4.9.2）：
  1. 自检指令若写成「系统指令式」会被 AstrBot 的 `safety_mode` 判为提示词注入而拒答，
     或**谎报成功**（回了"已处理完成"却没调用发送工具）→ 自检一律 `--clone` 真实指令。
  2. `data/logs/astrbot.log` 启动后即停写、stdout 又块缓冲 → 排障要用
     `PYTHONUNBUFFERED=1 ... -u run_astrbot.py run` 启动。
- 新增工具：`scripts/park_wechat.py`（把微信停到诱饵会话）、`scripts/dump_conv.py`
  （看 agent 到底回了什么）、`scripts/clean_test_artifacts.py`（清自检留下的 cron 任务与
  对话历史垃圾，会先备份）。

## [1.2.6]

这一版的主题：**原生引用打通 + 定时任务发错会话根治 + 知识库接线**。

### 修复（严重）

- **定时任务/主动发送把消息发到了「当时打开的会话」**。9/15 21:30 两条定时消息
  （群晚安 + 私聊催睡）**全掉进了「文件传输助手」**，目标会话都没收到，
  而桥接日志却写着"已切到联系人 / 文字已发送至 1000000001"。
  三处叠加：
  1. `uia_sender.py` 里切不到目标会话时**故意**"尝试在当前窗口发送"→ 发错人；
  2. `state.get_contact()` 的直连映射表 `_ob_id_to_contact` 是空的，
     于是拿**原始数字 ID** 当联系人名去找会话，必然找不到；
  3. 补上反查后又暴露：私聊**聊天标题显示的是备注名**（RulerCordelius），
     而名册里 `names[0]` 是昵称（测试用户）→ 搜索切到 测试用户、标题却是
     RulerCordelius → 校验失败。
  修法：
  - ⚠️ **安全红线：切不到目标会话就放弃这次发送**，绝不发到当前窗口；
    切完还会再校验一次当前标题，不一致也放弃（日志打 `[UIA✗]`）。
  - `get_contact()` 增加**反查**：ob_id → 群（反查 `known_groups`）→ 人
    （`all_persons()[wxid].uid`），且人**优先用 `get_chat_name(wxid)`**
    （WeFlow contacts 的 displayName = 备注名 = 聊天标题）。
  - 回归脚本 `scripts/test_session_switch.py`：先把微信停在「文件传输助手」，
    再发到私聊 → 实测通过，WeFlow 确认落到正确会话。

### 修复（功能）

- **原生引用终于可用**。此前三次探测的结论"微信右键菜单 UIA 看不见"是**错的**，
  真因两个：
  1. `item.RightClick()` 点的是**整行中心**，那里是空白、菜单不弹；
     要点在**气泡**上（自己发的消息靠右，实测 x≈62% 命中）。
  2. 菜单**既不是新顶层窗口、也不是 Win32 的 `#32768`**，而是**微信主窗口的子节点**
     （路径 `/Weixin/引用`，`MenuItemControl`）；之前只找"新增顶层窗口"所以永远找不到。
  已实现 `_right_click_until_menu()`：横向扫描 6 个位置，每点一次用
  `_find_quote_menu()` 查找，判据是**同名 MenuItemControl 且父控件里有
  「复制/转发/删除」兄弟项**（避免把常驻同名控件误判成菜单）。
  参考 Mon3trBot v1.7.4 的 `send_quote()`（其关键点是引用前先 `roll_into_view()`）。

### 变更

- **知识库接线**：`arknights` 库其实早已导全（32 个文档 / 12326 分块，
  Mon3trBot 的方舟资料全在里面），但 `cmd_config.json` 的 `kb_names` 是**空的**，
  等于没接。已设为 `["arknights", "endfield"]`（知识库是自动注入的，
  见 `astr_main_agent.py`）。实测矿石病致死率、325 梗均可正确回答。

### 新增

- `docs/测试题库.md`：110 道题的回归题库（常识 / 方舟知识库内 / 方舟游戏本体 / 人设特化），
  每题带判定关键词与禁止词，文末写明判分规则与待复核项。
- `sim/qa_suite.py`：读该题库自动出题、通过模拟器跑、分组统计、输出 JSON 报告。
- `scripts/probe_quote5.py`：引用探测（`--sweep` 横向扫描 / `--do` 真发 / `--seed` 播种）。
- `scripts/clear_input.py`：清空某会话的聊天输入框（探测残留文字用）。
- `scripts/test_session_switch.py`：会话切换 + 发送的回归测试。

## [1.2.5]

这一版的主题：**修掉机器人把群友「群友C」叫成「群友C」的问题**。

### 修复

- **模型把日语汉字自动简体化，导致叫错群友名字**。
  查日志确认：收信侧 127 次全是正确写法「広」，18 处错误**全部出现在机器人发出的回复里**
  —— 是模型自己把「広」写成了「广」。两层一起修：
  1. `scripts/patch_persona_name_rule.py`：人设加「名字照抄」死命令
     （群友昵称一个字不许改，特别点名 広/沢/気 这类日语汉字；群名同理）。
     带备份与 `--revert`。
  2. `ob_protocol.fix_display_names()`：出站前用工整的名册再兜一道。
     **安全边界**——只有当"被简体化后的写法"能唯一对应到一个**已知名字**时才替换
     （用 196 个已知展示名建变体表，当前命中 1 条：群友C → 群友C）。
     所以"广东""车站"这类普通文字完全不受影响；命中时打日志
     `[OB11] 昵称纠错：… → …` 方便核对。
  3. 顺带新增 `state.known_display_names()`：汇总人名 / 群成员名 / 群名，供纠错使用。

## [1.2.4]

这一版的主题：**修复面板保存写坏 config.json + 机器人身份自动获取**。

### 修复（严重）

- **面板「保存配置」会把 config.json 写坏**：写入末尾多了一个字面量 `\n`（两个字符），
  之后 config.json 无法再被解析，桥接重启会直接崩、设置页也会提示"加载配置失败"。
  根因是 v1.2.3 重构面板时把 Python 的 `"\n"` 误写成了 `"\\n"`（共 3 处：
  两处写文件、一处日志拼接）。已全部改正，并修复了已损坏的 config.json
  （29 个键值完整保留，坏文件备份为 `config.json.bak_broken_*`）。

### 新增

- **机器人身份自动获取**（基础设置 → 机器人身份 →「🔍 从微信自动获取」）：
  - wxid：从 WeFlow `/api/v1/messages` 里找 `isSend=1` 的消息，`senderUsername`
    就是本机账号（最权威的来源，因为那条消息就是这台机器发出去的）。
    wxid 已填时不覆盖，只在为空时自动填入。
  - 昵称：从各群 `/api/v1/group-members` 里按 wxid 找到自己的条目，
    收集 displayName / nickname / 群昵称 / 备注。手动改过就不覆盖，点按钮才刷新。
  - 实测能识别出 wxid 与 `Mon3tr`。

### 修复（其他）

- **wxid 比对容错**（`bridge_core.ignore_reason`）：微信 4.x 有时给 wxid 带 `_xxxx` 后缀，
  本机配置是 `wxid_rvph2xhviigs19_79c1` 而 WeFlow 报的是 `wxid_rvph2xhviigs19`，
  原来的 `==` 比对永远不命中，"跳过机器人本机消息"这条保险失效
  （目前靠昵称兜底才没出事）。改成前缀互比，两个方向都能命中。

## [1.2.3]

这一版的主题：**Web 面板第 2/3 页重构 —— 更好看、更方便、把说不清的说清**。

### 变更

**基础设置页**
- 全部设置项分成三层：**常用**（机器人身份 / 消息行为 / 图片，常驻展开）、
  **进阶**（连接地址 / 端口 / 日志，折叠）、**上游遗留**（桥接侧图片转述等 9 项，折叠并标黄警示）。
  没有任何配置被删除（遵守「不得删除上游既有代码」），只是不再占着主视野。
- 新增**搜索框**：输入关键字即时过滤（如「图片」「端口」「token」），命中时自动展开折叠层。
- 布尔配置从下拉框改成**开关**；会改了要重启的字段加「需重启」角标（共 5 个）。
- 补上两个一直没暴露的配置：`mention_as_text`（@ 转文字，默认开）、
  `quote_reply_native`（原生引用，当前微信不可用，默认关）。
- 把「实测不可用」的选项标清楚：`send_method=weflow_api` 标注"必然发不出去"；
  `weflow_send_api` 标注"当前代码没有使用这个配置"。
- 保存条改为吸底常驻，并写明生效方式（群聊模式立即生效，其余需重启桥接）。

**成员与权限页**
- **新增「群里管什么」总表**：每个群一行，两列开关——
  **回复**（要不要理这个群，立即生效）+ **插嘴**（没人 @ 时会不会自己冒出来）。
  原先散在三处的「群回复开关 / 插话黑名单」收进这一张表。
- 插话黑名单改为**反向表达**：表格里勾 = 允许插嘴，不勾 = 进黑名单；
  不在已知群列表里的旧条目仍保留在「高级」区的手动补充框，不会丢。
- 当**插话白名单非空**时自动显示黄色警示（此时表格的勾选会被白名单收紧），
  避免出现"明明勾了为什么还不插嘴"的困惑。
- 重复的三段「白名单其他自定义条目」说明合并为一个「高级」折叠区，
  三层过滤关系（平台白名单 > 群回复开关 > 插话白/黑名单）用一段话讲清楚。
- 155 人的成员表改为**内部滚动**（表头吸顶），不再把下面的内容顶出两屏。
- 管理员立即生效、白名单/插话需重启 AstrBot，在保存条上写明。

### 新增

- 面板支持 **hash 深链**（`#settings` / `#members`）：刷新后停留在原页签。

### 修复

- 上游遗留的 `ollama_*` / `image_caption_*` 字段此前在表单里裸露，容易被误改；现折叠收起。

## [1.2.2]

这一版的主题：**定时任务静默失败根治 + 人设话题边界**。

### 修复（严重）

- **定时任务 / 所有主动发送 100% 静默失败**。用户在群里问「为什么定时任务没按时触发」。
  实测：任务**准时触发了**（`cron_jobs.last_run_at` 正常跳），失败在发送环节。
  根因是 `aiocqhttp.WebSocketReverseApi.call_action`：主动发送（无事件上下文）
  只能靠 `len(_api_clients) == 1` 选路由，而本机同时挂着**模拟器**
  （`sim/sim_wechat.py`，`X-Self-ID=99009900`）→ 抛 `ApiNotAvailable`。
  该异常不带参数、`str(e)` 为空，所以 AstrBot 侧只看到
  `error: failed to send message to session xxx: `（冒号后空白），
  DB 里却仍写 `completed` / `last_error=None` —— **悄无声息**。
  实测 2026-09-14 当天 **7 次调用 0 次成功**（含 6:00 早安天气、21:30 催睡群/私聊）。
  修复：`scripts/patch_aiocqhttp_primary_client.py`，多客户端时排除模拟器，
  剩余候选不唯一时仍拒绝（绝不猜）。回归测试 `scripts/test_aiocqhttp_routing.py`。
  **端到端验证通过**：23:16:00 的一次性 cron 实测
  `Tool send_message_to_user Result: Message sent to session ...`，
  桥接侧 `[UIA✓] … 文字已发送至 1000000001`，且模拟器仍在连接状态。

### 新增

- `scripts/cron_test_push.py`：主动发送链路自检工具。
  `--clone <任务ID>` 可复用真实任务的指令做高保真自检，`--cleanup` 清理。
- `scripts/test_aiocqhttp_routing.py`：补丁的回归测试（4 个场景，含事故场景）。
- `scripts/patch_persona_topic_guard.py`：给 mon3tr 人设加「话题边界」段落
  （可 `--revert`，原人设备份到 `data/backup/`）。

### 修复（其他）

- **人设无端倒游戏剧情**：bot 连续多轮主动大段复述《明日方舟》剧情
  （「我又想起好多罗德岛的事……」），并把群友的**现实**项目与游戏设定混为一谈，
  甚至编造出历史里根本不存在的人名（"杏仁"）与往事（全库检索 0 命中）。
  上下文只有 3.5 万 token，**不是上下文爆炸**，而是自我复述形成的正反馈循环
  （它自己的长输出进了历史，下一轮接着往下编）。已给人设加「话题边界」段落。

### 文档

- `docs/开发文档.md` §4.9：补全定时任务的执行链路（消息只能靠 agent 调工具发出）、
  「aiocqhttp 只能挂 1 个 OneBot 客户端」铁律、以及自检方法。
- `docs/开发文档.md` §6：补丁清单新增 `aiocqhttp/api_impl.py`。
- `AGENT.md`：已知坑新增上述铁律。

## [1.2.1]

这一版的主题：**上下文爆炸治理 + 出站段（引用/@）修复**。

> v1.2.0 是本地中间构建、未对外发布，本版包含其全部改动。

### 修复（严重）

- **UIA 发送器死锁**：`_lock` 用了不可重入的 `threading.Lock`，而 `send_quote` 会在
  持锁状态下调用 `send_text` 降级发送 → 第二次 acquire **永久自锁**，实测卡死发送线程
  80s+ 不返回。生产影响：AstrBot 每发一条带 `reply` 段的回复都会卡死桥接发送线程。
  已改为 `threading.RLock`。
- **上下文无限膨胀**：`/stats` 实测单会话达 **1,224,323 tokens**。根因是
  `agent_runner.config.compression.max_turns = -1`（永不按轮数截断）。治理方案见「变更」。
- **主动回复概率被还原**：`possibility_reply` 从 0.1 改回 **0.03**（此前改过又被覆盖）。
- **AstrBot 面板看不到黑名单**：此前只加了默认值与类型 schema，漏了 WebUI 提示区
  （`CONFIG_METADATA_3`）。已补 `provider_ltm_settings.active_reply.blacklist`。
- **`at` 段被静默丢弃**：出站只处理 `reply/text/image/file/face`，`at` 不在其中 ——
  所以群里的回复看不出在回谁。

### 变更

- **上下文治理改为 token 阈值优先**（原为轮数硬截断）：所有文本对话模型的
  `max_context_tokens` 设为 **65536**，超阈值走 `llm_compress` 把旧对话**总结成摘要**保留；
  `max_turns` 放宽到 **100** 只作兜底。理由：轮数截断是硬丢弃会真的忘，token 压缩保留语义。
  （此前 MiniMax-M3 窗口 1M、agnes-3.0-flash 512K，不设就是放任上下文涨到百万级。）
  ⚠️ 换模型时记得给新模型也设 `max_context_tokens`。
- **原生引用默认关闭**（`quote_reply_native=false`）：实测当前微信 4.x 的消息右键菜单是
  **自绘的、对 UIA 完全不可见**（右键 / 悬浮 / 先选中再右键三种方式均零新增控件、
  零新增顶层窗口），wxauto4 那套 `right_click + select_option("引用")` 在此版本上不可用。
  实现代码保留，将来微信版本变化可直接打开再试。
- **新增 `mention_as_text`（默认开）**：把 `at` 段降级成文字「@昵称 」，
  群里终于看得出这条回复在回谁；已自带 `@` 或 `@全体` 时不重复加。
- **面板补上黑名单文案（前端语言包）**：AstrBot 会把 schema 的 `description`/`hint` 转成
  i18n key（`ext_group.ltm.<path>.description`），真正显示的文字来自编译好的前端语言包
  （中/英/俄/日 4 种语言都内嵌在 `dashboard/dist/assets/index-*.js`）。只补 Python schema
  会导致面板显示**原始 key**。已用 `scripts/patch_astrbot_i18n_blacklist.py` 注入四语言条目。
- **桥接面板：平台 ID 白名单也改成勾选框**（原来只有主动回复的白/黑名单是勾选框）。
  抽出 `renderNameList`/`collectNameList` 两个通用函数，三处名单行为统一；
  不在已知群列表里的条目自动落入「自定义条目」框，不会丢。
- **新增 `sim/sim_test_suite.py`**：模拟器综合测试套件（10 项：功能如实答 / 模型名 /
  知识库 AK+终末地 / 称呼规则 / 语言跟随 英日 / 简洁度 / 上下文记忆），一键跑并输出汇总，
  实测 10/10 通过。
- **新增分析脚本** `scripts/analyze_log.py`、`scripts/analyze_mine.py`：统计各场景
  回复率、回复长度、分段情况（排障用，不参与打包）。

## [1.1.0]

这一版的主题：**主动回复黑名单 + 文件发送**。

### 新增

- **主动回复黑名单**（AstrBot 核心补丁）：`provider_ltm_settings.active_reply.blacklist`，
  与白名单并行、优先级更高——黑名单里的群（群 ID 或 UMO）绝不随机插话，不影响 @ 唤醒回复。
  AstrBot WebUI 的配置 schema 已同步扩展，面板可直接编辑；
  桥接面板「成员与权限」页同步增加黑名单群勾选框 + 自定义条目。
- **文件发送**（桥接）：OneBot `file` 段不再被静默忽略。支持 `file:///` URI、
  本地路径、`base64://`、http(s) 下载四种来源；发送走 UIA 剪贴板
  CF_HDROP 文件拖放格式（SetFileDropList → Ctrl+V → Enter），任意类型文件均可。

### 修复

- 之前 bot 生成文件后"发不出去"且桥接静默回 ok——现在文件段会真正投递，
  失败时日志明确记录 `[UIA✗] 文件`。

## [1.0.0]

**版号制的起点**（此前为 `1.0.1-rc.1 ~ rc.7` 预发布序列，全部归档在下方）。
从这一版起：版本号只用 `vX.Y.Z`，用 `scripts/build_release.py` 出包，
产物落在 `release/versions/vX.Y.Z/` 归档 + `release/newestbuild/` 最新镜像
（约定对齐 `C:\WechatBotShare`）。

### 变更

- **目录整合**：桥接运行环境与 AstrBot 一并迁入项目根 `runtime/`（bridge / astrbot），
  仓库、模拟器、文档、发布产物同处一个根目录，整目录可搬迁。
- **路径全部相对化**：配置里的 `astrbot_config_file`、`astrbot_attachments` 改为相对路径，
  以「项目根（桥接目录的上一级）」为基准；留空时按常见布局自动搜索
  （runtime/astrbot、同级 AstrBot…）。**拷到任何电脑、任何盘符、任何目录都能直接跑**。
- **发布流程脚本化**：`scripts/build_release.py` 读取 `VERSION`，自动生成版本归档、
  刷新 newestbuild、回滚备份（backups/pre-vX.Y.Z-*）与可选纯净 zip。

### 继承的历史改动（rc.1 ~ rc.7 一览）

- rc.2 群聊内置指令透传（`/sid` 等不再被"某某在群某某中说："外壳顶掉）
- rc.3 管理员/白名单可视化；同人同 ID（`user_id=md5(wxid)`，加一次全局生效）
- rc.4 群指令需 @ 触发、每群回复开关、白名单可视化、设置页重组
- rc.5 面板分工修正（图片理解交还 AstrBot；随机插话文案对齐）
- rc.6 修复 all 模式逐条必回（非 @ 消息不再被硬塞 At 段）
- rc.7 合并 mention/all 为「标准模式」

## [1.0.1-rc.7]

这一版的主题：**合并 mention / all两种普通模式**。

### 变更

- **「仅@回复」与「全部回复」合并为「标准模式」**：rc.6 修复 @ 段之后，两种普通模式在桥接侧的
  区别只剩「非 @ 消息要不要转发」——转发过去 AstrBot 也只是按随机插话（active_reply）掷骰，
  关掉就等于「仅@回复」。桥接不再需要第二个普通模式：
  - 面板选项由 3 个减为 2 个：**标准模式 / 批处理**；
  - 旧配置值 `mention` 在读取与运行时自动归一化为 `all`，无需改配置文件；
  - 上游 mention 相关代码保留不删（归一化后不可达），符合不删上游代码的约定。
- 「随机插话」的生效前提说明同步更新（不再要求切换 all，批处理除外）。

## [1.0.1-rc.6]

这一版的主题：**修复 all 模式逐条必回**。

### 修复

- **all 模式下每条群消息都被 AstrBot 当成 @ 唤醒**：推送事件里的 `@机器人` 段是无条件加的，
  `mention` 模式下只有 @ 过的消息能走到推送这一步所以从未暴露；切到 `all` 后所有非 @ 消息
  也被硬塞 `[At:]`，AstrBot 视为唤醒逐条回复，`active_reply` 随机插话机制完全失效。
  现在 @ 段只在**这批消息里真的有人 @ 过机器人**时才加（按批次记录、推送后复位，不跨批继承）。
- **all / batch 模式下裸斜杠指令未拦截**：「指令必须 @ 才触发」此前只挂在 mention 门槛上，
  现已独立成模式无关的拦截规则；@ 后仍无外壳透传。

## [1.0.1-rc.5]

这一版的主题：**明确面板分工，消除与 AstrBot WebUI 的重复感**。

### 变更

- **设置页分工修正**：桥接面板只保留微信侧管道配置（连接 / 机器人身份 / 消息 / 图片管道参数 / 高级）。
  「怎么理解图片」交还 AstrBot 面板（主模型多模态或其图片转述模型）；上游遗留的「桥接侧图片转述」
  降级到页尾独立分组并标注「默认关闭 · 一般不用改」，功能保留不删除。
- **主动回复文案对齐 AstrBot**：面板改用「随机插话」措辞并明确——这就是 AstrBot 配置里
  「随机小概率主动回复」（active_reply / possibility_reply）：非 @ 群消息按概率掷骰插话，
  勾选范围写入其 whitelist，**不影响 @ 唤醒的正常回复**。写入回路已实测（写→读→还原一致）。

## [1.0.1-rc.4]

这一版的主题：**群聊防串台 + 面板全面可视化**。

### 变更

- **群聊指令收回免 @ 特权**：`/sid` 这类斜杠指令在群里重新要求**必须 @ 机器人**才触发
  （rc.2 曾放宽为无需 @）。原因：一个群里跑多个机器人时，裸指令会被所有机器人同时响应、互相混淆。
  @ 后的指令仍无外壳透传（rc.2 的核心修复不受影响）；群图片「同一个人 @ 才读取」的暂存机制不受影响。
  私聊指令不变。

### 新增

- **每群回复开关**（会话静音）：面板「成员与权限」的群列表新增开关，关闭后该群所有消息
  （包括 @ 机器人）在桥接就被丢弃，机器人完全不掺和——同样面向多机器人共存场景。
  状态持久化在 `data/muted_sessions.json`，立即生效、无需重启。
- **主动回复白名单可视化**：从手填 textarea 改为「已知群勾选框 + 自定义条目」，
  现有白名单里无法匹配到已知群的条目会自动归入自定义区保留，不会丢。
- **设置页重组**：基础设置按「连接 / 机器人身份 / 消息 / 图片 / 高级」分区，去除重复分组；
  补齐此前面板缺失的配置项（引用回复前缀、图片大小上限、@ 等待窗、联系人切换方式、
  跳过日志开关、AstrBot 配置路径），布尔项正确保存为 true/false。

## [1.0.1-rc.3]

这一版的主题：**管理员与白名单的可视化管理 —— 加一次，处处生效**。

### 新增

- **人员注册表（`state.py` + 新模块 `people.py`）**：以微信 wxid 为稳定身份，
  user_id = `md5(wxid)`。同一个人在**私聊和所有群里是同一个 ID**，
  AstrBot 的 `admins_id` 只需登记一次，管理员身份即全局生效
  （此前群消息的 ID 是 `md5(群ID_昵称)`，每个群都不同，管理员要逐群添加）。
- **群成员名册**：启动时从 WeFlow `/api/v1/group-members` 拉取所有已知群的
  成员名单（wxid ↔ 昵称，多候选名匹配），每 10 分钟自动刷新；据此把群消息
  的发言人解析回 wxid。名册未命中的消息自动回退旧行为（群ID_昵称）并记日志。
- **Web 面板新页「成员与权限」**（`web_panel.py`）：
  - 成员列表：勾选即授管理员，一键「保存管理员」——写入桥接 `data/admins.json`
    并自动同步为 UID 写进 AstrBot `cmd_config.json` 的 `admins_id`；
  - 群会话表：每个已知群的「群 ID + 完整 UMO」一键复制（供 AstrBot 白名单使用）；
  - AstrBot 白名单/主动回复设置：平台 ID 白名单开关与名单、主动回复开关、
    概率、白名单，直接在桥接面板编辑并写入 AstrBot 配置。
- **管理员自动重放**：桥接每次启动把 `data/admins.json` 重放同步到 AstrBot
  （`main.py`），重启/换机无需人工再配。
- **AstrBot 主配置路径**：新增 `astrbot_config_file` 配置项（默认自动探测
  `../AstrBot/data/cmd_config.json`），写回时保留原文件 BOM。

### 变更

- 群消息的 OneBot `user_id` 由 `md5(群ID_昵称)` 改为 `md5(wxid)`。
  群会话本身仍按 `group_id`（= `md5(群ID)`）隔离，**群聊记忆不受影响**；
  仅"发言人身份"变化，AstrBot 侧 `admins_id` 里的旧格式 UID 会失效（由面板同步接管）。

> 注：AstrBot 只在启动时读取 `admins_id`，面板保存后需重启 AstrBot 生效。

## [1.0.1-rc.2]

对应上游 tag `v1.0.1`。这一版的主题是：**让群聊里的内置指令能用、不再被"外壳"吃掉**。

### 修复

- **群聊内置指令全部失效**（`/sid`、`/help`、`/reset` …）：桥接会把群消息包装成
  `某某在群某某中说：<原文>` 再发给 AstrBot，于是 AstrBot 看到的文本**不以 `/` 开头**，
  内置指令被当成普通聊天丢给大模型，用户永远等不到回复（私聊没有这层包装，所以私聊一直正常）。
  现在以 `/` 开头的群消息**原样透传**，不套外壳。
- **群聊里裸指令被提前丢弃**：`mention` 模式下，未 @ 机器人的群消息会被桥接直接跳过，
  `/sid` 这类指令还没到 AstrBot 就没了。现在以 `/` 开头的消息**无需 @** 也放行，
  与 AstrBot 自身的 `wake_prefix` 语义一致。
  （只放宽"是否放行"，**不影响图片门槛** —— 群图片仍只认同一个人的真实 @。）

> 注：私聊路径本就没有这层包装，`/sid` 在私聊里始终可用。改动只针对群聊。

## [1.0.1-rc.1]

对应上游 tag `v1.0.1`。这一版的主题是：**让桥接能长期稳定地跑起来**。

### 修复

**发送链路（UIA）**
- 消息被写进微信左侧搜索框：聊天输入框 `mmui::ChatInputField` 在控件树第 18 层左右，上游遍历深度上限 14 找不到，回退逻辑把唯一的另一个 EditControl（搜索框）当成了输入框。深度改为 26，并按类名精确定位
- 找不到合适输入框时不再退化为使用搜索框，直接中止
- `uiautomation` 2.x 移除了 `IsValuePatternAvailable` 与 `Control.SetValue`，原代码抛异常后退化成 `SendKeys`（发给当前焦点控件 → 落进搜索框）。改为 `GetPropertyValue(PropertyId…)` + `GetValuePattern().SetValue()`
- 发送按钮定位错误：原按"名称为空或含发送/空"匹配，抓到的是左侧栏无名图标（点了没反应，表现为"字填进输入框但发不出去"）。改为按类名 `mmui::XOutlineButton` + 名称「发送」定位
- 发送结果判定：改为依次尝试 发送按钮 → Enter → Ctrl+Enter，以「输入框是否清空」为唯一成功判据；失败会在日志明确报出，不再假装成功
- 发送前先 `SetFocus()`，避免 `SendKeys` 打到别的控件
- 窗口句柄：上游用 `Qt51514QWindowIcon` / `WeChatMainWndForPC`，微信 4.x 实际是 `mmui::MainWindow`（标题 `Weixin`）

**会话与身份**
- 会话 ID 用 `hash(wxid)`：**Python 对字符串的 hash 每进程随机化**，桥接一重启同一联系人就变成新会话，AstrBot 面板里表现为"每个私聊/群聊都出现两遍"。改为 MD5 前 8 字节，跨重启稳定
- 群 ID 改用 `xxx@chatroom` 而非群名，群改名不再产生新会话
- SSE 的 `groupName` 偶发缺失时，上游会用 `sourceName`（也是原始 ID）当群名，面板显示成 `xxx@chatroom`。改为持久化「微信 ID → 显示名」缓存（启动时从 WeFlow 全量预取 + 消息流回写）

**消息流**
- 推送期间到达的新消息会一直压在缓冲里（上游 `processing=True` 时不再排期）→ 推送完成后重新排期
- 自回复去重完全失效：上游 `_sent_recently` 只判断、从不写入，机器人会把自己的回复当成新消息再回一遍 → 发送时记录，接收时比对
- 本地请求走系统代理导致 502：在 `config.py` 代码层面补 `NO_PROXY`，不再依赖启动脚本
- `processed_ids` 无上限增长 → 加 FIFO（上限 5000）

### 新增

- **会话列表点击切换**：先在左侧会话列表按名字点击（精确 > 前缀 > 包含），失败再退回 Ctrl+F 搜索；切换后用标题栏校验是否真的切过去了。`switch_method`：`auto`（默认）/ `list` / `search`
- **群图片门槛**：图片必须与**同一个人的 @ 属于同一次请求**才读取——@ 之前发的先暂存等 @，@ 之后紧跟的直接读；换人 @ 不消费别人的暂存，超时作废。避免群里任何人发图都触发回复
- **图片理解两条路**（`image_caption_model`）：留空 → 图片原样以 `base64://` 段交给 AstrBot（由它用主模型直看或走转述模型）；填了 → 走上游的桥接侧描述（取图落盘 → 描述模型 → 发文字）。上游的描述体系完整保留
- **只读 OneBot 接口**：`get_login_info` / `get_group_info` / `get_group_member_info` / `get_group_member_list` / `get_group_list` / `get_friend_list` / `get_stranger_info` / `get_msg`。此前一律回空 `{}`，AstrBot 拿不到群名与成员昵称（实测它每次解析 @ 都会调 `get_group_member_info`）
- **被丢弃的消息会打日志**：`⏭️ 跳过 群[…] 某人: 原因（30s 内累计 N 条）`，按"会话+成员+原因"节流，可用 `log_skipped_messages` 关闭
- 新增配置：`switch_method`、`log_skipped_messages`、`quote_reply_prefix`、`web_host`、`image_mention_window`、`image_max_bytes`
- 联系人名持久化缓存（`data/chat_names.json`，已被 gitignore）
- 项目标识：`PROJECT_NAME = "Akasha-WeChat_RC"` + `VERSION` 文件，启动日志首行打印

### 变更

- `web_host` 默认 `0.0.0.0`（局域网可访问），只想要本机访问改 `127.0.0.1`
- `senders.create_sender()` 在 `send_method=weflow_api` 时额外打一条警告：WeFlow 实测**没有发送接口**（`/api/v1/message`、`/api/v1/send` 等均 404），该路径必然失败，正常请用 `uia`

### 其他

- 补齐维护文档：`README.md`（fork 说明）、`AGENT.md`（约定与踩坑）、`CHANGELOG.md`（本文件）、`VERSION`、`.gitignore`
- 确立维护约定：不删上游代码、同文件编辑串行、隐私红线、长驻程序用可见窗口启动

### 已知限制

- 微信原生「引用回复」气泡做不了（需要客户端右键菜单操作，UIA 不可靠）；`quote_reply_prefix` 只是文本模拟，默认关闭
- 群成员完整名单与群人数取不到（WeFlow 无对应接口），`get_group_member_list` 只含发过言的成员
- AstrBot 在拿不到群人数时会用 `len(成员列表)` 兜底，因此该数字偏小——已知，不编造

## 启动链路按接收端分流：面板可选 Alpha / WeFlow（2026-10-18）

### 修复
- `一键启动.bat` 原先无条件启动 WeFlow，导致面板切到 Alpha 后关闭 WeFlow 仍会被重新拉起；现读取
  `runtime/bridge/config.json` 的 `message_source`，Alpha 时只检查 :5030，不启动也不触碰 WeFlow。
- 一键启动的守护检测原先依赖窗口标题 `AkashaWatchdog`，桥接子进程会继承标题造成误判；改为按
  `watch_services.py` 命令行检测。`启动守护.bat` 增加独立窗口标题，防止再次认错。

### 变更
- `scripts/watch_services.py` 每轮重读配置动态生成守护目标：Alpha 只守护 AstrBot + 桥接，WeFlow 才
  额外守护 :5031；从面板切换无需重启守护，手动关闭 WeFlow 也不再被旧规则拉起。
- `wechat-weflow-bridge-ob11/web_panel.py` 设置页新增常驻「消息链路」：真实下拉框选择接收端
  （WeFlow / Alpha）与发送端（UIA / Alpha API）；状态卡由硬编码 WeFlow 改为「接收端」。
- 排查脚本 `scripts/probe_port_batch.bat` 同时探测 5030/5031/8766 并打印当前 `message_source`；
  `临时启动WeFlow_时间回拨.bat` 注明仅在接收端=WeFlow 时使用。

### 验证
- 一键启动在 `message_source=alpha` 时输出 `Alpha selected - skipping WeFlow`；重复运行能正确识别已运行的守护。
- 实关 WeFlow 根进程并等待 50 秒：无新 WeFlow 进程、:5031 保持关闭，:5030 Alpha、:8766 桥接、AstrBot 均在线。
- 守护 `--once` 输出接收端 ALPHA，目标只有 AstrBot + 桥接；面板 :8766、Alpha accounts 接口均 200。

## Alpha 消息兼容补丁（2026-10-18）

### 修复
- **引用消息（appmsg type=57）**：新增 `KIND_QUOTE` 与 `_parse_quote()`，把
  `<title>` 作为用户的新回复、`<refermsg><content>/<displayname>` 作为被引用上下文，
  输出为 `〔引用 某人：…〕\n新回复`。此前 type=57 被降级为
  `[未识别消息 type=57]`，模型只能看到标题片段。
- **空内容图片事件**：Alpha SSE 的图片事件可能 `content=""` 且 `type=3`，
  现在会归一成 `[图片]` 并进入既有取图链路；空文本事件仍按旧逻辑跳过。
  此前这类图片被 `ignore_reason()` 判成“空内容”直接丢弃。
- 被引用内容如果是另一段 XML，会压缩成 `[图片]/[视频]/[语音]/[富媒体消息]`
  或其 `<title>`，不再把整段 XML 塞进模型上下文。

### 验证
- 用 Alpha `/api/v1/messages` 拉取真实群消息，13 条 type=57 全部解析为 `quote`，
  不再出现 `[未识别消息 type=57]`。
- 真实样本包含“引用文本”和“引用图片 XML”两类，均输出可读上下文。
- 构造空 content + type=3 事件 → `[图片]`；空 content + type=1 仍为空并按旧逻辑跳过。
- 源码/运行副本同步后语法检查通过；重启桥接后 Alpha SSE 与 AstrBot 均在线。

## Alpha 原生消息源接入（2026-10-18）

### 新增
- `message_source.py`：消息源抽象层，把 WeFlow / Alpha 两套 HTTP API 差异（鉴权、SSE 参数、群成员参数、内容归一）统一封装
- 配置项 `message_source`（`weflow` / `alpha`），`alpha_base_url` / `alpha_access_token` / `alpha_account_id`
- `AlphaApiSender`：Alpha 原生 `POST /api/v1/messages/send-text` 发送器（需付费激活，默认不启用）

### 变更
- `bridge_core.py`：SSE / contacts / group-members / messages / media 全部走 `message_source` 抽象层
- `main.py`：启动自检支持 Alpha（Alpha `/api/v1/messages` 必须带 `talker`，改探 `/api/v1/accounts`）
- `senders.py`：`send_method=weflow_api` 归一化为 `uia`（WeFlow 没有发送接口，选了必失败）；新增 `alpha_api` 选项
- `web_panel.py`：连接组新增消息源选择 + Alpha 3 项；发送方式改为 `uia` / `alpha_api`；`_detect_bot` 支持 Alpha
- `state.py`：新增 `resolve_name_in_group()`（wxid → 群内昵称）
- `scripts/watch_services.py`：按 `message_source` 决定是否守护 WeFlow（选 alpha 时不拉 WeFlow/RunAsDate）
- `scripts/sync_to_rc.py`：同步列表加入 `message_source.py`

### 已知差异（已适配）
- Alpha 群消息 `sourceName` 是发言人 wxid，不是昵称
- Alpha 群成员参数名是 `chatroomId`，不是 `talker`
- Alpha `media=true + order=desc + limit>100` 会 422 `media.hydration_budget_exceeded`（取图改用 limit≤50）
- Alpha 发送文字未激活时返回 409 `SEND_CONNECTION_REQUIRED`（实测确认，桥接会失败并打日志）

### 验证
- Alpha API：accounts / sessions / contacts / group-members / messages / media / SSE / MCP 全部实测通过
- SSE：连接后持续收 `stream.ready` + `message.new`（群文本 + XML 表情）60 秒无断连
- 群名册：12/12 群、336 条昵称映射成功拉取
- wxid → 昵称解析：实测从 Alpha 的群消息 wxid 成功解析出该成员昵称 ✅
- 图片：media API 返回 `/api/v1/media/...` + Bearer 下载，实测 6233 字节 JPEG ✅
- AlphaApiSender：未激活时正确返回 False + `SEND_CONNECTION_REQUIRED` ✅
- 桥接端到端：Alpha SSE → 归一化 → add_to_buffer → process_sender → AstrBot 推送，全链路无报错 ✅
- 生产桥接已切换到 Alpha 并保持运行（AstrBot 已连接，SSE 已连接）
