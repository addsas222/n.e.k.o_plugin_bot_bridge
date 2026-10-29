# bot_bridge — 机器人插件兼容桥

把 **MaiBot / NoneBot2 / AstrBot / HoshinoBot / discord.py cogs / Koishi** 这些机器人框架的插件**原样装进 N.E.K.O.**。
它是一个 N.E.K.O. **Adapter**（`plugin.toml` 里 `type = "adapter"`），跑在自己的插件进程里：

```
外来插件目录 (MaiBot / NoneBot2 / AstrBot / HoshinoBot / discord.py / Koishi)
        │  ① 发现 + 加载（每个框架一个兼容层）
        ▼
   bot_bridge  ──② 投影──▶  N.E.K.O. 插件入口 + 对话期工具（LLM 可调用）
        ▲
        │  ③ 能力回落：send / llm / db / config / api / message …
        └────────────  N.E.K.O. 宿主能力（push_message / 模型网关 / 总线 / 存储）
```

一句话：**外面的插件能用猫娘的身体说话、能借猫娘的大脑思考。**

---

## 1. 支持范围（诚实版）

| 框架 | 兼容层 | 覆盖的插件面 |
| --- | --- | --- |
| **MaiBot** | `bridge/backends/maibot.py` + 官方 SDK 2.19.0（`vendor/maibot_sdk/`） | `MaiBotPlugin` 的 SDK v2 插件（`@Command` / `@Tool` / `@Action` / `@API` / `@EventHandler`）+ 旧式 `src.plugin_system` 兼容插件（走 `maibot_sdk.compat`）+ 能力代理（send / llm / db / config / api / component / chat / message / tool …） |
| **NoneBot2** | `bridge/backends/nonebot.py` + `shims/nonebot/` | 事件响应器模型：`on_command` / `on_message` / `on_keyword` / `on_regex` / `on_startswith` / `on_endswith` / `on_fullmatch` / `on_shell_command`（+ `ArgumentParser` / `shell_command` / `ShellCommandArgs`）、`Matcher.handle()/got()/finish()/reject()/pause()`、`CommandArg` / `RegexGroup` / `EventMessage` 依赖注入、`Rule` / `Permission`、`get_driver().type`（驱动分支的插件据此走「驱动不支持」分支而不是崩）。**OneBot v11 与 v12 并存**：加载时按插件源码里 import 的适配器（`adapters.onebot.v11` / `.v12`）分流，v12 插件拿到的是 v12 事件（`detail_type` / `get_session_id()` / `mention` 段语义），v11 插件行为不变 |
| **AstrBot** | `bridge/backends/astrbot.py` + `shims/astrbot/` | `Star` 插件、`@filter.command` / `regex` / `event_message_type` / `permission_type` / `llm_tool`、`AstrMessageEvent`、`MessageEventResult`、`astrbot.api.message_components`、`Context` 的常用方法；**`from astrbot.api.all import *` 可用**（插件模板首行），`astrbot.core.provider.entities` / `astrbot.api.platform` / `astrbot.api.util` 可导入，`astrbot.api.web` 导入不失败但调用显式抛能力缺失 |

### 边界：外来插件能 import 什么

桥接层提供的是**插件 API 子集**，不是任意依赖管理器。外来插件只能 import：

1. N.E.K.O. 运行时自带的包（`pydantic` / `httpx` / `aiohttp` / `websockets` / `yaml` / `openai` / …）；
2. 本桥的 shim（`nonebot*` / `astrbot*`）与 vendored 的 `maibot_sdk`；
3. 插件目录自己的模块。

所以「顺手依赖了 `nonebot_plugin_xxx`、某个没被 shim 的 `astrbot.*` 子模块、或 MaiBot 已删除的宿主模块」的插件会在 import 阶段失败。失败是**可见**的：该插件被跳过、`continue_on_error` 让其余插件继续加载、原因出现在 `bridge_status.errors` 与注册表日志里。补救办法二选一：

* 把缺的包放进该插件目录并让它 import（依赖随插件走）；
* 或把插件目录名加进 `[bridge].disabled_plugins` 先停用。

### 其它机器人框架

| 框架 | 状态 |
| --- | --- |
| MaiBot（SDK v2 + 旧式） | ✅ 已交付 |
| NoneBot2 系（含 LittlePaimon、Zhenxun 等基于 NoneBot2 的插件） | ✅ 已交付 |
| AstrBot | ✅ 已交付 |
| HoshinoBot | ✅ 已交付（独立 backend + `hoshino`/`aiocqhttp` shim；`Service` 的 9 个触发器全有，含 `on_natural_language`；`util` 是与上游同形的**包**，支持 `util.date_name`/`month_name`/`time_name`/`concat_pic`/`randomizer` 与 `hoshino.util.textfilter.filter`） |
| discord.py cogs | ✅ 已交付（独立 backend + `discord`/`discord.ext.commands` shim；`setup(bot)` + `Cog` + `@commands.command`；**`discord.ui`（View/Button/Select/Modal/TextInput）与 `discord.ext.tasks`（`@tasks.loop`）已补齐**，`HelpCommand` / `FlagConverter` 可用；但组件回调通道不存在 —— `View.wait()` 与 `interaction.response.*` 显式抛能力缺失） |
| **Koishi** | ✅ 已交付（独立 backend + `koishi`/`schemastery` 桩，Node 子进程宿主；覆盖 `ctx.command().action()` / `session.send` / logger / ready-dispose 子集——依赖 koishi 内部服务（database/console/model）的插件会拿到清晰错误。市场源：npm 镜像的 Koishi 插件索引 JSON（相关度前 250）） |
| LangBot / ZeroBot | ❌ 自有插件 API / Go，需新 backend |

新增一个框架 = 加 `bridge/backends/<name>.py`（实现 `detect/load/start/stop`，可选 `shims/<name>/`），
再在 `bridge/backends/__init__.py` 的 `BACKEND_MODULES` 里登记；路由核心不用动。
`requires` 里声明的运行时依赖缺失时，该兼容层会被停用并把原因写进 `backend_errors`。

