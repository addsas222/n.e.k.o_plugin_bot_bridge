#!/usr/bin/env node
// bot_bridge 的 Koishi 宿主进程：加载一个 koishi 插件，把它的 command/事件
// 投影成 JSON-lines 协议消息，与 Python 桥接层（backends/koishi.py）通信。
//
// 协议（stdin/stdout 每行一个 JSON 对象，UTF-8）：
//   宿主 → 控制器：
//     {"type":"ready","manifest":{...}}
//     {"type":"result","call_id":N,"ok":bool,"replies":[...],"error":"..."}
//     {"type":"call","call_id":N,"method":"send_chat","params":{...}}   ← 插件反向借用宿主能力
//     {"type":"log","level":"info","message":"..."}
//   控制器 → 宿主：
//     {"type":"invoke_command","call_id":N,"name":"…","argv":{…},"session":{…}}
//     {"type":"call_result","call_id":N,"ok":bool,"value":…,"error":"…"}
//     {"type":"event","event":"…","payload":{…}}
//     {"type":"start"} / {"type":"stop"}
//
// 设计上限（ponytail 级）：只覆盖 command/action/session.send/logger/ready/dispose；
// 依赖 koishi 内部服务（database/console/model）的插件会在访问时拿到清晰错误。

import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import readline from 'node:readline';
import process from 'node:process';
import path from 'node:path';
import fs from 'node:fs';

// 宿主自己的桩（koishi / schemastery）相对本文件解析——与插件目录无关。
const localRequire = createRequire(import.meta.url);
const { makeContext, Session, Schema } = localRequire('koishi');

function argValue(flag) {
  const index = process.argv.indexOf(flag);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const pluginDir = path.resolve(argValue('--plugin-dir') || process.cwd());
const configJson = argValue('--config') || '{}';

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}
function log(level, message) {
  send({ type: 'log', level, message: String(message).slice(0, 600) });
}

let callSeq = 0;
const pendingCalls = new Map();
function hostCall(method, params) {
  const call_id = ++callSeq;
  return new Promise((resolve, reject) => {
    pendingCalls.set(call_id, { resolve, reject });
    send({ type: 'call', call_id, method, params });
    setTimeout(() => {
      if (pendingCalls.has(call_id)) {
        pendingCalls.delete(call_id);
        reject(new Error(`宿主能力调用超时：${method}`));
      }
    }, 30000);
  });
}

const hostBridge = {
  async send_chat(params) { return hostCall('send_chat', params || {}); },
  async llm_generate(params) { return hostCall('llm_generate', params || {}); },
  // 兼容两种调用形态：Logger._emit 传 (level, prefix, args数组)；
  // ContextCore 的 logger 传 (level, 完整message)。
  log(level, messageOrPrefix, args) {
    const message = Array.isArray(args)
      ? `${messageOrPrefix} ${args.map((a) => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ')}`
      : String(messageOrPrefix);
    log(level, message);
  },
};

let ctx = null;
let plugin = null;

async function invokeCommand(msg) {
  const command = ctx.getCommand(msg.name);
  if (!command) {
    send({ type: 'result', call_id: msg.call_id, ok: false, replies: [], error: `未知命令：${msg.name}` });
    return;
  }
  const payload = msg.session || {};
  payload.argv = msg.argv || { name: command.name, options: {}, args: [] };
  const session = new Session(payload, hostBridge, ctx);
  let result;
  try {
    result = await command.run(session);
  } catch (err) {
    send({
      type: 'result',
      call_id: msg.call_id,
      ok: false,
      replies: session._replies,
      error: String((err && err.message) || err),
    });
    return;
  }
  if (result !== undefined && result !== null) session._replies.push(String(result));
  send({ type: 'result', call_id: msg.call_id, ok: true, replies: session._replies });
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
  if (!line.trim()) return;
  let msg;
  try { msg = JSON.parse(line); } catch (_) { return; }
  if (msg.type === 'call_result') {
    const pending = pendingCalls.get(msg.call_id);
    if (!pending) return;
    pendingCalls.delete(msg.call_id);
    if (msg.ok) pending.resolve(msg.value);
    else pending.reject(new Error(msg.error || '宿主能力调用失败'));
  } else if (msg.type === 'invoke_command') {
    invokeCommand(msg).catch((err) => {
      send({
        type: 'result',
        call_id: msg.call_id,
        ok: false,
        replies: [],
        error: String((err && err.message) || err),
      });
    });
  } else if (msg.type === 'event') {
    ctx.emit(msg.event, msg.payload);
  } else if (msg.type === 'start') {
    ctx.emit('ready', ctx);
    ctx.emit('start', ctx);
    log('info', 'ready/start 已派发');
    send({ type: 'result', call_id: msg.call_id, ok: true });
  } else if (msg.type === 'stop') {
    ctx.emit('dispose', ctx);
    log('info', 'dispose 已派发，进程退出');
    send({ type: 'result', call_id: msg.call_id, ok: true });
    process.exit(0);
  }
});

async function main() {
  const packageJson = JSON.parse(fs.readFileSync(path.join(pluginDir, 'package.json'), 'utf-8'));
  const exported = packageJson.exports && typeof packageJson.exports === 'object'
    ? packageJson.exports['.']
    : undefined;
  const entryRel = packageJson.main || packageJson.module
    || (exported && typeof exported === 'object' ? (exported.import || exported.require) : exported)
    || 'index.js';
  const entryPath = path.resolve(pluginDir, entryRel);
  if (!fs.existsSync(entryPath)) {
    send({ type: 'error', message: `入口不存在：${entryPath}` });
    process.exit(1);
  }
  let config = {};
  try { config = JSON.parse(configJson); } catch (_) {}

  ctx = makeContext(hostBridge, config);

  const pluginModule = await import(pathToFileURL(entryPath).href);
  plugin = pluginModule.default || pluginModule;
  // 注意：不能用 plugin.apply(ctx, config)——那是 Function.prototype.apply
  //（this=ctx、参数=config），koishi 约定是普通调用 plugin(ctx, config)。
  const init = typeof plugin === 'function' ? plugin : plugin?.apply;
  if (typeof init !== 'function') {
    send({ type: 'error', message: '插件没有可调用的 apply/default 导出' });
    process.exit(1);
  }
  await init(ctx, config);

  const commands = {};
  for (const [name, command] of Object.entries(ctx.commands)) {
    commands[name] = {
      name,
      description: command.description,
      aliases: command.aliases,
      options: command.options.map((o) => ({ name: o.name, description: o.description })),
    };
  }
  const events = [...ctx._listeners.keys()].map((event) => ({ event_type: event }));
  send({
    type: 'ready',
    manifest: {
      name: packageJson.name,
      version: packageJson.version,
      description: packageJson.description || '',
      commands,
      events,
    },
  });
}

main().catch((err) => {
  send({ type: 'error', message: String((err && err.stack) || err).slice(0, 1200) });
  process.exit(1);
});
