# 市场测试夹具（真实索引切片）

这些 JSON 是**真实官方索引的小切片**（各 3 条），只用于离线测试 `bridge/market.py`
的解析与安装编排；测试不会联网。

| 文件 | 来源 URL | 抓取日期 | 说明 |
| --- | --- | --- | --- |
| `maibot_plugin_details.json` | `https://raw.githubusercontent.com/Mai-with-u/plugin-repo/main/plugin_details.json` | 2026-09-25 | MaiBot 官方插件仓库（顶层是 list）。三条分别覆盖：`manifest.urls.repository`、旧版 `manifest.repository_url`、两者都没有 |
| `nonebot_plugins.json` | `https://registry.nonebot.dev/plugins.json` | 2026-09-25 | NoneBot 官方商店索引（顶层是 list，键含 `module_name` / `project_link` / `homepage` / `valid`）。三条分别覆盖：仓库名与插件名对得上、对不上、homepage 不是 GitHub |
| `astrbot_collection.json` | `https://github.com/AstrBotDevs/AstrBot_Plugins_Collection/raw/refs/heads/main/plugin_cache_original.json` | 2026-09-25 | AstrBot 官方插件集合（顶层是 object，键是插件名，值里有 `repo`）；这也是 `AstrBotSource` 默认用的索引。三条里有两条正常，第三条是**真实存在的坏数据** `astrbot_plugin_yunsdf`（`repo` 写成 `https://github.com/WhiteCloudOL/`，没有仓库名），用来钉住「坏条目降级成 unsupported，而不是让整个源解析失败」 |
| `astrbot_market.json` | `https://api.soulter.top/astrbot/plugins` | 2026-09-25 | AstrBot 官方市场的原始形态（顶层 object，含 `$meta` 与 `download_url`）。默认不直连这个地址（主机不在白名单），夹具用来验证同一条解析路径 |

重新生成切片的方法：下载对应索引，按文件里出现的 `module_name` / 键名取 2–3 条原样写入
（**不要手改字段名**，夹具的意义就是绑住上游形态）。