> 各框架的优先级数值约定并不通用（NoneBot 数值越小越先跑，MaiBot 权重越大越先跑），
> 所以跨框架排序只保证「精确命令优先」，其余按插件键稳定排序；要确定归属请用
> `bridge_run_command` 的 `plugin` 参数点名。

**兼容层是「子集 + 显式失败」**：每个 shim 的模块文档里写清了哪些 API 没实现；
外来插件调用未映射的能力时，返回的是结构化失败
（`{"success": false, "code": "unsupported_capability", "error": "..."}`），
**不会**假装成功。清单见各 backend 的 `SUPPORTED_CAPABILITIES` / 文档字符串。

**验证状态**（本机实测，不是纸面声明）：

| 验证 | 结果 |
| --- | --- |
| 离线套件 `pytest tests -q` | **390 项通过**（2026-10-29 在 Python 3.11.16 上复跑，与 CI 的 `python-version: "3.11"` 一致：`390 passed`，含 OneBot v12 分流 6 项、适配器覆盖项 5 项、打包完整性的「所有 .py 可编译」）。跑法见 §6，需要 `NEKO_SOURCE_ROOT` 指向 N.E.K.O. 源码树（会自动探测 `%LOCALAPPDATA%\N.E.K.O\plugins\_host_sdk` 等常见布局，找不到时报一段能照做的错误） |
| 端到端脚本 `tests/stub_run.py` | **39/39**（同日复跑：夹具插件全部加载、命令命中、能力回落、重载、单个插件启停、删除临时拷贝） |
| 真机 `tests/live_check.py` | **17/17**（宿主里同时加载 5 个框架的插件，命令执行、点名、`/runs`+`/export`、重载、市场搜索）。证据链：插件侧 `send_chat … submitted=True`（**硬证据**）→ 插件服务器 `proactive bridge forwarded: plugin=bot_bridge` 与 `main_logic.proactive_delivery` 的 `release batch`（这两条**只作参考**：是否及时出现取决于宿主会话/用户是否活跃 —— `user active before claim` 时投放会一直被推迟，实测正常情况秒级~十几秒；连发多条会被 cross_server 合并，见「推送投递链路」）。教训：日志按日期分文件，`live_check` 对轮转后新出现的文件加了本轮时间戳门槛（否则会像本仓库作者一样把「只 grep 了旧文件」误判成「转发停了」）。|
| 市场搜索（真机 + 真网络） | 三个源全部健康：maibot 366 条 / nonebot 941 条 / astrbot 1329 条；关键词命中正常（`禁言`→3、`weather`→2、`天气`→2） |
| 冷缓存探活（真机） | 删掉 `data/market/cache` 后逐源探活 astrbot：**3.5s / 1329 条**，缓存重建 ✓（网络慢时可能几十秒；三源合并会超面板 30s 客户端上限，所以逐源）|
| 跨源搜索公平性（真机 + 单测） | 修掉「排在最前的源（maibot）吃光 limit」：`limit=20` 现在按源轮流取（真机 7/7/6），结果里同时出现 maibot / nonebot / astrbot；载荷带 `source_counts`（每源命中数）与 `source_failures`（失败源），面板显示「各源命中」 |
| 市场安装·zip（真机） | `nonebot_plugin_weather_lite` → GitHub zip → 装完**出现在插件列表**（`loaded=true`），验证后删除 |
| 测试隔离（回归） | 离线套件/夹具跑完**不再往真机插件目录写状态目录**：宿主存储根用 `NEKO_STORAGE_SELECTED_ROOT` / `data_root` 指到临时目录，跑完 `data/` 只剩合法项（`hello_astrbot`、`hello_neko`、`market`、`plugins`） |
| 市场安装·PyPI wheel（真机） | `nonebot_plugin_docs` → `files.pythonhosted.org` 真 wheel → `requires=['nonebot2']`（读自 PyPI 元数据）→ `loaded=true`；修掉 shim 缺的 `Driver.type` 后才成功 |
| 市场安装（真机，旧记录） | 真装 `astrbot-plugin-morning-news`：下载 GitHub zip、解出 15 个文件、写入 `.bot_bridge_market.json` 溯源；因插件自带 `feedparser` 缺失 → **如实报告** `loaded=false` + 缺失模块名（验证后已删除，环境复原） |
| 宿主消息面 | `bridge_status.recent_pushes` 读到 `MESSAGE_PUSH` 记录（`source=maibot:hello_neko` / `nonebot:echo_plugin`） |
| 宿主转发面 | 宿主日志（仅本轮新增行）`proactive bridge forwarded: plugin=bot_bridge` + `proactive_delivery … release batch` |
| 宿主接受度 | `POST /plugins/refresh` 认出 adapter；`/api/tools` 里出现 `source=plugin:bot_bridge` 的对话期工具 |
| 插件界面（真机） | `/surfaces` 列出 panel(hosted-tsx)+guide(markdown)；`/hosted-ui/source` 返回面板源码；`/hosted-ui/context` 返回状态（3 插件 / 9 工具 / 4 入口 / 市场源 366·941·1329 / 最近推送）与 4 个授权动作 |
| 面板动作（真机） | `bridge_reload` / 逐源探活 `bridge_market_sources{source=maibot}` / `bridge_market_search` 全部 200 真跑通；未声明的入口被宿主 403 拦下（`bridge_status` 预期如此），面板会把原因显示出来 |
| 入口授权（真机） | `[[plugin.entries]]` 声明后预览含全部 8 个入口、动作 403→200（**无需重启 N.E.K.O.**；`POST /plugins/refresh` 或单插件重载即可） |
| 新框架（真机） | 一次加载 **5 个框架**：maibot / nonebot / astrbot / hoshino / discord（插件 5 个，后端错误 0）；`/hi`（hoshino）→「你好，朋友！」，`!echo 你好世界`（discord）→「复读：你好世界」，都经 bridge 推进 N.E.K.O. 对话 ✓ |
| 启停（真机，**跨进程**） | 停止 `nonebot:echo_plugin` → `POST /plugin/bot_bridge/reload`（重启插件进程）→ 它**没有**被拉起来、`disabled_plugins=['echo_plugin']`、面板 `stopped_plugins` 里有 `nonebot:echo_plugin`（可点启动）；用 key 启用 → 回到 5 个插件、状态文件回到 `[]` ✓ |
| 启停状态怎么存（真机证据） | 宿主日志里 **0 条** `Updated config for plugin bot_bridge` —— 配置写回根本没走完（插件侧看到 `Config persistence response timed out: final persistence status is unknown`）。所以：**`<插件数据目录>/disabled_plugins.json` 是唯一权威**（同步原子写），宿主配置只在后台镜像（5s 超时、失败只告警）。配置是**单向镜像**：状态文件存在时改 `[bridge].disabled_plugins` 不能解除禁用（要解除请用面板或 `bridge_set_plugin`），只有状态文件不存在（首次运行/迁移）才采纳配置值 |
| 启停延迟（真机） | 停用 **1.06s** / 启用 3.70s（后者含真正加载插件）；配置写回卡住时入口也不等它（单测注入 30s 的慢写回，断言入口 < 1s 返回）|
| 删除（真机） | 造一个临时插件 `nonebot:dummy_delete_me`，`bridge_delete_plugin` → 文件真被删除、插件表回落 ✓；越界路径/插件根目录本身在单测里被拒绝 ✓ |
| 桥自己的超时先于宿主（真机） | 35s 夹具命令经 `bridge_run_command`：修 clamp 之前宿主砍在 30s（`Entry 'bridge_run_command' timed out after 30.0s`，run `failed`）；clamp 之后是桥自己 25s 先超时 —— run **`succeeded`**（25.3s），payload `handled=false`、`error="bot plugin call 'nonebot:slow_probe_plugin' timed out after 25.0s"`，宿主那 30s 根本没轮到 |
| 停用后投影面 | 单测钉住：停用后该插件的 `bot_cmd_*` 入口与 `bot_*` 对话期工具从 router 里消失（停/启各验一次），不会留着让模型还能调 |
| 入口超时（真机实测） | 装一个 sleep 35s 的命令经 `bridge_run_command` 调用 → **30.3s `status=failed`**（探针插件已删）；宿主 `/plugins` 的生效快照 `entries` 里只有市场三条是 `timeout=300`，其余为 None→30s。据此把 `call_timeout` 收敛到 **25s**（先于宿主超时返回结构化错误）|
| 插件日志 | `logs/plugin/N.E.K.O_Plugin_bot_bridge_*.log` 中每次外来推送一条 `send_chat … submitted=True/False` |

