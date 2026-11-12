"use strict";
/* 只管理本次启动的两个 Python 服务进程与准备 / 停止信号。

   - API 默认 33111、web 默认 22888，占用时按原启动器规则向后寻找
   - 只管理本次启动的子进程，不停止占用端口的其他应用
   - 使用随包 Python 与绝对 backend_runner.py，不寻找系统 Python / .venv
   - 两个进程都隐藏窗口，父进程管道关闭时 Python 自行正常退出 */
const { spawn } = require("node:child_process");
const net = require("node:net");
const fs = require("node:fs");
const paths = require("./paths");

const HOST = "127.0.0.1";
const API_PORT = 33111;
const WEB_PORT = 22888;
const READY_TIMEOUT = 60000;
const STOP_TIMEOUT = 45000;
const READY_PREFIX = "XM-IMS-READY";
const STOP_COMMAND = "XM-IMS-STOP\n";

function portFree(port) {
  return new Promise(resolve => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => server.close(() => resolve(true)));
    server.listen(port, HOST);
  });
}

async function findFreePort(start) {
  let port = start;
  while (port < 65535 && !(await portFree(port))) port += 1;
  return port;
}

class Backend {
  constructor() {
    this.processes = new Map();
    this.stopping = null;
  }

  async start() {
    if (!fs.existsSync(paths.pythonExe) || !fs.existsSync(paths.backendRunner)) {
      throw new Error("程序文件不完整，请重新安装 XM-IMS");
    }
    const apiPort = await findFreePort(API_PORT);
    const webPort = await findFreePort(WEB_PORT);
    const baseEnv = {
      ...process.env,
      XM_DESKTOP: "1",
      XM_PROGRAM_ROOT: paths.installRoot,
      XM_DATA_ROOT: paths.dataRoot,
      PYTHONUTF8: "1",
      PYTHONDONTWRITEBYTECODE: "1",
    };
    // 先起 API，再让 web 指向实际端口；两者都等到各自的准备消息后才算成功。
    const api = this._spawn("api", apiPort, baseEnv);
    await api.ready;
    const web = this._spawn("web", webPort, { ...baseEnv, XM_API_BASE: `http://${HOST}:${apiPort}` });
    await web.ready;
    return { url: `http://${HOST}:${webPort}` };
  }

  _spawn(role, port, env) {
    const child = spawn(paths.pythonExe, ["-B", paths.backendRunner, role, String(port)], {
      cwd: paths.backendRoot,
      env,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const entry = { child, port, ready: null };
    this.processes.set(role, entry);
    entry.ready = new Promise((resolve, reject) => {
      let settled = false;
      let buffered = "";
      const finish = (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) reject(error);
        else resolve();
      };
      const timer = setTimeout(() => finish(new Error("服务启动超时，请重新打开程序")), READY_TIMEOUT);
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", chunk => {
        buffered += chunk;
        let index;
        while ((index = buffered.indexOf("\n")) >= 0) {
          const line = buffered.slice(0, index).trim();
          buffered = buffered.slice(index + 1);
          if (line === `${READY_PREFIX} ${role} ${port}`) finish();
        }
      });
      // 服务日志只保留到进程结束，不进入界面也不写入数据目录。
      child.stderr.resume();
      child.once("error", () => finish(new Error("无法启动程序的后台服务")));
      child.once("exit", () => finish(new Error("后台服务启动失败，请重新打开程序")));
    });
    // 未 await 期间的失败由 start() 中的 await 处理，这里避免未捕获拒绝。
    entry.ready.catch(() => {});
    return entry;
  }

  /** 正常退出：发送停止指令并等待 lifespan 收尾；失败时只终止本次子进程。 */
  async stop() {
    if (this.stopping) return this.stopping;
    this.stopping = (async () => {
      const entries = [...this.processes.values()];
      for (const entry of entries) {
        try { entry.child.stdin.write(STOP_COMMAND); } catch (_) { /* 管道已关闭 */ }
      }
      const deadline = Date.now() + STOP_TIMEOUT;
      for (const entry of entries) {
        while (entry.child.exitCode === null && entry.child.signalCode === null && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 100));
        }
      }
      for (const entry of entries) {
        if (entry.child.exitCode === null && entry.child.signalCode === null) {
          try { entry.child.kill(); } catch (_) { /* 已退出 */ }
        }
      }
      this.processes.clear();
    })();
    return this.stopping;
  }
}

module.exports = { Backend };
