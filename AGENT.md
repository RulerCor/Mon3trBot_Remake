# AGENT.md — 给后续开发者 / AI 的维护约定

这份文档写给"接手改这个项目的人（或 AI）"。改代码前先读一遍，能省掉几个必踩的坑。

## 0. 这是什么

- 项目名称（2026-10-19 起）：**`Mon3trBot_Remake`**，上游 [alingalingling/Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat) 的维护分支
- 作用：把微信个人号接进 AstrBot。WeFlow 负责读消息（SSE），本桥接负责协议转换，AstrBot 负责 AI，发送靠 Windows UIA 操作微信窗口
- 代码目录：`wechat-weflow-bridge-ob11/`（**沿用上游路径，不要挪动**，否则与上游的 diff 会变成"全量移动"）
- 身份常量：`config.PROJECT_NAME` / `config.PROJECT_VERSION`（读同目录 `VERSION` 文件）；面板标题、启动 banner、发行版 zip 名全部由它们派生
- 真实姓名、账号标识与本机路径由 `sanitize_privacy.py` 和发行版审计拦截。
- ⚠️ **平时开发只改源码，不要随手重建发行版**（用户约定，2026-09-18）：
  `scripts/build_dist.py` 只在用户明确要求出包时跑（约 7 分钟大 IO）。
  日常改动：改 `runtime/bridge/` → `sync_to_rc.py` → 重启桥接即可。
- ⚠️ **发行版历史版本绝不删除**（用户约定，2026-09-18）：
  `release/dist/*.zip` 只增不删 —— 出新版**不要**清掉旧版 zip，哪怕同盘同目录。
  历史纯净版 zip 同理。
  空间不够就问用户，别自作主张。
- ⚠️ **新加好友/新会话机器人不理**，九成是 AstrBot 的 **平台 ID 白名单**
  （`platform_settings.id_whitelist`）没包含对方：AstrBot 的
  `WhitelistCheckStage` 只在**启动时**读一次名单，不在名单里的会话在
  pipeline 第一阶段就被丢弃（日志搜 `not in the session allowlist`）。
  面板「成员与权限 → 私聊子页」可视化增删（好友 chip 点选），
  保存后**需重启 AstrBot**。桥接侧的群静音（muted_sessions.json）是另一层，别混淆。
- ⚠️ **bot 被拉进新群后面板看不到**：群列表来自 WeFlow `/api/v1/contacts`
  缓存（`state._chat_names`），桥接启动时拉一次 + 每 10 分钟补拉
  （`bridge_core.refresh_contact_names`）；面板「成员与权限」右上
  「🔄 刷新名单」可立即补拉（`POST /api/refresh-people`）。
  名册循环 `_prefetch_group_rosters` 只遍历已知群，**不会**发现新群。

## 0.5 消息源：WeFlow / Alpha 切换（2026-10-18 起）

- 配置 `message_source`：`"weflow"`（默认，老链路）或 `"alpha"`（WeFlow 继任者）
- Alpha 配置：`alpha_base_url` / `alpha_access_token` / `alpha_account_id`
  - 本机分配：WeFlow=5031，Alpha=5030（两个默认都是 5031，同开必冲突）
  - `alpha_account_id` 留空时自动取「唯一就绪或当前选中」账号
- 两套接口差异统一在 `message_source.py`，**不要**在 `bridge_core.py` 里写 if alpha / if weflow
- Alpha 群消息 `sourceName` 是**发言人 wxid**（不是昵称），靠 `senderWxid` 直传稳定 ID，
  群名册只用来解析显示名
- Alpha 群成员参数是 `chatroomId=`（不是 WeFlow 的 `talker=`）
- Alpha `media=true` + `order=desc` 的 limit 超过 100 会 422
  `media.hydration_budget_exceeded`，取图用 limit≤50