---

## 2. 安装

1. 把本目录放到 N.E.K.O. 的用户插件目录：

   ```
   %LOCALAPPDATA%\N.E.K.O\plugins\bot_bridge\
   ```

2. 让宿主发现它（任一）：
   - 打开 N.E.K.O. 插件管理器刷新；
   - 或 `curl -X POST http://127.0.0.1:48916/plugins/refresh`；
   - 或重启 N.E.K.O.。
3. 启动插件（插件管理器里打开，或 `curl -X POST http://127.0.0.1:48916/plugin/bot_bridge/start`）。
4. 把外来插件丢进插件根目录（默认 `<插件数据目录>/plugins/`，见下）。

### 外来插件放哪

默认为：

```
%LOCALAPPDATA%\N.E.K.O\plugins\bot_bridge\data\plugins\
├── <插件目录>            # 直接放（扁平布局）
└── maibot/<插件目录>     # 或按框架分子目录（两种都认）
```

也可以在配置里改成任意路径：

```toml
[bridge]
plugins_root = "D:/bots/plugins"
```

每个框架还可以**单独指定插件源目录**（`[bridge.framework_dirs]`），适合"插件已经在别处"
的场景 —— 比如让 Koishi 兼容层直接读现成 Koishi 实例的插件目录、NoneBot 读已有的
插件目录，不用把文件搬进 `plugins_root`：

```toml
[bridge.framework_dirs]
koishi = "D:/bots/koishi/plugins"   # 只影响 koishi 框架从哪里读插件
nonebot = "D:/bots/nonebot/plugins"
```

规则：

* 框架源目录**只影响"从哪里读"**：目录不存在只告警跳过，不会代建（多半是路径写错）；
* 同一插件同时出现在框架源目录和 `plugins_root` 时，**框架源目录优先**；
* 写错/未启用的框架名会被忽略并告警；
* 从框架源目录加载的插件**不受 `bridge_delete_plugin` 删除授权**（删除只允许
  `plugins_root` 里面的东西 —— 外部目录可能是真实的机器人安装）；
* koishi 例外说明：兼容层要把 `koishi-host.mjs` / 协议桩写进插件目录的祖先链
  （Node 裸导入解析靠祖先链）。框架源目录指向外部目录时，桥只补缺失文件，
  **绝不覆写**已有的 `koishi-host.mjs` / `node_modules`，不会动你现有实例的依赖。

支持单文件插件（NoneBot2 常见的 `xxx.py`）与目录插件。

---

## 2.5 安全校验（加载前静态扫描）

第三方插件是**直接 import 进本进程**（MaiBot / NoneBot2 / AstrBot / Hoshino / discord.py）
或**起 Node 子进程**（Koishi）的，`framework_dirs` 又允许从任意目录读插件 —— 所以每个
插件在加载前都会做一遍只读静态扫描（`bridge/security.py`）：

| 级别 | 命中模式 | 默认行为 |
| --- | --- | --- |
| `dangerous` | 动态执行（`eval` / `exec` / `compile` / `__import__` / JS `eval` / `new Function`）、进程创建（`subprocess` / `os.system` / `child_process`）、危险反序列化（`pickle.loads` / `marshal.loads`）、`ctypes` 原生调用 | `report` 模式亮灯照常加载；`enforce` 模式**拒绝加载**（进 `errors`，`security_reports` 里留全文） |
| `warning` | 递归删除目录（`shutil.rmtree` / `fs.rmSync(..., {recursive:true})`）、同行带 `..` 的按路径删除 | 只记录，不拦 |

扫描结果落到三处：插件日志（`source=security`）、`bridge_status` 的每个插件
`security` 摘要与顶层 `security_reports`、面板插件行的风险标记。

如实说边界：这是**启发式 tripwire，不是沙箱** —— 扫不出混淆、编码、字符串拼接出来
的恶意调用，运行时隔离仍靠宿主进程边界。它拦的是「一眼危险的第三方代码」和
「不知不觉装进来的 stole-credentials 套壳」这类肉眼可见的模式。

