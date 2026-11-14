"""桌面外壳内的 Python 服务入口。

只接受内部角色 api / web 与实际端口，角色对应既有 app.api_app / app.web_app，
生命周期、数据库与同步管理器全部沿用原实现，不在这里另起一套。

准备信号：服务真正开始监听后向 stdout 输出一行
    XM-IMS-READY <role> <port>
Node 只把本次子进程的这条消息当作准备完成，不用别的应用占用端口来推断成功。

首次启动控制（仅 API 角色、仅本次启动标识一致时）：
    启动器 → Node → 管道 → 本入口：XM-IMS-TRIAL-COMMIT / XM-IMS-TRIAL-RELEASE
两个命令把本次控制对象里的等待状态前移；非本次试运行进程不接受这些命令。
Runner 另向 stdout 输出本次阶段消息，由 API 应用转发给 Node。

停止信号：stdin 收到 XM-IMS-STOP 或父进程管道 EOF（外壳意外结束）时设置
uvicorn 的退出标志，让已有请求与 lifespan 正常收尾。不提供 HTTP 停机端点，
也不接收业务命令、模块路径或 shell 命令。
"""
import os
import sys
import threading

import uvicorn

STOP_COMMAND = "XM-IMS-STOP"
TRIAL_COMMIT = "XM-IMS-TRIAL-COMMIT"
TRIAL_RELEASE = "XM-IMS-TRIAL-RELEASE"
_HOST = "127.0.0.1"
ROLES = ("api", "web")


class _Runner(uvicorn.Server):
    """在服务真正开始监听之后才报告准备完成；端口占用等失败不会误报。"""

    def __init__(self, config, role, port):
        super().__init__(config)
        self.role, self.port = role, port

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            print(f"XM-IMS-READY {self.role} {self.port}", flush=True)


class TrialControl:
    """本次首次启动的交接信号：生命周期等待它，stdin 线程推进它。

    commit / release / stop 三个信号都是单向的；STOP 或父进程 EOF 会唤醒所有
    正在等待的生命周期，不留下无限等待事件。
    """

    def __init__(self, attempt):
        self.attempt = str(attempt)
        self._commit = threading.Event()
        self._release = threading.Event()
        self._stop = threading.Event()

    def request(self, signal):
        getattr(self, "_" + signal).set()

    def wait_commit(self):
        return self._wait(self._commit)

    def wait_release(self):
        return self._wait(self._release)

    def _wait(self, event):
        while not event.is_set() and not self._stop.is_set():
            event.wait(0.2)
        return event.is_set()

    def stop(self):
        self._stop.set()

    @property
    def stopped(self):
        return self._stop.is_set()

    def report(self, phase, extra=""):
        """向 Node 输出本次阶段消息；只在本次标识内有效。"""
        line = f"XM-IMS-TRIAL {self.attempt} {phase}"
        print(line + (f" {extra}" if extra else ""), flush=True)


def _read_stdin(server, control=None) -> None:
    """父进程管道关闭（EOF）或收到停止指令时请求退出。"""
    try:
        for line in sys.stdin:
            command = line.strip()
            if command == STOP_COMMAND:
                break
            if control is not None and command in (TRIAL_COMMIT, TRIAL_RELEASE):
                control.request("commit" if command == TRIAL_COMMIT else "release")
    except (OSError, ValueError):
        pass
    if control is not None:
        control.stop()
    server.should_exit = True


def _install_control(attempt):
    """API 进程注入本次控制对象；标识格式不正确视为非试运行进程。"""
    if not attempt or not all(ch in "0123456789abcdef" for ch in attempt):
        return None
    from app import runtime
    control = TrialControl(attempt)
    runtime.set_trial_control(control)
    return control


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in ROLES:
        print("XM-IMS-START-FAILED 用法：backend_runner.py <api|web> <port>", file=sys.stderr)
        return 2
    role, raw_port = argv
    try:
        port = int(raw_port)
    except ValueError:
        print("XM-IMS-START-FAILED 端口无效", file=sys.stderr)
        return 2

    control = _install_control(os.environ.get("XM_DESKTOP_ATTEMPT", "")) if role == "api" else None
    if role == "api":
        from app.api_app import create_api_app as create
    else:
        from app.web_app import create_web_app as create

    server = _Runner(uvicorn.Config(create(), host=_HOST, port=port, log_level="warning"),
                     role, port)
    threading.Thread(target=_read_stdin, args=(server, control), daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