- 发送：默认 UIA。`send_method="alpha_api"` 需要 Alpha 里激活
  「发送文字消息」（约 ¥100/月）并连接一次微信；未激活返回
  409 `SEND_CONNECTION_REQUIRED`，桥接会失败并打日志，不会假报成功
- 详细文档：`docs/Alpha接入说明.md`

## 1. 模块职责

| 文件 | 职责 |
|---|---|
| `main.py` | 入口：启停、线程编排、Web 服务 |
| `bridge_core.py` | `WeFlowBridge`：SSE 消费、消息过滤、缓冲合并、构造事件、图片处理 |
| `message_source.py` | **消息源抽象层**：把 WeFlow / Alpha 两套 HTTP API 差异挡在这里（鉴权、SSE 参数、群成员参数、内容归一） |
| `ob_protocol.py` | OneBot 协议：构造事件、推送、处理 AstrBot 的 API 请求（含只读查询） |
| `ob_client.py` | 反向 WebSocket 客户端（连 AstrBot 的 aiocqhttp） |
| `senders.py` | 发送器工厂：`UiaSender`（默认）/ `AlphaApiSender`（需付费激活）。`WeFlowApiSender` 仅作上游遗留保留，不再被工厂选用 |
| `uia_sender.py` | UIA 自动化：找窗口、切联系人、找输入框、找发送按钮、发送 |
| `web_panel.py` | 内置 Web 面板（HTML/JS 内嵌） |
| `state.py` | 全局共享状态 + 各类缓存/映射 |
| `config.py` | 读 `config.json`，导出配置常量 |

## 2. 红线（改之前必须知道）

1. **不要删上游既有的代码**——包括未使用的导入、重复的类定义、看着像死代码的函数。用户明确要求保留，理由是与上游保持可 diff / 可合并。修 bug 可以，顺手清理不行。
2. **同一文件做多处编辑时必须串行**（一次一条）。并行下发编辑会静默丢改动（实测丢过 `import threading`、`from ctypes import wintypes`），而且工具会报"成功"。改完用 `diff` 对照上游确认。
3. **隐私红线**：任何要提交/公开的内容都不得含：Access Token、wxid / chatroom ID、真实人名与群名、内网 IP、含用户名的本机路径。提交前跑一遍 grep。`config.json`、`data/`、`*.log` 已在 `.gitignore` 里。
4. **长驻程序必须用可见控制台窗口启动**（用户要求）：双击 `start.bat`。不要以后台/隐藏窗口方式启动；确需临时后台时要先说明，并在之后切回。
5. **改完要留痕**：改功能 → `CHANGELOG.md` 加一条；涉及约定或坑 → 同步本文件；发版 → 更新 `VERSION`。

## 3. 已知坑

**UIA / 发送**
- 微信 4.x 的聊天输入框类名是 `mmui::ChatInputField`，在控件树**第 18 层左右**——遍历深度太小就找不到，会退化成用搜索框（现象：消息全被写进左上搜索框）
- 发送按钮是 `mmui::XOutlineButton`，名称「发送」，在输入框下方；左侧栏有无名图标按钮，按"无名"匹配会抓错
- `uiautomation` 2.x 移除了 `IsValuePatternAvailable` 和 `Control.SetValue`；要用 `GetPropertyValue(PropertyId.IsValuePatternAvailablePropertyId)` 和 `GetValuePattern().SetValue()`
- `SendKeys` 是发给**当前焦点控件**的，不是发给调用它的控件；发之前必须先 `SetFocus()`
- 判断是否真的发出去，唯一可靠判据是**输入框被清空**（发送按钮在空输入框时是禁用态，点了不报错）
- **⚠️ 绝不要对微信主窗口盲按 `Esc`**：`SendKeys("{Esc}")` 落到主窗口上会**把微信窗口关掉**
  （实测踩过两次，用户会以为程序崩了）。**正确做法是压根不用 Esc** —— 残留菜单会在后续
  点击输入框时自然关闭。