```toml
[bridge.security_scan]
enabled = true
mode = "report"   # enforce = 发现 dangerous 命中就拒绝加载
```

---

## 3. 配置

首次运行会把 `config.example.toml` 的内容复制到
`%LOCALAPPDATA%\N.E.K.O\plugins\bot_bridge\config\plugin.toml`，改那一份才生效。

关键项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `plugins_root` | 空 | 外来插件根目录；空 = 数据目录下的 `plugins/` |
| `framework_dirs` | 空 | 各框架单独的插件源目录（`[bridge.framework_dirs]`，如 `koishi = "D:/bots/koishi/plugins"`）；目录不存在告警跳过不代建；与 `plugins_root` 同名时框架目录优先；外部目录的插件不可经 `bridge_delete_plugin` 删除 |
| `security_scan` | `enabled=true, mode="report"` | 加载前静态安全扫描：`report` = 日志/面板亮灯照常加载；`enforce` = 高风险命中（动态执行/进程创建/危险反序列化/ctypes）拒绝加载 |
| `enabled_frameworks` | 空 | 只启用某些兼容层（`maibot` / `nonebot` / `astrbot` / `hoshino` / `discord`）；空 = 全部 |
| `nonebot_adapter` | `auto` | NoneBot2 的 OneBot 适配器版本：`auto` = 按插件源码扫描（只 import `onebot.v12` → v12，其余 → v11）；`v11` / `v12` = **强制**所有 NoneBot 插件用该版本。强制会把另一种协议插件的 `isinstance` 判断全部打空、命令不再命中 —— 只在「双协议插件要固定走某分支」时用。也认 `onebot.v11` / `~onebot.v12` 写法；认不出的值回退到 `auto` 并记 warning。生效版本与来源（`scan` / `config`）可在插件 `native.adapter` / `native.adapter_source` 里查到 |
| `disabled_plugins` | 空 | 按插件目录名/插件 id 单独停用 |
| `expose_commands_as_tools` | true | 把外来命令投影成对话期工具（猫娘可直接调用） |
| `expose_plugins_tools` | true | 把外来工具投影成对话期工具 |
| `max_exposed_tools` | 40 | 工具投影上限，防止灌爆模型工具表 |
| `default_visibility` / `default_ai_behavior` | `["chat"]` / `respond` | 外来插件往对话推送时的默认语义 |
| `max_push_chars` | 4000 | 单条推送文本长度上限 |
| `call_timeout` | 25 | 单次外来命令/工具调用超时（秒）。**小于宿主入口预算**（实测本插件除市场三条外都是 30s），保证超时是本插件返回的结构化错误，而不是宿主砍成「跑失败」 |
| `poll_conversations` | false | 可选：轮询总线上 N.E.K.O. 的对话轮次，喂给外来插件（best-effort） |

### 模型网关绑定（`llm.*` 能力的前提）

`plugin.toml` 里声明了两个模型用途：

```toml
[plugin.models.chat]        # llm.generate / generate_with_tools
[plugin.models.embedding]   # llm.embed
```

宿主需要把模型槽**绑定**到它们，否则模型网关对插件请求返回
`403 model_usage_not_bound`，桥接层会把该错误原样回给外来插件
（`{"success": false, "code": "model_usage_not_bound", ...}`）。
绑定入口：`PUT /api/models/plugins/{plugin_id}/bindings/{usage_id}`。

---

## 4. 在 N.E.K.O. 里怎么用

### 对话期工具（猫娘自己会调）

| 工具 | 作用 |
| --- | --- |
| `bridge_list_plugins` | 列出外来插件及其命令/工具/事件 |
| `bridge_run_command` | 把一条消息交给外来插件匹配执行（命中命令就执行并回话） |
| `bridge_call_plugin_tool` | 直接调用某个外来插件的工具/命令 |

外加：每个外来命令/工具都会投影成 `bot_<框架>_<插件>_<名字>` 形式的工具。

### 插件入口（插件管理器里手动触发 / 被其它插件调用）

`bridge_status`、`bridge_list_plugins`、`bridge_run_command`、
`bridge_call_api`、`bridge_set_plugin`、`bridge_delete_plugin`、`bridge_reload`，
以及每个外来命令对应的 `bot_cmd_<框架>_<插件>_<命令>`。

调用示例：

```bash
curl -X POST http://127.0.0.1:48916/runs \
  -H "Content-Type: application/json" \
  -d '{"plugin_id":"bot_bridge","entry_id":"bridge_run_command","args":{"text":"/天气 北京"}}'
```

### 启停与删除（单个插件）

| 入口 | 作用 | 说明 |
| --- | --- | --- |
| `bridge_set_plugin` | 启动 / 停止**一个**插件 | `args={"plugin_id": "框架:插件" 或原生 id 或唯一后缀, "enabled": true/false}`。只动这一个插件（不重启整座桥），随后重投影工具/入口。停用会记进 `<插件数据目录>/disabled_plugins.json`（权威，宿主配置只是后台镜像），**重启后不会自动拉起**；再启动会自动把它从禁用列表里去掉 |
| `bridge_delete_plugin` | 删除一个插件 | 先停用，再删文件（**只允许删插件根目录里的东西**，越界/根目录本身直接拒绝）。`purge_state=true` 时连带删掉它的数据/缓存目录；默认保留数据 |

```bash
# 停止一个插件（不会删文件）
curl -X POST http://127.0.0.1:48916/runs -H "Content-Type: application/json" \
  -d '{"plugin_id":"bot_bridge","entry_id":"bridge_set_plugin","args":{"plugin_id":"nonebot:echo_plugin","enabled":false}}'

# 删除它（插件文件真实删除；数据目录保留）
curl -X POST http://127.0.0.1:48916/runs -H "Content-Type: application/json" \
  -d '{"plugin_id":"bot_bridge","entry_id":"bridge_delete_plugin","args":{"plugin_id":"nonebot:echo_plugin"}}'
```

面板里是「选中一行 → 启动 / 停止 / 删除」三个按钮：启动/停止走 `props.api.call`（带显式超时），
删除走宿主确认框（`@ui.action(confirm=...)`），确认后才会真删。
对话期模型能调 `bridge_set_plugin`（启停可逆），**删除只留人工入口**。

