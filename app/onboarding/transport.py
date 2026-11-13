"""GitHub 官方 HTTPS 访问与隐藏子进程调用。

Token 只存在于请求头与本进程内存，不进日志、异常文本或响应；对官方 HTTPS 来源
的只读请求提供有界退避，创建仓库、推送与浏览器授权不自动重试。
"""
import json
import os
import select
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from ..sync.common import fail

GITHUB_API = "https://api.github.com"
USER_AGENT = "XM-IMS-Onboarding"
# 只读请求的重试间隔；有副作用的调用一律不重试。
RETRY_DELAYS = (2.0, 5.0)
DOWNLOAD_DEADLINE = 240.0
INSTALLER_TIMEOUT = 300
LOGIN_TIMEOUT = 180

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


class GitHubHttpError(Exception):
    """GitHub 返回的非 2xx；body 只含 GitHub 的错误说明，不含任何本机凭据。"""

    def __init__(self, status, body):
        self.status, self.body = status, body


def request_json(url, method="GET", token=None, payload=None, timeout=30, retry=True):
    """调用官方 GitHub HTTPS API；Token 只存在于请求头。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    attempts = 3 if retry else 1
    for attempt in range(attempts):
        if attempt:
            time.sleep(RETRY_DELAYS[attempt - 1])
        try:
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429 or exc.headers.get("X-RateLimit-Remaining") == "0" \
                    or (exc.code == 403 and "rate limit" in body.lower()):
                fail("ONBOARDING_RATE_LIMITED", "GitHub 接口调用次数达到限制，请稍后再试",
                     user_message="GitHub 暂时拒绝了请求",
                     user_hint="这是接口调用频率限制，请等待几分钟后再试")
            if exc.code >= 500 and attempt < attempts - 1:
                continue
            raise GitHubHttpError(exc.code, body)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < attempts - 1:
                continue
            fail("ONBOARDING_NETWORK", f"访问 GitHub 失败：{url}",
                 user_message="连接 GitHub 失败",
                 user_hint="请检查网络连接后重试；如需代理，请先在本机配置好代理")


def download(url, target):
    """流式下载官方安装包；整段下载有总时限，网络类错误按 2s/5s 有限重试。"""
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    for attempt in range(3):
        if attempt:
            time.sleep(RETRY_DELAYS[attempt - 1])
        if time.monotonic() >= deadline:
            break
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as resp, open(target, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        return
                    out.write(chunk)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("下载超时")
        except urllib.error.HTTPError as exc:
            exc.read()
            if exc.code >= 500 and attempt < 2:
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    fail("ONBOARDING_DOWNLOAD", f"下载官方安装包失败或超时：{url}",
         user_message="下载官方安装包没有成功",
         user_hint="请检查网络后重试；也可以从官网手动安装后点击「重新检测」")


def run_hidden(argv, env=None, timeout=30, data=None):
    """后台子进程：隐藏窗口，并用临时文件承接不含凭据的标准流。

    Git 的 Windows 启动包装器退出后，实际子进程可能继续持有输出管道。若继续
    使用 PIPE，超时清理会等待管道彻底关闭而没有期限；临时文件不依赖 EOF。
    可能输出凭据的命令必须使用 run_hidden_memory，不能写入临时文件。
    """
    with tempfile.TemporaryFile() as stdin_file, \
            tempfile.TemporaryFile() as stdout_file, \
            tempfile.TemporaryFile() as stderr_file:
        if data is None:
            stdin = subprocess.DEVNULL
        else:
            stdin_file.write(data)
            stdin_file.seek(0)
            stdin = stdin_file
        result = subprocess.run(
            argv, stdin=stdin, stdout=stdout_file, stderr=stderr_file, timeout=timeout,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stdout_file.seek(0)
        stderr_file.seek(0)
        result.stdout = stdout_file.read()
        result.stderr = stderr_file.read()
        return result


def run_hidden_memory(argv, env=None, timeout=30, data=None):
    """后台子进程：隐藏窗口，标准输出只保存在本进程内存。

    Windows Git 启动包装器可能留下继续持有输出管道的子进程。这里只用同一截止
    时间主动读取管道，不启动会阻塞在 read() 的线程；超时后可直接关闭管道并返回，
    避免等待 EOF 无限挂起，也避免凭据被写入临时文件。
    """
    with tempfile.TemporaryFile() as stdin_file:
        if data is None:
            stdin = subprocess.DEVNULL
        else:
            stdin_file.write(data)
            stdin_file.seek(0)
            stdin = stdin_file
        process = subprocess.Popen(
            argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + timeout
        try:
            output = _read_pipe_until_eof(process, deadline, timeout)
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _stop_process(process)
            raise subprocess.TimeoutExpired(argv, timeout) from None
        except BaseException:
            _stop_process(process)
            raise
        finally:
            if process.stdout:
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass
        return subprocess.CompletedProcess(argv, process.returncode, output, b"")


def _read_pipe_until_eof(process, deadline, timeout):
    """按截止时间主动读取管道；Windows 不依赖可能被后代进程占住的 EOF。"""
    fd = process.stdout.fileno()
    output = bytearray()
    if os.name == "nt":
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            available = _pipe_available(fd)
            if available is None:
                return bytes(output)
            if available:
                output.extend(os.read(fd, min(available, 65536)))
                continue
            time.sleep(min(0.01, remaining))

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        if not select.select([fd], [], [], remaining)[0]:
            raise subprocess.TimeoutExpired(process.args, timeout)
        chunk = os.read(fd, 65536)
        if not chunk:
            return bytes(output)
        output.extend(chunk)


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
    """终止仍在运行的本层进程；等待上限固定，避免超时清理再次挂住。"""
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
