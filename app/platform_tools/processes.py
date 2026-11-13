"""隐藏子进程与有期限的内存输出捕获，不包含安装或 Git 业务规则。"""
import os
import select
import subprocess
import tempfile
import time

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _PEEK_NAMED_PIPE = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    _PEEK_NAMED_PIPE.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    _PEEK_NAMED_PIPE.restype = wintypes.BOOL


def run_hidden_memory(argv, env=None, timeout=30, data=None, *, cwd=None, capture_stderr=False):
    """隔离标准输入，按同一截止时间读取输出，输出只保存在内存中。

    Windows 包装进程退出后，后代可能仍持有输出管道。主动读取管道而不启动
    阻塞读取线程，保证超时清理不会再次无限等待 EOF；无输入时不继承桌面
    后台的控制管道。有输入时使用独立文件，避免输入写入阻塞超时检查。
    """
    with tempfile.TemporaryFile() as stdin_file:
        if data is None:
            stdin = subprocess.DEVNULL
        else:
            stdin_file.write(data)
            stdin_file.seek(0)
            stdin = stdin_file
        process = subprocess.Popen(
            argv, stdin=stdin, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            cwd=cwd, env=env, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + timeout
        try:
            stdout, stderr = _read_pipes_until_eof(process, deadline, timeout)
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _stop_process(process)
            raise subprocess.TimeoutExpired(argv, timeout) from None
        except BaseException:
            _stop_process(process)
            raise
        finally:
            for pipe in (process.stdout, process.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except (OSError, ValueError):
                        pass
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _read_pipes_until_eof(process, deadline, timeout):
    """轮流读取两个标准流，避免某一个流填满后阻塞子进程。"""
    output = {"stdout": bytearray(), "stderr": bytearray()}
    pending = {
        pipe.fileno(): name
        for name in output
        if (pipe := getattr(process, name)) is not None
    }
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        if os.name == "nt":
            ready = []
            for fd in tuple(pending):
                available = _pipe_available(fd)
                if available is None:
                    del pending[fd]
                elif available:
                    ready.append((fd, min(available, 65536)))
            if not ready:
                if pending:
                    time.sleep(min(0.01, remaining))
                continue
        else:
            ready = [(fd, 65536) for fd in select.select(list(pending), [], [], remaining)[0]]
            if not ready:
                raise subprocess.TimeoutExpired(process.args, timeout)
        for fd, size in ready:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)
            chunk = os.read(fd, size)
            if chunk:
                output[pending[fd]].extend(chunk)
            else:
                del pending[fd]
    return bytes(output["stdout"]), bytes(output["stderr"])


def _pipe_available(fd):
    """返回 Windows 管道可读字节数；写端全部关闭时返回 None。"""
    available = wintypes.DWORD()
    handle = msvcrt.get_osfhandle(fd)
    if _PEEK_NAMED_PIPE(handle, None, 0, None, ctypes.byref(available), None):
        return available.value
    error = ctypes.get_last_error()
    if error in (109, 232):
        return None
    raise ctypes.WinError(error)


def _stop_process(process):
    """终止本层进程，只进行有上限的等待。"""
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