### 启停与删除（单个插件）

> 启停状态存哪：`<插件数据目录>/disabled_plugins.json`（权威，原子写）——
> 宿主配置 `[bridge].disabled_plugins` 只是**后台镜像**，因为真机上宿主配置写回会超时
> （日志里连 `Updated config for plugin bot_bridge` 都没打出来）。镜像失败不影响启停，只告警。

### 推送投递链路（实测）

外来插件调 `send.text` / `push_message` 之后，消息经过：

1. **插件侧**：`send_chat <- … submitted=True`（宿主 API 接受）；
2. **插件服务器**：`proactive_bridge` 转发 → `proactive bridge forwarded: plugin=bot_bridge`；
3. **主服务**：`[EventBus] proactive_message submitting proactive callback to delivery manager`；
4. **投放**：`trigger_agent_callbacks` → `send_lanlan_response` → `turn_end … agent_callback_turn=True`
   （这一轮真的进了对话上下文）；桥的 `bridge_status.recent_pushes` 也能读到。

实测（16:27–16:29 两轮）：

| 时间 | 事件 |
|---|---|
| 16:27:27–28 | 六条推送被提交，`pending=6` |
| 16:27:42 | `[proactive:YUI] release batch n=5 keys=['__uniq:6'…'__uniq:14']` —— **批量放出** |
| 16:29:21 / 16:29:25 | 单条推送 → `release batch n=1` → `send_lanlan_response len=13` → `agent_callback_turn=True` |

**是排队 + 批量投放，不是丢弃**：`release batch` 在**秒级~十几秒**内出现（这一行落在
`N.E.K.O_Plugin_*.log`，不在 Main 日志里 —— 找错文件会误判成「没投递」）。
连发多条时，`cross_server` 会把**连续未同步**的主动消息合并：
`[cleanup] 精简了 N 条未同步的连续主动搭话消息，仅保留最后一条` —— 所以「发 6 条只看到 1–2 条」是合并行为。
（`AI 正在响应或已有一轮在跑（409）`、`user active before claim`、BM25 drop 属于**角色自身主动搭话调度**
`proactive_chat.service` 的日志，与本链路无关，别拿来解释推送。）

### 入站通道说明（重要）

宿主**不会**把用户消息直接推给适配器（N.E.K.O. 仓库里
`adapter_startup` / `handle_adapter_message` 没有任何调用点），所以入站有两条明确路径：

1. **显式入口 / 对话期工具**（稳定）：`bridge_run_command`。NEKO 侧的 LLM 或别的插件
   把用户消息交给它即可。它还可以点名插件：
   `{"text": "/echo hi", "plugin": "nonebot:echo_plugin"}`。
2. **总线轮询**（可选，默认关闭）：`poll_conversations = true` 时，桥接层周期性读
   `ctx.bus.conversations`，把能识别为「用户发言」的轮次（`poll_user_only`）喂给外来插件。
   这依赖宿主往 `conversations` 存储写轮次，属于 best-effort。

### 消息交给哪个插件（顺序规则）

不点名时按这个顺序找**第一个命中**的插件：

1. 有明确触发模式的命令多的插件优先（正则 / 前缀 / 关键词）；
2. 其次比命令优先级；
3. 最后按插件键字典序。

也就是说「精确命令」优先于「什么都收的消息监听器」，不会因为哪个框架先加载就被抢走。
需要确定归属时直接点名（`plugin` 参数，或用 `bridge_call_plugin_tool`）。

跨框架互调：外来插件的 `ctx.api.call` 先在本桥已加载的同类插件里解析，找不到再落到
`plugins.call_entry` 去调 N.E.K.O. 插件（此时 API 名写成 `<插件id>:<入口id>`，或用最后一个 `.` 分隔）。

---

### 插件管理器界面（Hosted TSX 面板）

`plugin.toml` 里声明了 `[[plugin.ui.panel]]`（`ui/panel.tsx`，`kind=panel` → hosted-tsx）
与一个 Markdown 教程页（`docs/quickstart.md`）。面板四个页签：

| 页签 | 内容 |
| --- | --- |
| 插件 | 两张表：**已加载**（框架 / id / 名称 / 命令 / 工具 / 事件 / running，选中一行后可 停止 / 删除）+ **已停用 / 没跑起来**（同一个「启动 / 删除」按钮，加载失败的原因显示在行里）。调用一律传 `框架:插件` 键，跨框架同名 id 不会卡住 |
| 兼容层 | 各框架兼容层与它们的运行时依赖；后端错误（例如缺 `yaml`） |
| 市场 | 市场源状态（条目数、缓存年龄）+ **逐源探活**按钮 → 关键词搜索 → 选中一行 → 安装（确认后写入并热重载） |
| 推送 | 最近进入 N.E.K.O. 对话的推送（读宿主 `messages` 存储） |

后端契约：`@ui.context(id="dashboard")` 提供 `props.state`（**不联网**，所以面板秒开）；
只有带 `@ui.action` 的入口能在面板里调用 —— 面板拿不到的动作会被宿主 403 拦掉。

```bash
# 面板源码 / 上下文 / 动作（surface 选择器走查询参数或 body）
curl "http://127.0.0.1:48916/plugin/bot_bridge/hosted-ui/source?kind=panel&id=dashboard"
curl "http://127.0.0.1:48916/plugin/bot_bridge/hosted-ui/context?kind=panel&id=dashboard"
curl -X POST "http://127.0.0.1:48916/plugin/bot_bridge/hosted-ui/action/bridge_reload" \
     -H "Content-Type: application/json" \
     -d '{"args":{},"kind":"panel","surface_id":"dashboard"}'
```

**动作授权怎么来的**：宿主只允许面板调用「它知道的入口」。它知道的入口来自
*入口预览*（`plugin/core/registry.py::_effective_entries`）：装饰器扫描 **∪** 清单声明。
热改插件后的重载：全局 `POST /plugins/reload` 会重启所有插件（真机 86s），
只想让本插件重读代码/清单用 `POST /plugin/bot_bridge/reload`（真机 ~4s）。

