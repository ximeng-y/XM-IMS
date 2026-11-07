"""GitHub 官方 HTTPS 访问与隐藏子进程调用。

Token 只存在于请求头与本进程内存，不进日志、异常文本或响应；对官方 HTTPS 来源
的只读请求提供有界退避，创建仓库、推送与浏览器授权不自动重试。
"""
import json
import subprocess
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
    """后台子进程：隐藏窗口、默认不给标准输入，避免任何交互式提示挂起。"""
    return subprocess.run(argv, input=data, capture_output=True, timeout=timeout,
                          stdin=None if data is not None else subprocess.DEVNULL,
                          env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
