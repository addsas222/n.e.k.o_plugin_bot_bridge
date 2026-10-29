# Vendored `maibot_sdk` (unmodified copy)

* **Upstream**: https://github.com/Mai-with-u/maibot-plugin-sdk (`maibot_sdk` package; the same
  package ships inside the MaiBot host application `botfw/maibot`).
* **Version**: 2.19.0 (`maibot_sdk.__version__` / upstream `pyproject.toml`).
* **License**: LGPL-3.0-only — see `LICENSE` in this directory (copied verbatim from upstream).
* **Source revision**: upstream tag `v2.19.0` = commit `c2a039a7f4bcb367c7d770e88d6e034337be6b12`
  (2026-10-28), extracted with `git archive v2.19.0 maibot_sdk`.

## What this is

This is an **unmodified copy** of the upstream SDK, used by
`bridge/backends/maibot.py` as an **import fallback**: third-party MaiBot plugins do
`import maibot_sdk`, and N.E.K.O. does not ship that package. The backend only puts
`bot_bridge/vendor/` on `sys.path` when a **user-installed `maibot_sdk` is not
importable**, and a user-installed copy always takes precedence.

Therefore this directory is **replaceable**: it can be swapped for any other 2.x
release of the upstream SDK without touching bridge code, and it may be deleted
outright once `maibot_sdk` is installed in the host Python.

## Modification policy

Nothing in this tree is patched, reformatted, or renamed. Do **not** edit files here;
upgrade by replacing the whole `maibot_sdk/` directory with a fresh upstream copy and
updating this README.

## Upgrade history

| From | To | Relationship |
| --- | --- | --- |
| 2.8.2 (`5ad3a2a`) | **2.19.0** (`c2a039a`) | **Purely additive**: 0 modules removed, 0 public names removed across the 66 modules shared by both versions. New: `capabilities/{web,webui,files,market,internal_chat,desktop_pet}.py`, `config_widgets.py`, plus `ReplyExtension` / `ConfigWidget` exports. Verified by AST symbol diff of the two tags, then by replacing the tree and re-running `pytest tests`. |

The bridge only imports `maibot_sdk`, `.context`, `.plugin`, `.components`, `.types`,
`.config`, and `.compat` (+ `compat._import_hook`, `compat.legacy_adapter`) — all of
which exist unchanged in 2.19.0, so the legacy `src.plugin_system` path keeps working.

## Layout

```
vendor/
  maibot_sdk/            # upstream package, verbatim (75 files: 73 .py + py.typed + LICENSE)
    LICENSE              # upstream LGPL-3.0 text
    __init__.py ...      # capabilities/, compat/, plugin.py, context.py, config_widgets.py, ...
```