本插件的三个市场入口在 `plugin.toml` 里用 `[[plugin.entries]]` 又声明了一遍，
所以即使宿主对插件模块的扫描缓存过期，授权依然成立（实测：`/plugins/refresh` 之后
预览包含全部 8 个入口，面板动作从 403 变 200，**不需要重启 N.E.K.O.**）。

`[[plugin.entries]]` 里同时声明了 `timeout`。**生效预算按「运行中 handler 快照的 meta」解析**
（`plugin/server/runs/trigger_service.py::_execute_trigger` → `resolve_entry_timeout`），跟预览无关，
默认 `NEKO_PLUGIN_EXECUTION_TIMEOUT = 30s`。真机实测（本插件）：

| 入口 | 宿主快照里的 `timeout` | 实测结论 |
|---|---|---|
| `bridge_market_sources` / `_search` / `_install` | **300s** | 声明进了快照，三源探活 25.6s 能跑完（修之前默认 30s 时在 30.3s 被掐，只写了两个索引缓存）|
| `bridge_run_command` / `bridge_call_api` / `bridge_set_plugin` | **90s** | 2026-10-29 复查：这些已经**出现在快照里**（早期版本是 None → 30s，现在宿主的入口预览把清单 `timeout` 一并透出）|
| `bridge_delete_plugin` | **120s** | 同上 |
| `bridge_reload` | **180s** | 同上 |
| `bridge_status` / `bridge_list_plugins` | None → **30s** | 清单与装饰器都没写 timeout，按宿主的默认预算 |

> 上表是 2026-10-29 从 `GET /plugins` 的 `entries[].timeout` 直接读出来的（不是估计）。
> 早期版本「其余入口一律 None→30s」的结论已经过期；但 `[bridge].call_timeout = 25s`
> 的**设计依据仍然成立**：桥自己的超时必须先于宿主预算触发，才能返回结构化错误。
> 顺带修掉了 `bot_` 双前缀（工具名曾是 `bot_bot_<框架>_…`，与本文档约定不符且白吃
> 4 个字符预算，长名更早被截断）。

所以桥现在按「**默认 30s**」设计：`[bridge].call_timeout` 默认 **25s**，让超时先由本插件返回
结构化错误，而不是让宿主砍成一次「跑失败」；面板的慢动作（探活/搜索）一律按源拆开，
每次调用都落在 30s 内，并额外传客户端 `timeoutMs`。

> 打包元数据（`plugin.meta.json`）对本插件**不适用**：官方探针要求插件目录里没有
> 运行期状态目录，而本桥的插件根目录就在 `<插件目录>/data/plugins/` 下，探针会以
> "staged tree contains directories that cannot survive packaging" 拒绝生成；
> 而且它按源码树内容指纹校验，`data/**` 每次写入都会让它失效。上面的清单声明
> 是不依赖指纹的那条通路（宿主的 `_packaged_entries_preview` 也以清单为兜底）。

### 插件市场（浏览官方索引 + 一键安装）

> 市场源是四个官方索引：MaiBot / NoneBot2 / AstrBot / Koishi（Koishi 走 npm 镜像的
> 搜索 JSON，单页上限 250、按相关度排序）。HoshinoBot 与 discord.py cogs
> 没有集中索引，**不走市场**——把插件目录（或单文件）放进插件根目录即可，
> 面板的启停/删除对它们一样有效。


**跨源搜索是按源轮流取的**（`bridge/market.py:_interleave`）：修之前是「按 `_sources` 顺序
拼接后截断」，排在最前的源（默认 maibot）会占满 `limit=20` 的结果，后面两个框架在结果里
看起来像不存在（实测修复前 20 条全是 maibot；修复后 maibot 7 / nonebot 7 / astrbot 6）。
搜索返回里带有 `source_counts`（每源命中数，截断前）与 `source_failures`（哪个源挂了），
面板会把这两项显示出来 —— 结果里某个源一条都没有时，能一眼看出是「没命中」还是「源不可用」。

**三处超时要分清**：

| 超时 | 位置 | 默认 | 说明 |
|---|---|---|---|
| `[bridge.market].timeout` | 本插件内部 | 60s | 单次索引/下载请求上限。astrbot 索引实测 ~25s，30s 会贴边超时 |
| 入口 `timeout=` | `plugin.toml` / 装饰器 | 300s | 宿主给**单次入口调用**的预算（宿主默认只有 30s） |
| iframe 客户端上限 | 宿主面板运行时时 | 30s | `props.api.call` 的默认值；`ActionButton` 不透传 `timeoutMs`，所以本面板的探活/搜索用普通按钮 + 显式 `timeoutMs: 180000` |
| 宿主入口预算 | 宿主 | 30s（`NEKO_PLUGIN_EXECUTION_TIMEOUT`） | 单次入口调用的上限，按**运行中 handler 快照的 meta** 解析。真机实测：本插件只有市场三条在快照里是 300s，其余（含装饰器/清单写了 180/90 的）仍是 30s —— 35s 的夹具命令在 **30.3s** 被宿主砍成 `failed`。所以桥自己的 `call_timeout` 默认 **25s**（先超时、返回结构化错误），慢活按源拆开 |



桥接层内置一个市场：读官方插件索引 → 搜索 → 装进隔离的插件目录 → 热重载。

| 面 | 说明 |
| --- | --- |
| 入口 `bridge_market_sources` | 列出市场源状态。**默认不联网**（只报缓存/配置，毫秒级）；传 `refresh=true` 才真的拉索引探活（正常几秒到几十秒，受网络影响可能到分钟级；单次抓取受 `[bridge.market].timeout` 总预算约束）。面板上是**逐源按钮**：三个源一起拉会超过宿主 iframe 的 30s 单请求上限 |
| 入口 `bridge_market_search` | 在索引里搜索（只读） |
| 入口 `bridge_market_install` | 安装选中条目（`source` + `plugin_id`，可选 `overwrite`），装完自动 reload |
| 对话期工具 `bridge_market_search` | 只读搜索；**安装刻意不暴露给模型**，装代码必须是显式动作 |

配置（`[bridge.market]`）：