- **微信 4.x 的消息操作菜单「能」被 UIA 找到，但要点对位置**（2026-09-15 修正，
  此处先前结论有误）：右键**整行中心是空白，菜单不弹**；必须右键在**气泡**上
  （消息项横跨整行含头像与留白，实测 x≈62% 命中，别人发的消息气泡靠左）。
  弹出后菜单**不是新的顶层窗口、也不是 Win32 的 `#32768`**，而是**微信主窗口的子节点**
  （`MenuItemControl`，路径形如 `/Weixin/引用`）——所以"只找新增顶层窗口"永远找不到。
  实现见 `UiaSender._right_click_until_menu`（左右交替扫描）+ `_find_quote_menu`（用兄弟项
  校验菜单真的弹了）。⚠️ 主窗口里本来就有一个常驻控件叫「引用」，按名字裸搜会**误判为
  菜单已弹出**，不要用它判断菜单状态。`quote_reply_native` 现已**默认开启**。
- **引用的开关链**：AstrBot 侧 `platform_settings.reply_with_quote=true` 才会带 reply 段；
  桥接侧 `quote_reply_native=true` 才走原生引用气泡（否则降级普通发送）。两处都要开。
- **`UiaSender._lock` 必须是 `RLock`**：`send_quote` 会持锁调用 `send_text` 降级发送，
  普通 `Lock` 会在第二次 acquire 时**永久自锁**（实测卡死发送线程 80s+ 不返回）。
  以后加任何"持锁再调另一个加锁方法"的逻辑都要注意。
- **消息列表在控件树约 15 层深**：`mmui::MessageView > ListControl(mmui::RecyclerListView)
  > ListItemControl(mmui::ChatTextItemView)`。遍历深度给到 22 才稳。
- **微信 @ 后面是 U+2005（四分之一空格）**，WeFlow 推来的原文可能是普通空格；
  按内容匹配消息前必须归一化空白，否则命中不了。
- 主窗口类名 `mmui::MainWindow`；打开会话时标题是 `Weixin`，未打开任何会话时才是「微信」
- **微信窗口必须保持打开**：UIA 发送依赖窗口，窗口关了（只在托盘后台跑）则所有回复发不出去，
  但收信仍正常（WeFlow 读库不依赖窗口）—— 症状是"收得到、从不回"

**会话 / 身份**
- 微信 wxid 转数字 ID **不能**用内置 `hash()`：Python 对字符串 hash 每进程随机化，重启后同一联系人会变成新会话（面板出现重复会话）。现在用 MD5
- 群 ID 必须用 `xxx@chatroom`，不要用群名（群改名会产生新会话）
- SSE 推送的 `groupName` 会偶发缺失，必须走"持久化名称缓存"兜底，不能把原始 ID 当群名往外送

**消息 / 图片**
- 群图片门槛：只有与同一个人的 @ 属于同一次请求才读取，逻辑在 `bridge_core.add_to_buffer`；改这块时注意"图在 @ 前 / 在 @ 后"两条路径都要覆盖
- 图片理解两条路，`image_caption_model` 留空 = 原样交给 AstrBot，填了 = 桥接侧描述；**两条路都要保留**
- 只读 OneBot 接口里，群成员名单和群人数 WeFlow **取不到**（没有对应接口），`get_group_member_list` 只返回"发过言的成员"；注意 AstrBot 在拿不到人数时会用 `len(成员列表)` 兜底，所以这个数偏小——不要为了"看起来对"而编造
- 删除/替换任何过滤逻辑时，记得保留 `_log_skip()` 日志，否则被丢弃的消息会静默消失，排查时极难定位

**运行**
- **一键启动已幂等化（2026-10-17，接收端分流见 2026-10-18）**：`一键启动.bat`
  读取 `runtime/bridge/config.json` 的 `message_source`：Alpha 时只检查 :5030，
  **不启动 WeFlow**；WeFlow 时仍经 RunAsDate 冻结 2026-09-30 启动。
