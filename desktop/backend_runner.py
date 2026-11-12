"""桌面外壳内的 Python 服务入口。

只接受内部角色 api / web 与实际端口，角色对应既有 app.api_app / app.web_app，
生命周期、数据库与同步管理器全部沿用原实现，不在这里另起一套。

准备信号：服务真正开始监听后向 stdout 输出一行
    XM-IMS-READY <role> <port>
Node 只把本次子进程的这条消息当作准备完成，不用别的应用占用端口来推断成功。

停止信号：stdin 收到 XM-IMS-STOP 或父进程管道 EOF（外壳意外结束）时设置
uvicorn 的退出标志，让已有请求与 lifespan 正常收尾。不提供 HTTP 停机端点，
也不接收业务命令、模块路径或 shell 命令。
"""
import os
import sys
import threading

import uvicorn

STOP_COMMAND = "XM-IMS-STOP"
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


def _read_stdin(server) -> None:
    """父进程管道关闭（EOF）或收到停止指令时请求退出。"""
    try:
        for line in sys.stdin:
            if line.strip() == STOP_COMMAND:
                break
    except (OSError, ValueError):
        pass
    server.should_exit = True


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

    if role == "api":
        from app.api_app import create_api_app as create
    else:
        from app.web_app import create_web_app as create

    server = _Runner(uvicorn.Config(create(), host=_HOST, port=port, log_level="warning"),
                     role, port)
    threading.Thread(target=_read_stdin, args=(server,), daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