```toml
enabled = true              # 关掉则市场入口与工具都不可用（桥接层本身照常工作）
allow_install = true        # 关掉则安装入口直接拒绝，并说明怎么打开
sources = []                # 只启用这些源；空 = 全部（maibot / nonebot / astrbot；另两个框架没有官方索引）
mirror_prefixes = ["https://ghproxy.net/"]   # 索引/下载被墙或证书报错时依次尝试的前缀
cache_ttl_seconds = 600
timeout = 60.0
max_archive_bytes = 33554432
```

安装行为与安全边界：

* **索引来源**（均已实测可拉取，条目数为实测值）：

  | 源 | 索引 | 条目 | 安装方式 |
  | --- | --- | --- | --- |
  | `maibot` | `raw.githubusercontent.com/Mai-with-u/plugin-repo/main/plugin_details.json` | 366 | `manifest.urls.repository`（GitHub 仓库）→ zip |
  | `nonebot` | `registry.nonebot.dev/plugins.json` | 941 | 仓库名与插件名对得上 → GitHub zip；否则用官方 PyPI 包 → `pypi.org/pypi/<包>/json` 取 wheel（优先 `py3-none-any`）→ `files.pythonhosted.org` 下载 |
  | `astrbot` | `AstrBot_Plugins_Collection/plugin_cache_original.json`（AstrBot 自身 registry 回退地址） | 1329 | `repo` → zip |

  AstrBot 官方市场 `api.soulter.top/astrbot/plugins`（1329 条、带 `download_url`）也能解析，
  但主机不在默认白名单里，所以默认走 GitHub 镜像；要用官方市场就把
  `api.soulter.top` 加进 `[bridge.market].allow_hosts`。

* **只认索引里的条目**：`source` + `plugin_id` 必须先出现在索引里；未知 id 直接结构化失败（`code=not_in_index`）。
* **落盘布局按框架校验 + 自动摊平**：解压后必须在深度 1–2 找到该框架认的形态
  （MaiBot `_manifest.json`+`plugin.py` / AstrBot `metadata.yaml`+`main.py` / NoneBot 的模块目录），
  否则拒绝安装（`code=manifest_missing`）——装了却发现不了是最糟的结果。
  找到的那一层会被**提到深度 2**：wheel 的 `<模块名>/` 直接落到
  `<plugins_root>/nonebot/<模块名>/`（`*.dist-info`/`*.data` 丢弃），仓库把包目录裹一层的
  zip 同样只搬包目录。安装结果因此在 loader 眼里就是「一个普通插件目录」。
* **失败是结构化的**：`ok=false` + `code` ∈
  `market_disabled` / `install_disabled` / `plugins_root_unavailable` / `unknown_source` /
  `source_unavailable` / `not_in_index` / `unsupported_install_kind` / `unsupported_host` /
  `already_installed` / `target_occupied` / `download_failed` / `archive_invalid` /
  `path_traversal` / `too_many_files` / `extract_too_large` / `manifest_missing` / `install_failed`，
  文案只作补充，调用方按 `code` 分支即可。
* **依赖如实报**：安装结果带 `requires`（wheel 的 PyPI `requires_dist` 或插件目录里的
  `requirements.txt`），并写进溯源文件；缺依赖的插件会在加载阶段被跳过，原因出现在
  `bridge_status.errors` 与安装结果的 `load_error` 里。
* **只走 https + 主机白名单**（github.com / codeload.github.com / raw.githubusercontent.com /
  ghproxy.net / registry.nonebot.dev / pypi.org / cloud.astrbot.app …，可用 `allow_hosts` 扩展），
  下载有体积上限。
* **解压是保守的**：拒绝绝对路径、`..`、符号链接与逃出目标目录的成员，限制解压总量与文件数，
  跳过 `.git` / `__pycache__`；目标目录被限制在 `plugins_root/<框架>/<插件>` 内。
* 目标目录已存在且未 `overwrite=true` → 拒绝覆盖；覆盖只删那一个目录。
* 每次安装写一份 `.bot_bridge_market.json`（来源、id、版本、URL、压缩包 sha256、时间）作为溯源。
* 无仓库地址、只能从 PyPI 装的条目（部分 NoneBot 插件）返回**结构化失败**并说明原因 ——
  本桥不代管第三方依赖。

```bash
# 搜索
curl -X POST http://127.0.0.1:48916/api/llm-tools/callback/bot_bridge/bridge_market_search \
  -H "Content-Type: application/json" -d '{"name":"bridge_market_search","arguments":{"query":"禁言","source":"maibot"},"call_id":"m1"}'
# 安装（走入口，走的是 /runs 或插件管理器）
curl -X POST http://127.0.0.1:48916/plugins/refresh   # 让宿主先认到桥
curl -X POST http://127.0.0.1:48916/runs -H "Content-Type: application/json" \
  -d '{"plugin_id":"bot_bridge","entry_id":"bridge_market_install","args":{"source":"maibot","plugin_id":"SengokuCola.Mute-Plugin"}}'
```

---

## 5. 能力映射表（外来插件 → N.E.K.O.）

| 外来能力 | N.E.K.O. 侧实现 | 备注 |
| --- | --- | --- |
| 发消息（MaiBot `ctx.send.*`、AstrBot `event.send`、NoneBot `Matcher.send/finish`） | `ctx.push_message(parts=[{type:text}], visibility, ai_behavior)` | 文本/图片/混合，走宿主唯一的对话推送通道 |
| 大模型（`ctx.llm.generate` / `generate_with_tools` / `embed`、AstrBot `get_using_provider()`） | `ctx.models.get_client()` → 模型网关（OpenAI 兼容） | 需要宿主绑定模型槽 |
| 数据库（MaiBot `ctx.db.*`） | 每个外来插件独立 sqlite（`data/<插件>/botdata.sqlite3`） | 表结构由插件自己建 |
| 配置（`ctx.config.*`） | `data/<插件>/config.json`（可回退读 `config.toml`） | 桥接层负责读写 |
| 插件互调（`ctx.api.call`） | 先在自己加载的外来插件里查；否则 `plugins.call_entry` 调 N.E.K.O. 插件 | |
| 组件/插件查询 | `ctx.query_plugins` / 桥接注册表 | |
| 消息/聊天流查询 | `ctx.bus.messages` / `ctx.bus.conversations` | 只读快照 |
| 其它（emoji / render / statistics / maisaka / knowledge / gateway / frequency / person …） | **未映射** → 结构化失败 | 见各 backend 文档字符串 |