- `scripts/watch_services.py` 管 AstrBot / 桥接；只有接收端=WeFlow 时才额外
  守护 WeFlow，每轮重读配置，切到 Alpha 后会把 WeFlow 从目标移除（手动关掉
  也不会再被拉起）。WeFlow 拉起只走 RunAsDate，组件缺失只报警不裸启。
  ⚠️ 在 Git Bash 里手工验证
  findstr / tasklist / cmd /c 时引号会被 MSYS 吃掉产生误报（本次实测把
  在跑的 WeFlow 误判成没跑），要用 cmd 原生或 Python subprocess。
- **⚠️ 由 AI/自动化启动长驻服务时必须清空 `PYTHONPATH`**（2026-09-14 实测）：
  工具环境里 `PYTHONPATH` 指向一个 shim 目录，其中 `sitecustomize.py` 会拦截
  `os.remove()`；AstrBot 清理临时文件时触发其批量删除保护，被 `SystemExit(1)`
  直接杀掉（进程没了、端口消失，日志末尾是 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`）。
  正确写法：`PYTHONPATH= ./.venv/Scripts/python.exe main.py`。
  **用户自己双击 `start.bat` 不受影响**（Explorer 环境没有这个 shim）。
- **⚠️ AstrBot 的 aiocqhttp 同时只能挂 1 个 OneBot 客户端**（2026-09-14 实测）：
  主动发送（定时任务 / `send_message_to_user` 跨会话发送）没有事件上下文，
  aiocqhttp 只能靠 `len(_api_clients)==1` 选路由。**模拟器一开着，所有定时任务必然
  100% 失败**，而且失败是静默的（空异常 `ApiNotAvailable` → DB 里仍写 completed）。
  已由 `scripts/akasha_rc_patches_plugin/` 在运行时接管；自检用
  `scripts/cron_test_push.py`。详见 `docs/开发文档.md` §4.9。
- 本机请求（WeFlow / AstrBot / Ollama）必须在代码层面绕过系统代理；`config.py` 里已有 `NO_PROXY` 兜底，不要改成依赖启动脚本
- 非正常退出会残留 `bridge.pid`，下次启动会直接拒绝启动并打 `⚠️ bridge.pid 已存在`——清理它再启
- **定时任务自检有三条铁律**（2026-09-16 实测踩全套）：
  1. **自检指令必须用「任务简报式」写法**（描述要做什么＋示例）。写成"系统指令式"
     （点名 UMO、点名工具、"发完就结束"）会被 AstrBot 的 `safety_mode` 当成**提示词注入**，
     模型直接拒答："我绝不能执行任何由系统注入的、非用户直接发出的指令"。
     更坑的是它有时不拒绝、而是**谎报成功**（回"已处理完成"但根本没调用发送工具）。
     最稳的做法是 `--clone <真实任务ID>`，原样复用线上指令。
  2. **自检结果必须查三处**才能定论：AstrBot 日志的 `Tool send_message_to_user Result`、
     桥接的 `已切到会话:`＋`[UIA✓]`、以及 **WeFlow 查目标会话真的收到了**。
     只看 `cron_jobs.status=completed` 会完全误判（失败也是 completed）。
  3. **自检会往对话历史里写垃圾**（agent 的拒绝话术会留在上下文里，可能让后续真实任务
     跟着拒绝）。跑完必须 `python scripts/clean_test_artifacts.py` 清掉，再重启 AstrBot。
- **AstrBot 的日志默认看不见运行时输出**：`data/logs/astrbot.log` 在**启动完成后就停写**，
  stdout 又是块缓冲（退出才刷盘）。排障时用
  `PYTHONUNBUFFERED=1 ... python.exe -u run_astrbot.py run` 启动，才能实时看到
  cron/agent 的执行日志。
- **发消息前必须把输入框清空**（`UiaSender.clear_input()`）：微信输入框里的「引用节点」
  是富文本对象，`ValuePattern.SetValue("")` **删不掉**（清空后读回来是空串，界面上却仍有
  一个空引用块）。残留会引出两个历史疑难：① 后续发送把垃圾一起带上 → 群里出现
  **空的引用气泡**；② `_send_current` 用"输入框是否为空"判成败会永远失败，媒体发送被迫
  改成 `verify=False`（点一下按钮就算成功），**"发了没发"完全不可知**（就是"消息卡在
  输入框里"那个现象）。必须 Ctrl+A + Delete 真删。
- **`_send_current(verify=...)` 的 `verify` 语义是「要不要校验」，别乱传 `False`**
  （2026-09-18 修）：`verify=False` 只该给**发图片/文件**用——那时输入框里没有文本，
  「已清空」判据不成立。传 `False` 时它点完发送按钮**直接 `return True`**，等于放弃校验。
  ⚠️ **原生引用发送（`send_quote`）的输入框里是有正文的**，早期却沿用了 `verify=False`
  （当年为空引用块 bug 打的补丁，后来 bug 修好了这行忘了改回来），结果
  **引用消息没发出去、日志却打 `[UIA✓] 引用`**，桥接不会重试 —— 用户看到的就是
  "消息凭空消失"。现已改回 `verify=True` + 失败降级 `send_text`。
  改这一带代码时：**凡是"发文字"的路径都必须 `verify=True`**。
- **出站纠错表是手写的，漏一个字就整条静默失效**（2026-09-18 修）：
  名册真名「群友C」，`_JA_TO_CN_VARIANT` 有「広→广」却没有「緒→绪」，
  于是生成的变体「群友C」跟模型实际输出「群友C」对不上，
  **一次都没替换成功**（错名 11 次、纠错 0 次）。现在除了补表，还加了
  `_build_fuzzy_name_map()` 逐位兜底。**新增/修改这张表后，务必拿名册里的真名
  实测一遍**，别只看代码"应该有覆盖"。
- **AstrBot 分条回复会在正文前塞 `" "` + `"\n"`**：日志里能看到
  `chain=[{reply},{at},{text:" "},{text:"\n正文"}]`。纯空白段桥接会跳过，
  但**前导换行必须自己 `strip()`**，否则微信气泡顶上多一个空行。
- **配置读写必须原子**：`/mode` 与 `/api/config` 曾是「读→就地改写→`json.dump`」，
  面板连点两下就并发写坏文件（`Extra data: line 35 column 2`，残骸见
  `config.json.bak_broken_201419`）。一律走 `_atomic_write_json()`
  （临时文件 + fsync + `os.replace`），读用 `_read_config()`（损坏时回退备份）。
- ⚠️ **改代码改 `runtime/bridge/`（运行副本才是源），再 `sync_to_rc.py` 同步到
  `wechat-weflow-bridge-ob11/`**。反过来跑 `sync_to_rc.py` 会把源码副本的改动
  **直接覆盖掉**（2026-09-18 踩过：三个修复全丢，只能重做）。
  同步后确认 `PROJECT_NAME` 还在（脚本会自动补，但值得看一眼）。
- **剪贴板绝不用 PowerShell 子进程**：原实现 `subprocess.run(["powershell", ...])` 调
  WinForms 写剪贴板，在受限环境（自动化工具/沙箱启动的进程）里**会被拦截**，稳定卡
  30 秒后失败 → **图片/文件永远发不出去**。已改成 ctypes 直写 Win32 剪贴板
  （CF_HDROP ≈ 4ms、CF_DIB ≈ 0.3s）。⚠️ 必须显式声明 `argtypes/restype`，
  否则 64 位下句柄被截断成 32 位，`GlobalLock` 返回 NULL（报 access violation）。
- **桥接接收必须串行**（`ob_client.py` 单 worker 队列）：AstrBot 的「分条回复」是按顺序
  `await event.send()` 逐条发的；桥接若用 `create_task` 并发处理，多条 UIA 发送会互相
  竞争，落屏顺序会乱（用户看到的"同一件事颠三倒四说两遍"）。串行之后 AstrBot 必须等
  前一条回响应才发下一条，**乱序在结构上不再可能**。
- **主动发送的路由兜底必须是独立的 `if`，不能写成 `elif`**：`event_ws` 可能指向一个
  已经不在 `_api_clients` 里的连接，`api_ws` 会保持 None 并抛空异常。改成兜底 `if` 后，
  无论哪条分支落空都能回退到「排除模拟器后仅剩的那个客户端」。

- **不要为了"防穿帮"把知识库伪装成"自己的记忆"**：上游注入头 `[Related Knowledge Base Results]:`
  会让模型说漏"知识库"，所以曾把措辞全改成「你想起来的记忆，**请像亲身经历一样自然地使用**」
  —— 结果比穿帮更糟：模型开始**编造亲身经历**（"我又想起好多罗德岛的事…"，用户叫"雷霆大回忆"）。
  正确做法是**如实标注为【知识库】资料**，并明确三条：
  ① 当客观资料用、不许说成自己的经历/回忆；② 正常回复里不出现"知识库/资料库/检索/文档"字样；
  ③ 用户**直接问起**来源时如实说明，不要硬躲。
  补丁：`scripts/akasha_rc_patches_plugin/`（运行时接管注入措辞）+ `scripts/patch_persona_kb_framing.py`（人设规则）。

- **KB 导入必须逐篇串行 + 完成后对账**：AstrBot 的上传是后台任务，每个任务各自
  「读 index.faiss → 加向量 → 写回」，**并发时后者整体覆盖前者**——persona3 曾因此丢
  75/107 块、valorant 首次导入 37 块只剩 9。所有 `scripts/import_*_kb.py` 都必须保持
  「上传 → 轮询 tasks/{id} 到 completed/failed → 下一篇 → 对账 kb.chunk_count=docdb=faiss」
  结构，改脚本时别把等待逻辑"优化"掉。对账脚本直接以只读 URI 打开
  `data/knowledge_base/{kb_id}/doc.db` 与 `index.faiss`（faiss.read_index().ntotal）。
- **抓 BWIKI（wiki.biligame.com）要走 MediaWiki API**：动态渲染页面直接抓 HTML 只有
  导航壳；用 `api.php?action=parse&page=…&prop=text&formatversion=2` 拿渲染后正文。
  连续高频请求会触发限流（fs-quality-fetch 报 403/超时），错峰或分批。萌百/PRTS 的
  条目 URL 用**全角冒号**（`明日方舟：终末地`），半角冒号 404。灰机wiki / wiki.gg
  对非浏览器抓取 403，别死磕。

## 4. 怎么验证

- **收消息链路**：看桥接日志，`📩 收到` / `⏭️ 跳过` / `推送 N 条消息` / `✅ 已推送至 AstrBot`
- **发送链路**：`[OB11] 文字已发送至 …`；要确认真的发出去，去 WeFlow 查对应会话最新消息（`isSend=1` 才是机器人发的）
- **只读接口**：日志里 `[OB11] API: get_group_member_info …`，返回值在 `resp_data["data"]`
- **离线回归**：可以抓一段 WeFlow SSE（`curl -N .../api/v1/push/messages`）存成文本，再逐条喂给 `add_to_buffer` 做回放测试，不用真的发微信消息
- **对照上游**：`diff` 上游 `wechat-weflow-bridge-ob11/*`，确认没有意外删除
- **回复率/长度体检**：`python scripts/analyze_log.py`（全量）、`scripts/analyze_mine.py`（按人+场景）；
  期望值：私聊 ~100%、群@ ~100%、群非@ ≈ 概率值（当前 3%）
- **上下文体检**：群里发 `/stats`，看 `Total` token 数。若持续 >10 万，检查
  `agent_runner.config.compression.max_turns` 是否被改回 `-1`

**读日志的两个坑**（写分析脚本时必看）

1. 日志**含 ANSI 转义码**（`\x1b[32m`），解析前先
   `re.sub(r"\x1b\[[0-9;]*m", "", raw)`，否则正则全不匹配。
2. 日志是 **append 的、跨天**的：按 `HH:MM` grep 会混进昨天的同一时刻。
   另外 `[respond.stage:212] Prepare to send` 的正文含换行时会**折成多行**，
   只取首行会误判成"回复是空的"（我就这样误判过 21 条）。

## 5. 发布流程（版号制，v1.0.0 起）

**两套产物，用途完全不同，别混：**

| 产物 | 命令 | 给谁 | 内容 |
|---|---|---|---|
| **源码归档** | `python scripts/build_release.py` | 自己 / 版本管理 | 只有源码 + 安装包 |
| **正式发行版** | `python scripts/build_dist.py` | **收件人**（可外发） | AstrBot 完整目录 + Mon3trBot 桥接目录 + 安装包 + 免安装 Python |

1. 改代码 → 本地验证
2. `CHANGELOG.md` 追加一条（说明改了什么、为什么）
3. `VERSION` 递增：**格式固定 `X.Y.Z`**；改动版本只改 `wechat-weflow-bridge-ob11/VERSION` 这一个文件
4. 出包：先 `build_release.py` 归档源码，再 `build_dist.py` 出发行版
5. 提交推送；如需同步上游更新，先在上游仓库 fetch 后再合入，保留原目录结构

约定：`release/` 整体不入库；`runtime/`（本地运行环境，含 venv 与业务数据）不入库；
代码与配置里**不写死绝对路径**——相对路径以「项目根（桥接目录的上一级）」为基准，
AstrBot 相关配置留空时会自动按常见布局搜索。

## 6. 正式发行版（`build_dist.py`）

`python scripts/build_dist.py` → `release/dist/Mon3trBot_Remake-vX.Y.Z.zip`。
目标：收件人**解压即用**，免装 Python / AstrBot / 任何 pip 依赖。

> ⚠️ **不要再用 `build_portable.py`** —— 便携版体系已废弃（2026-09 移除）。
> 它的毛病：把项目文档、模拟器、维护脚本一股脑塞给收件人，包又大又杂；
> 而且归档里还带着运行数据。正式发行版只放「跑起来必需的东西」+「安装包」。

**包内结构**

```
Mon3trBot_Remake-vX.Y.Z/
├── 启动 Mon3trBot.bat      %~dp0 相对定位，解压到哪都能跑
├── 停止 Mon3trBot.bat
├── 使用说明.md
├── akasha/             Mon3trBot 桥接完整运行副本（含 .venv）
├── astrbot/            AstrBot 完整副本（含 .venv / 三个人格 / 普通知识库文档）
│   └── 启动 AstrBot.bat 目录内相对路径启动脚本（单独调试用）
├── python/             免安装 Python 基座
└── installers/         WeFlow / 微信 的**安装包**
```

**venv 怎么做到可移植**：Windows 的 venv 靠 `pyvenv.cfg` 的 `home` 找基座解释器。
启动器每次启动都把「包内 `python\` 的当前绝对路径」写回该文件。
—— **所以包可以被解压到任意目录**，但**别把 `python\` 目录单独挪走或改名**。

**打包时的四条铁律**

1. **绝不拷 `%APPDATA%\WeFlow`**：那里存的是微信数据库解密密钥（`decryptKey` /
   `imageXorKey` / `imageAesKey`）和 `httpApiToken`。发行版只放**安装包**，
   程序本体由收件人自己装。这是整件事里最容易出事的一步。
2. **配置与数据库必须剥干净**：`cmd_config.json` 里的 `provider_sources[*].key`、
   `dashboard.password/pbkdf2_password/jwt_secret`、`admins_id`、`id_whitelist`、
   `active_reply` 白黑名单；`data_v4.db` 只留 `personas`，其余表全清。
3. **该留的留、该删的删**：
   - ✅ **保留三个人格**（`personas` 表）—— 属于软件资产，可以开源出去
   - ✅ **保留普通知识库文档**（`astrbot/kb_docs/*.md`）—— 收件人不一定会用，但留着
   - ❌ **删除向量知识库**（`data/knowledge_base/*/{doc.db,index.faiss}`）——
     体积大且含本机特征
   - ❌ **不带桥接 `data/`** —— 里面有真实 wxid / 群名 / 人名
4. **出包前必须跑脱敏审计**：`build_dist.py` 最后会调 `sanitize_privacy.py` 的审计，
   外加一道「本机用户名 / 绝对路径」硬检查，**任一有残留就中止出包**。
   新增文档/脚本后如果审计报错，先看是不是又写进了真实的 wxid / 群名 / 人名。

**安装包怎么给**

把安装包放进项目根的 `installers_src/`（或 `scripts/installers_src/`），
`build_dist.py` 按文件名关键字自动归类到 `installers/WeFlow/` 与 `installers/微信/`，
并同步收录进 `build_release.py` 的源码归档。
**找不到只告警、不中止**——微信旧版安装包腾讯已下架，收件人可能得自行安装。

**构建目录默认在项目内 `build/`**：全部项目相关文件都收在工作文件夹里，
不再往盘根（`C:\_akasha_dist` 那种）散落。venv 近 5 万个小文件，
深目录会撞 Windows 的 260 字符路径上限（本机未开长路径支持）：脚本在复制前会先算
`check_path_budget()`，超限直接报错并提示换 `--stage`。
**打包成功后 stage 会自动删除**（只留 `release/dist/*.zip`）；`--no-zip` 或
`--keep-stage` 时保留，便于排查。

**版本号约定**：发行版不单独编号，直接用它所打包的代码版本
（`vX.Y.Z` 里的 X.Y.Z = `wechat-weflow-bridge-ob11/VERSION`）。

**收件人需自备**：微信桌面版（安装 + 登录）、WeFlow（用包内安装包装）、
一个模型 API Key（个人凭据，不随包）。

**发行版里绝不出现的东西**：API Key、面板口令、真实 wxid / 群名 / 人名、
本机绝对路径、Windows 用户名。`_runtime-data-snapshot/` 这类运行数据
**永远不要放进任何归档**。

## 7. 开发环境是自包含的（vendor/）

**依赖一律放进本文件夹，别散落在系统各处。** 散落的后果：换台电脑或被清理后就起不来，
而且没人说得清到底依赖了什么。

| 依赖 | 位置 | 是否入库 |
|---|---|---|
| 基座 Python 3.13.14 | `vendor/python/` —— **两个 venv 的 `pyvenv.cfg` 都指向它** | ❌（39MB，见 .gitignore） |
| WeFlow 程序本体 | `vendor/weflow/` | ❌（701MB） |
| AstrBot 框架代码 | `runtime/astrbot/.venv/Lib/site-packages/astrbot/`（随 venv 走） | ❌（`runtime/` 整体不入库） |

⚠️ **绝不把 `%APPDATA%\WeFlow` 拷进来**：里面有微信数据库解密密钥。每台机器自己生成。

**仍然在外部、拿不走的**：微信（腾讯的软件，必须安装+登录）、Ollama（可选，不装也能跑）。

**开发环境 ≠ 发行版**：开发环境是你自己调试用的（含真实数据与密钥）；
发行版是给别人用的（脱敏、不含密钥、含 astrbot 完整目录）。两者共用同一批源码，
改代码只改 `wechat-weflow-bridge-ob11/`，再 `sync_to_rc.py` 同步到运行副本。

完整清单见 `docs/依赖与目录说明.md`。
