"""XM-IMS 启动器：同一控制台内拉起后端 + 前端两个 uvicorn 进程

- 后端默认端口 33111、前端默认端口 22888，被占用时自动顺延到空闲端口
- 前端经环境变量 XM_API_BASE 获知后端实际端口（端口顺延后前后端仍对上）
- 两个子进程共享本控制台（不新开窗口）：关闭控制台时 Windows 会向同一
  控制台的全部进程广播 CTRL_CLOSE，默认处理即终止所有进程，因此关窗即
  整体停止；本进程因其他原因退出时，finally 中也会主动杀掉子进程兜底
- 本窗口常驻前台：任一服务退出则停止其余服务并退出，窗口随之关闭

环境变量：XM_NO_BROWSER=1 时不自动打开浏览器（测试用）。
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
BACKEND_START_PORT = 33111
FRONTEND_START_PORT = 22888
READY_TIMEOUT = 30


def _python() -> str:
    venv_py = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    return str(venv_py) if venv_py.is_file() else sys.executable


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def _find_free_port(start: int) -> int:
    port = start
    while not _port_free(port):
        port += 1
    return port


def _wait_ready(proc: subprocess.Popen, port: int) -> bool:
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False  # 进程已退出，多半是启动失败（日志就在本窗口上方）
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            try:
                s.connect((HOST, port))
                return True
            except OSError:
                pass
        time.sleep(0.3)
    return False


def _kill_all(procs: list) -> None:
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            pass


def main() -> None:
    # 控制台与管道重定向下都按行即时输出（进程常驻，块缓冲会积压提示信息）
    sys.stdout.reconfigure(line_buffering=True)
    py = _python()
    backend_port = _find_free_port(BACKEND_START_PORT)
    frontend_port = _find_free_port(FRONTEND_START_PORT)

    env = os.environ.copy()
    env["XM_API_BASE"] = f"http://{HOST}:{backend_port}"

    procs: list = []
    try:
        procs.append(subprocess.Popen(
            [py, "-m", "uvicorn", "app.api_app:app",
             "--host", HOST, "--port", str(backend_port)],
            cwd=BASE_DIR, env=env))
        procs.append(subprocess.Popen(
            [py, "-m", "uvicorn", "app.web_app:app",
             "--host", HOST, "--port", str(frontend_port)],
            cwd=BASE_DIR, env=env))
    except OSError as e:
        print(f"无法启动子进程：{e}")
        _kill_all(procs)
        sys.exit(1)

    try:
        if not _wait_ready(procs[0], backend_port):
            print("后端启动失败，请查看上方日志后重新启动。")
            sys.exit(1)
        if not _wait_ready(procs[1], frontend_port):
            print("前端启动失败，请查看上方日志后重新启动。")
            sys.exit(1)

        frontend_url = f"http://{HOST}:{frontend_port}"
        print("=" * 58)
        print("  XM-IMS 已启动")
        print(f"  前端页面 : {frontend_url}")
        print(f"  后端 API : http://{HOST}:{backend_port}   (OpenAPI 文档 /docs)")
        if backend_port != BACKEND_START_PORT:
            print(f"  [提示] 后端端口 {BACKEND_START_PORT} 被占用，已顺延至 {backend_port}")
        if frontend_port != FRONTEND_START_PORT:
            print(f"  [提示] 前端端口 {FRONTEND_START_PORT} 被占用，已顺延至 {frontend_port}")
        print("  关闭本窗口即停止全部服务")
        print("=" * 58)

        if os.environ.get("XM_NO_BROWSER") != "1":
            webbrowser.open(frontend_url)

        # 前台常驻：任一服务退出则整体收尾（正常情况一直等到关窗）
        while True:
            if any(p.poll() is not None for p in procs):
                print("有服务已退出，正在停止其余服务并退出…")
                time.sleep(4)  # 留出时间阅读提示
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("收到中断信号，正在停止前后端服务…")
    finally:
        _kill_all(procs)


if __name__ == "__main__":
    main()