---

## 6. 开发与测试

离线测试用**真 SDK + 假宿主**（`tests/support.py`）：`plugin.sdk.*` 从 N.E.K.O. 源码树
导入，宿主 `ctx` 是假的（记录所有推送、可注入模型响应与总线记录）。

```bash
# 一次性准备（任意 venv，Python 3.11+）
python -m venv bridge-venv
bridge-venv\Scripts\python.exe -m pip install pydantic msgpack pytest pytest-asyncio pyyaml httpx aiohttp
#   pydantic  ← MaiBot SDK 需要
#   msgpack   ← N.E.K.O. SDK 需要
#   pyyaml    ← AstrBot 的 metadata.yaml 需要（宿主运行时自带）
#   httpx / aiohttp ← 生态插件（含市场装下来的）常用，宿主运行时自带，离线测试要补齐

# 单元 + 集成 + 打包测试（夹具插件覆盖五个框架，含旧版插件与新式 SDK 插件）
bridge-venv\Scripts\python.exe -m pytest tests -q

# 端到端脚本（逐项 PASS/FAIL，落 tests/stub-report.txt）
bridge-venv\Scripts\python.exe -X utf8 tests\stub_run.py

# 真机验证（宿主在跑；会把 tests/live/plugins/ 的示例插件铺进数据目录）
bridge-venv\Scripts\python.exe -X utf8 tests\live_check.py
```

`NEKO_SOURCE_ROOT` 环境变量可指向任意 N.E.K.O. 源码树（默认见 `tests/support.py`）。

真机调用示例（宿主在跑）：

```bash
curl -X POST http://127.0.0.1:48916/plugins/refresh
curl -X POST http://127.0.0.1:48916/plugin/bot_bridge/reload
curl -X POST http://127.0.0.1:48916/api/llm-tools/callback/bot_bridge/bridge_run_command \
     -H "Content-Type: application/json" \
     -d '{"name":"bridge_run_command","arguments":{"text":"/echo hi","plugin":"nonebot:echo_plugin"},"call_id":"t1"}'
```

验证证据留在四处：
* 宿主消息面：`bridge_status` 的 `recent_pushes`（读的是宿主总线 `messages` 存储里
  `push_message` 真正落下的 `MESSAGE_PUSH` 记录）；
* 宿主转发面：`%LOCALAPPDATA%\N.E.K.O\logs\N.E.K.O_Plugin_*.log` 里的
  `proactive bridge forwarded: plugin=bot_bridge` 与随后的
  `main_logic.proactive_delivery … release batch`（证明消息真的投给了角色）；
* 插件日志：`%LOCALAPPDATA%\N.E.K.O\logs\plugin\N.E.K.O_Plugin_bot_bridge_*.log` 里
  每次外来推送一条 `send_chat <- <插件>: N 字 … submitted=True/False`；
* `tests/stub-report.txt` / `tests/live-report.txt` 是脚本跑出来的逐项结果。

> 入口返回值不在 run 记录的 `result` 里；`GET /runs/{id}/export` 的
> `label == "trigger_response"` 条目才带结构化结果（外层是宿主信封，插件返回值在 `data`）。

---

## 7. 目录结构

```
bot_bridge/
├── plugin.toml              # 清单（type = adapter，entry，模型用途声明）
├── config.example.toml      # 运行配置模板
├── i18n/                    # zh-CN / en
├── __init__.py              # 适配器入口：生命周期、入口、入站轮询
├── bridge/
│   ├── contracts.py         # 冻结契约（HostServices / FrameworkBackend / …）
│   ├── errors.py            # 结构化错误
│   ├── host.py              # N.E.K.O. 能力实现（push_message / 模型 / 存储 / 总线）
│   ├── registry.py          # 发现、加载、启停、状态
│   ├── plugin_admin.py      # 单个插件的启停 / 删除（只允许删插件目录内的路径）
│   ├── router.py            # 投影成入口与对话期工具 + 消息分发
│   ├── market.py            # 插件市场：官方索引 → 搜索 → 安装（带来源/安全边界）
│   └── backends/            # 每个框架一个兼容层（自动发现：放 <name>.py 即可）
├── ui/panel.tsx             # 插件管理器面板（Hosted TSX）
├── docs/quickstart.md       # 面板里的教程页（Markdown）
├── shims/                   # 无第三方依赖的框架 API 子集（nonebot / astrbot / hoshino / discordpy）
├── vendor/maibot_sdk/       # 官方 MaiBot 插件 SDK（未改动，见其 LICENSE 与 README.vendor.md）
└── tests/
    ├── conftest.py          # 真 SDK + 插件包的导入引导
    ├── support.py           # 假宿主（记录推送 / 注入模型 / 注入总线）
    ├── fixtures/            # 五个框架的夹具插件（含旧版路径与负样本）
    ├── live/plugins/        # 真机用的示例插件（会被铺进数据目录）
    ├── stub_run.py          # 离线端到端脚本
    ├── live_check.py        # 真机端到端脚本
    ├── test_backend_*.py    # 各兼容层单测
    ├── test_smoke.py        # 桥接层冒烟
    └── test_packaging.py    # 打包完整性（复刻宿主校验器规则）
```

## 8. 许可与出处

* `vendor/maibot_sdk/` 是上游 `Mai-with-u/maibot-plugin-sdk`（**v2.19.0** tag
  `c2a039a`，**LGPL-3.0-only**）的**未改动**副本，仅作为导入回退；用户环境自行安装的
  `maibot_sdk` 优先。逐文件与 upstream tag 一致（差异仅 `LICENSE` 与本目录的说明文件）。
  由 2.8.2 升到 2.19.0 时做过 AST 符号比对：共有 66 个模块**零移除**，只新增了
  `capabilities/{web,webui,files,market,internal_chat,desktop_pet}` 与 `config_widgets`。
  详见 `vendor/maibot_sdk/README.vendor.md`。
* `shims/` 下的 NoneBot2 / AstrBot 兼容层是**独立重写**的 API 子集，不包含上游代码。
