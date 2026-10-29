// 机器人插件兼容桥 —— 插件管理器里的管理面板（Hosted TSX，由插件管理器在线编译）。
//
// 数据来源：Python 侧 @ui.context(id="dashboard") 的返回值（props.state）。
// 注意：运行时全局只有组件与 hooks（见 globals.d.ts）。后端调用必须走 props.api，
// 直接引用那个短名字在面板里是未定义标识符（静态检查也会直接报错）。
// 动作来源：Python 侧 @ui.action(...)（props.actions）；只有带 @ui.action 的入口能在面板里调用。
import {
  ActionButton,
  Button,
  Card,
  DataTable,
  Grid,
  Inline,
  Input,
  Page,
  Stack,
  StatCard,
  StatusBadge,
  Tabs,
  Text,
  Toolbar,
  Warning,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type PluginRow = {
  key?: string
  plugin_id: string
  framework: string
  name: string
  version?: string
  started: boolean
  commands: string[]
  tools: string[]
  events: string[]
  security?: { clean?: boolean; dangerous?: number; warnings?: number }
}

type BackendRow = {
  name: string
  label: string
  plugins: number
  requires: string[]
}

type SourceRow = {
  name: string
  display_name: string
  ok: boolean
  entries: number | null
  stale: boolean
  age_seconds: number | null
  error: string
  note: string
}

type PushRow = {
  source: string
  kind: string
  timestamp: number | null
  preview: string
}

type MarketRow = {
  source: string
  plugin_id: string
  name: string
  version: string
  author: string
  description: string
  install_kind: string
}

type MarketSourcesResult = { sources?: SourceRow[] }

type MarketSearchResult = {
  ok?: boolean
  count?: number
  results?: MarketRow[]
  error?: string
  source_errors?: Record<string, string>
  // 每源命中数（截断前）：跨源按源轮流取，这组数字解释「为什么只看到这几条」。
  source_counts?: Record<string, number>
  source_failures?: Record<string, string>
}

type StoppedRow = {
  key: string
  plugin_id: string
  framework: string
  name?: string
  path?: string
  disabled?: boolean
  error?: string
}

type State = {
  disabled_plugins?: string[]
  stopped_plugins?: StoppedRow[]
  ok?: boolean
  error?: string
  plugins_root?: string
  framework_dirs?: Record<string, string>
  plugin_count?: number
  plugins?: PluginRow[]
  backends?: BackendRow[]
  backend_errors?: Record<string, string>
  errors?: Record<string, string>
  tools?: string[]
  entries?: string[]
  market?: {
    enabled?: boolean
    error?: string
    sources?: SourceRow[]
    source_errors?: Record<string, string>
  }
  recent_pushes?: PushRow[]
}

export default function Panel(props: PluginSurfaceProps<State>) {
  const { t, state, actions } = props

  const [query, setQuery] = props.useLocalState<string>("marketQuery", "天气")
  const [results, setResults] = props.useLocalState<MarketRow[]>("marketResults", [])
  const [selected, setSelected] = props.useLocalState<MarketRow | null>("marketSelected", null)
  const [marketError, setMarketError] = props.useLocalState<string>("marketError", "")
  const [actionError, setActionError] = props.useLocalState<string>("actionError", "")
  const [busy, setBusy] = props.useLocalState<string>("marketBusy", "")
  const [pluginSelected, setPluginSelected] = props.useLocalState<PluginRow | null>("pluginSelected", null)
  const [pluginMessage, setPluginMessage] = props.useLocalState<string>("pluginMessage", "")
  const [sourceCounts, setSourceCounts] = props.useLocalState<Record<string, number>>("marketSourceCounts", {})

  const reloadAction = actions.find((action) => action.id === "bridge_reload") as HostedAction | undefined
  const deleteAction = actions.find((action) => action.id === "bridge_delete_plugin") as HostedAction | undefined
  const canToggle = actions.some((action) => action.id === "bridge_set_plugin")
  const probeAction = actions.find((action) => action.id === "bridge_market_sources") as HostedAction | undefined
  const installAction = actions.find((action) => action.id === "bridge_market_install") as HostedAction | undefined
  const canSearch = actions.some((action) => action.id === "bridge_market_search")

  const plugins = state.plugins ?? []
  const backends = state.backends ?? []
  const sources = state.market?.sources ?? []
  const pushes = state.recent_pushes ?? []
  const disabledPlugins = state.disabled_plugins ?? []
  const stoppedPlugins = state.stopped_plugins ?? []
  // 入参统一用 `框架:插件`（`find_key` 对跨框架同名 id 会报「不唯一」，而面板没有手输入口）
  const keyOf = (row: { key?: string; framework?: string; plugin_id: string }): string =>
    row.key ?? `${row.framework ?? ""}:${row.plugin_id}`
  const errorRows = Object.entries(state.errors ?? {})
  const backendErrorRows = Object.entries(state.backend_errors ?? {})
  const joint = (items?: string[]): string => (items && items.length ? items.join(", ") : "—")

  // 宿主只授权「它已经知道」的入口：热改插件入口后需要重新加载插件（或重启 N.E.K.O.）
  // 面板才会拿到新动作，否则宿主返回 403。这里把它翻译成用户能照做的提示。
  function explainActionError(error: Error): void {
    const raw = error instanceof Error ? error.message : String(error)
    setActionError(
      raw.includes("403") || raw.includes("not exposed")
        ? `${raw} —— 宿主还没重新加载该插件的入口白名单：在插件管理器里重载本插件（或重启 N.E.K.O.）后重试。`
        : raw,
    )
  }

  // 索引抓取可能要 25s 上下（三个官方源合计），宿主 iframe 对单次请求有 30s 的客户端上限，
  // 且 ActionButton 内部不透传 timeoutMs —— 所以慢动作直接调 props.api.call 并显式给超时。
  const SLOW_CALL_TIMEOUT_MS = 180000

  async function runSearch(): Promise<void> {
    setMarketError("")
    setActionError("")
    setBusy("search")
    try {
      const out = (await props.api.call(
        "bridge_market_search",
        { query, limit: 20 },
        { timeoutMs: SLOW_CALL_TIMEOUT_MS, userInitiated: true },
      )) as MarketSearchResult
      const rows = Array.isArray(out?.results) ? out.results : []
      setResults(rows)
      setSourceCounts(out?.source_counts ?? {})
      setSelected(null)
      const failures = Object.entries(out?.source_failures ?? {})
      if (!rows.length) setMarketError(out?.error ?? "没有命中（换个关键词或先探活市场源）")
      else if (failures.length) setMarketError(failures.map(([name, why]) => `${name}: ${why}`).join("；"))
    } catch (error) {
      setResults([])
      setMarketError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusy("")
    }
  }

  async function setPluginEnabled(row: PluginRow, enabled: boolean): Promise<void> {
    setPluginMessage("")
    setActionError("")
    setBusy(`plugin:${row.plugin_id}`)
    try {
      const out = (await props.api.call(
        "bridge_set_plugin",
        { plugin_id: keyOf(row), enabled },
        { timeoutMs: 60000, userInitiated: true },
      )) as { summary?: string; error?: string }
      setPluginMessage(out?.summary ?? (enabled ? `已启动 ${row.plugin_id}` : `已停止 ${row.plugin_id}`))
    } catch (error) {
      setPluginMessage(`${row.plugin_id}: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy("")
    }
  }

  async function setStoppedEnabled(row: StoppedRow, enabled: boolean): Promise<void> {
    setPluginMessage("")
    setActionError("")
    setBusy(`plugin:${row.key}`)
    try {
      const out = (await props.api.call(
        "bridge_set_plugin",
        { plugin_id: row.key, enabled },
        { timeoutMs: 60000, userInitiated: true },
      )) as { summary?: string; error?: string }
      setPluginMessage(out?.summary ?? `${enabled ? "已启动" : "已停止"} ${row.key}`)
    } catch (error) {
      setPluginMessage(`${row.key}: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy("")
    }
  }

  async function probeSource(name: string): Promise<void> {
    setMarketError("")
    setActionError("")
    setBusy(name)
    try {
      const out = (await props.api.call(
        "bridge_market_sources",
        { refresh: true, source: name },
        { timeoutMs: SLOW_CALL_TIMEOUT_MS, userInitiated: true },
      )) as MarketSourcesResult
      const row = (out?.sources ?? []).find((item) => item.name === name)
      if (row && row.ok === false) setMarketError(`${name}: ${row.error || "索引不可用"}`)
    } catch (error) {
      setMarketError(`${name}: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      setBusy("")
    }
  }

  return (
    <Page
      title={t("panel.title", { default: "机器人插件兼容桥" })}
      subtitle={t("panel.subtitle", { default: "机器人插件（MaiBot / NoneBot2 / AstrBot / Koishi 等）在这里接入 N.E.K.O." })}
    >
      <Stack gap="md">
        <Grid cols={4}>
          <StatCard label={t("panel.statPlugins", { default: "已加载插件" })} value={state.plugin_count ?? plugins.length} />
          <StatCard label={t("panel.statBackends", { default: "兼容层" })} value={backends.length} />
          <StatCard label={t("panel.statTools", { default: "对话期工具" })} value={(state.tools ?? []).length} />
          <StatCard label={t("panel.statPushes", { default: "最近推送" })} value={pushes.length} />
        </Grid>

        <Toolbar>
          <Text>{t("panel.pluginsRoot", { default: "插件根目录" })}: {state.plugins_root || "—"}</Text>
          {Object.entries(state.framework_dirs ?? {})
            .filter(([, dir]) => dir)
            .map(([framework, dir]) => (
              <Text key={framework}>
                {t("panel.frameworkDir", { default: "插件源" })}[{framework}]: {dir}
              </Text>
            ))}
          {reloadAction ? (
            <ActionButton action={reloadAction} tone="primary" onError={explainActionError}>
              {t("panel.reload", { default: "重新扫描插件" })}
            </ActionButton>
          ) : null}
          {probeAction
            ? sources.map((row) => (
                <Button key={row.name} disabled={busy !== ""} onClick={() => probeSource(row.name)}>
                  {busy === row.name
                    ? `${t("panel.probing", { default: "探活中" })} ${row.name}…`
                    : `${t("panel.probeOne", { default: "探活" })} ${row.name}`}
                </Button>
              ))
            : null}
        </Toolbar>

        {actionError ? <Warning>{actionError}</Warning> : null}
        {state.ok === false && state.error ? <Warning>{state.error}</Warning> : null}
        {backendErrorRows.length ? (
          <Warning>
            <Stack gap="xs">
              {backendErrorRows.map(([name, reason]) => (
                <Text key={name}>{name}: {reason}</Text>
              ))}
            </Stack>
          </Warning>
        ) : null}

        <Tabs
          items={[
            {
              id: "plugins",
              label: t("panel.tabPlugins", { default: "插件" }),
              content: (
                <Card title={t("panel.pluginsCard", { default: "已加载的机器人插件" })}>
                  <Stack gap="md">
                    <DataTable
                      data={plugins}
                      rowKey="plugin_id"
                      selectedKey={pluginSelected?.plugin_id}
                      onSelect={(row: PluginRow) => setPluginSelected(row)}
                      emptyText={t("panel.noPlugins", { default: "还没有插件：把插件目录放进插件根目录，或去「市场」页安装。" })}
                      columns={[
                        { key: "framework", label: t("fields.framework", { default: "框架" }) },
                        { key: "plugin_id", label: t("fields.pluginId", { default: "插件 id" }) },
                        { key: "name", label: t("fields.name", { default: "名称" }) },
                        { key: "version", label: t("fields.version", { default: "版本" }) },
                        { key: "commands", label: t("fields.commands", { default: "命令" }), render: (row: PluginRow) => joint(row.commands) },
                        { key: "tools", label: t("fields.tools", { default: "工具" }), render: (row: PluginRow) => joint(row.tools) },
                        { key: "events", label: t("fields.events", { default: "事件" }), render: (row: PluginRow) => joint(row.events) },
                        {
                          key: "started",
                          label: t("fields.started", { default: "状态" }),
                          render: (row: PluginRow) => (
                            <StatusBadge tone={row.started ? "success" : "warning"} label={row.started ? "running" : "stopped"} />
                          ),
                        },
                        {
                          key: "security",
                          label: t("fields.security", { default: "安检" }),
                          render: (row: PluginRow) => {
                            const scan = row.security
                            if (!scan) {
                              // 没有报告 = 扫描关着（或旧数据）：不能假充「通过」
                              return <StatusBadge tone="default" label={t("fields.securityUnscanned", { default: "未扫描" })} />
                            }
                            if (scan.clean) {
                              return <StatusBadge tone="success" label={t("fields.securityClean", { default: "通过" })} />
                            }
                            const dangerous = scan.dangerous ?? 0
                            const label =
                              dangerous > 0
                                ? `${t("fields.securityDangerous", { default: "高风险" })} ×${dangerous}`
                                : `${t("fields.securityWarning", { default: "提示" })} ×${scan.warnings ?? 0}`
                            return <StatusBadge tone={dangerous > 0 ? "danger" : "warning"} label={label} />
                          },
                        },
                      ]}
                    />
                    <Inline gap="sm" align="center">
                      <Text>
                        {pluginSelected
                          ? `${t("panel.selected", { default: "已选中" })}: ${pluginSelected.plugin_id}`
                          : t("panel.selectHint", { default: "先在上表里选一行，再点安装" })}
                      </Text>
                      {canToggle && pluginSelected ? (
                        <Button
                          disabled={busy !== ""}
                          onClick={() => setPluginEnabled(pluginSelected, true)}
                        >
                          {busy === `plugin:${pluginSelected.plugin_id}`
                            ? t("panel.working", { default: "处理中…" })
                            : t("panel.startPlugin", { default: "启动" })}
                        </Button>
                      ) : null}
                      {canToggle && pluginSelected ? (
                        <Button
                          disabled={busy !== ""}
                          onClick={() => setPluginEnabled(pluginSelected, false)}
                        >
                          {t("panel.stopPlugin", { default: "停止" })}
                        </Button>
                      ) : null}
                      {deleteAction && pluginSelected ? (
                        <ActionButton
                          action={deleteAction}
                          tone="danger"
                          values={{ plugin_id: keyOf(pluginSelected), purge_state: false }}
                          onError={explainActionError}
                        >
                          {t("panel.deletePlugin", { default: "删除插件" })}
                        </ActionButton>
                      ) : null}
                    </Inline>
                    {pluginMessage ? <Warning>{pluginMessage}</Warning> : null}
                    {stoppedPlugins.length ? (
                      <Stack gap="sm">
                        <Text>
                          {t("panel.stoppedTitle", {
                            default: "已停用 / 没跑起来的插件（点「启动」就能拉回来，文件还在）",
                          })}
                        </Text>
                        <DataTable
                          data={stoppedPlugins}
                          rowKey="key"
                          emptyText="—"
                          columns={[
                            { key: "framework", label: t("fields.framework", { default: "框架" }) },
                            { key: "key", label: t("fields.pluginId", { default: "插件 id" }) },
                            { key: "name", label: t("fields.name", { default: "名称" }) },
                            {
                              key: "state",
                              label: t("fields.status", { default: "状态" }),
                              render: (row: StoppedRow) => (
                                <StatusBadge
                                  tone={row.error ? "danger" : row.disabled ? "warning" : "default"}
                                  label={row.error ? "load-failed" : row.disabled ? "disabled" : "not-running"}
                                />
                              ),
                            },
                            {
                              key: "note",
                              label: t("fields.note", { default: "说明" }),
                              render: (row: StoppedRow) => row.error || row.path || "—",
                            },
                            {
                              key: "actions",
                              label: t("fields.actions", { default: "操作" }),
                              render: (row: StoppedRow) => (
                                <Inline gap="sm" align="center">
                                  {canToggle ? (
                                    <Button disabled={busy !== ""} onClick={() => setStoppedEnabled(row, true)}>
                                      {busy === `plugin:${row.key}`
                                        ? t("panel.working", { default: "处理中…" })
                                        : t("panel.startPlugin", { default: "启动" })}
                                    </Button>
                                  ) : null}
                                  {deleteAction ? (
                                    <ActionButton
                                      action={deleteAction}
                                      tone="danger"
                                      values={{ plugin_id: row.key, purge_state: false }}
                                      onError={explainActionError}
                                    >
                                      {t("panel.deletePlugin", { default: "删除插件" })}
                                    </ActionButton>
                                  ) : null}
                                </Inline>
                              ),
                            },
                          ]}
                        />
                      </Stack>
                    ) : null}
                    {errorRows.length ? (
                      <Warning>
                        <Stack gap="xs">
                          {errorRows.map(([name, reason]) => (
                            <Text key={name}>{name}: {reason}</Text>
                          ))}
                        </Stack>
                      </Warning>
                    ) : null}
                  </Stack>
                </Card>
              ),
            },
            {
              id: "backends",
              label: t("panel.tabBackends", { default: "兼容层" }),
              content: (
                <Card title={t("panel.backendsCard", { default: "各框架兼容层" })}>
                  <Stack gap="md">
                    <DataTable
                      data={backends}
                      rowKey="name"
                      columns={[
                        { key: "name", label: t("fields.backend", { default: "层" }) },
                        { key: "label", label: t("fields.label", { default: "名称" }) },
                        { key: "plugins", label: t("fields.pluginCount", { default: "插件数" }) },
                        {
                          key: "requires",
                          label: t("fields.requires", { default: "运行时依赖" }),
                          render: (row: BackendRow) => joint(row.requires),
                        },
                      ]}
                    />
                    <Text>{t("panel.toolList", { default: "对话期工具" })}: {joint(state.tools)}</Text>
                  </Stack>
                </Card>
              ),
            },
            {
              id: "market",
              label: t("panel.tabMarket", { default: "市场" }),
              content: (
                <Card title={t("panel.marketCard", { default: "插件市场（官方索引）" })}>
                  <Stack gap="md">
                    <Text>
                      {state.market?.error ||
                        t("panel.marketReady", { default: "搜索只读；安装是显式动作，会写入插件目录并热重载。" })}
                    </Text>
                    <DataTable
                      data={sources}
                      rowKey="name"
                      columns={[
                        { key: "name", label: t("fields.source", { default: "源" }) },
                        { key: "entries", label: t("fields.entries", { default: "条目" }) },
                        {
                          key: "ok",
                          label: t("fields.status", { default: "状态" }),
                          render: (row: SourceRow) => (
                            <StatusBadge
                              tone={row.ok ? (row.stale ? "warning" : "success") : "danger"}
                              label={row.ok ? (row.stale ? "stale" : "ready") : "unavailable"}
                            />
                          ),
                        },
                        {
                          key: "note",
                          label: t("fields.note", { default: "说明" }),
                          render: (row: SourceRow) => row.error || row.note || "—",
                        },
                      ]}
                    />

                    <Inline gap="sm" align="center">
                      <Input
                        value={query}
                        placeholder={t("panel.searchPlaceholder", { default: "关键词，例如：天气 / 禁言" })}
                        disabled={!canSearch}
                        onChange={(next: string) => setQuery(next)}
                      />
                      <Button disabled={!canSearch || busy !== ""} onClick={runSearch}>
                        {busy === "search" ? t("panel.searching", { default: "搜索中…" }) : t("panel.search", { default: "搜索" })}
                      </Button>
                    </Inline>

                    {marketError ? <Warning>{marketError}</Warning> : null}
                    {Object.keys(sourceCounts).length ? (
                      <Text>
                        {t("panel.sourceCounts", { default: "各源命中（跨源按源轮流取）" })}:{" "}
                        {Object.entries(sourceCounts)
                          .map(([name, total]) => `${name} ${total}`)
                          .join(" / ")}
                      </Text>
                    ) : null}

                    <DataTable
                      data={results}
                      rowKey="plugin_id"
                      selectedKey={selected?.plugin_id}
                      emptyText={t("panel.noResults", { default: "搜索结果为空" })}
                      onSelect={(row: MarketRow) => setSelected(row)}
                      columns={[
                        { key: "source", label: t("fields.source", { default: "源" }) },
                        { key: "plugin_id", label: t("fields.pluginId", { default: "插件 id" }) },
                        { key: "name", label: t("fields.name", { default: "名称" }) },
                        { key: "version", label: t("fields.version", { default: "版本" }) },
                        { key: "install_kind", label: t("fields.installKind", { default: "安装方式" }) },
                      ]}
                    />

                    <Inline gap="sm" align="center">
                      <Text>
                        {selected
                          ? `${t("panel.selected", { default: "已选中" })}: ${selected.source}:${selected.plugin_id}`
                          : t("panel.selectHint", { default: "先在上表里选一行，再点安装" })}
                      </Text>
                      {installAction && selected ? (
                        <ActionButton
                          action={installAction}
                          tone="danger"
                          values={{ source: selected.source, plugin_id: selected.plugin_id, overwrite: false }}
                          onError={explainActionError}
                        >
                          {t("panel.install", { default: "安装选中插件" })}
                        </ActionButton>
                      ) : null}
                    </Inline>

                    <Text>
                      {t("panel.cliHint", {
                        default: "脚本用法：POST /runs → entry_id=bridge_market_install，args={source, plugin_id}。",
                      })}
                    </Text>
                  </Stack>
                </Card>
              ),
            },
            {
              id: "pushes",
              label: t("panel.tabPushes", { default: "推送" }),
              content: (
                <Card title={t("panel.pushesCard", { default: "最近进入对话的推送" })}>
                  <DataTable
                    data={pushes}
                    rowKey="source"
                    emptyText={t("panel.noPushes", { default: "暂无推送：外来插件调用 send 后会出现在这里。" })}
                    columns={[
                      { key: "source", label: t("fields.source", { default: "来源" }) },
                      { key: "kind", label: t("fields.kind", { default: "类型" }) },
                      { key: "preview", label: t("fields.preview", { default: "内容" }) },
                    ]}
                  />
                </Card>
              ),
            },
          ]}
        />
      </Stack>
    </Page>
  )
}
