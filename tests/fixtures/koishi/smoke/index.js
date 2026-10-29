'use strict';
// 冒烟夹具：一个最简单的 koishi 命令插件。
// action 只返回字符串（经由桥接层回给 N.E.K.O），不向会话额外发言，避免重复。

module.exports = function apply(ctx) {
  ctx.command('smoke <text:text>', '冒烟测试命令').action(({ session }, text) => {
    const echoed = text || (session.argv && session.argv.args && session.argv.args.join(' ')) || '';
    return `pong: ${echoed}`;
  });
};
