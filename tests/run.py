"""在模拟桌面标准输入的独立进程中运行两层回归测试。"""
import ctypes
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WORKER_TIMEOUT = 45


class WindowsJob:
    """关闭时清理本次测试的进程树，不留下占用输出管道的后代。"""

    def __init__(self):
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "read_ops", "write_ops", "other_ops",
                "read_bytes", "write_bytes", "other_bytes",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io", IOCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def worker():
    from tests import workspace

    workspace()
    # 外层先绑定进程树清理，再放行；输入管道的写端在整个测试期间保持打开。
    if sys.stdin.buffer.readline() != b"START\n":
        raise RuntimeError("测试进程未收到启动信号")
    reading = threading.Event()

    def read_control():
        reading.set()
        # 使用原始读取，避免阻塞中的守护线程持有 Python 缓冲流锁而影响退出。
        while os.read(sys.stdin.fileno(), 4096):
            pass

    threading.Thread(target=read_control, daemon=True).start()
    reading.wait()
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT),
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def launch(directory):
    # 不继承调用者的 Git 仓库路径和配置，所有 Git 操作只针对临时本地仓库。
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        XM_BACKUP_TEST_ROOT=str(directory), XM_DESKTOP="1",
        XM_PROGRAM_ROOT=str(directory), XM_DATA_ROOT=str(directory / "data"),
        PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
    )
    job = WindowsJob() if os.name == "nt" else None
    process = None
    timed_out = False
    log = directory / "unittest.log"
    try:
        # 外层不用 PIPE 捕获输出，避免测试失败时外层也因等待管道 EOF 而挂起。
        with log.open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-u", "-B", str(Path(__file__).resolve()), "--worker"],
                cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=output, stderr=output,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt",
            )
            if job:
                job.assign(process)
            process.stdin.write(b"START\n")
            process.stdin.flush()
            try:
                process.wait(timeout=WORKER_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        if job:
            job.close()
        elif process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
    print(log.read_text(encoding="utf-8", errors="replace"), end="")
    if timed_out:
        print(f"测试进程超过 {WORKER_TIMEOUT} 秒，已终止本次测试的进程树。")
        return 1
    return process.returncode


def main():
    if sys.argv[1:] == ["--worker"]:
        return worker()
    temporary_root = (ROOT / ".XMTEMP").resolve()
    if not temporary_root.is_relative_to(ROOT.resolve()):
        raise RuntimeError("临时目录必须位于项目内")
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="backup-regression-", dir=temporary_root) as name:
        directory = Path(name).resolve()
        if not directory.is_relative_to(temporary_root):
            raise RuntimeError("测试目录越界")
        return launch(directory)


if __name__ == "__main__":
    sys.exit(main())
