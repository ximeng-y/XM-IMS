"""备份配置引导：环境检测、组件安装、GitHub 授权、候选检查与最终应用。

只依附当前 SyncManager 存在，每个后端实例一个服务对象；
不改变同步状态机、冲突流程与既有设置接口的行为。
"""
import json
import os
import platform
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .. import config, db
from ..errors import ApiError, error_dict, error_payload
from . import gitstore, snapshot
from .common import fail, remove_tree
from .gitstore import GitStore

GITHUB_API = "https://api.github.com"
_USER_AGENT = "XM-IMS-Onboarding"
# 仓库名只允许常规字母、数字、点、下划线、连字符，避免拼出任意 API 路径。
_REPO_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_GIT_ASSET_PATTERN = re.compile(r"Git-\d[\w.]*-64-bit\.exe")
_GCM_ASSET_PATTERN = re.compile(r"gcmuser-win-x64-[\w.]+\.exe")
# 对官方 HTTPS 来源的只读请求提供有界退避；创建仓库、推送与浏览器授权不重试。
_RETRY_DELAYS = (2.0, 5.0)
_DOWNLOAD_DEADLINE = 240.0
_INSTALLER_TIMEOUT = 300
_LOGIN_TIMEOUT = 180


class _GitHubHttpError(Exception):
    """GitHub 返回的非 2xx；body 只含 GitHub 的错误说明，不含任何本机凭据。"""

    def __init__(self, status, body):
        self.status, self.body = status, body


def _http_json(url, method="GET", token=None, payload=None, timeout=30, retry=True):
    """调用官方 GitHub HTTPS API；Token 只存在于请求头，不进日志、异常与响应。"""
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    attempts = 3 if retry else 1
    for attempt in range(attempts):
        if attempt:
            time.sleep(_RETRY_DELAYS[attempt - 1])
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
            raise _GitHubHttpError(exc.code, body)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < attempts - 1:
                continue
            fail("ONBOARDING_NETWORK", f"访问 GitHub 失败：{url}",
                 user_message="连接 GitHub 失败",
                 user_hint="请检查网络连接后重试；如需代理，请先在本机配置好代理")


def _http_download(url, target):
    """流式下载官方安装包；整段下载有总时限，网络类错误按 2s/5s 有限重试。"""
    deadline = time.monotonic() + _DOWNLOAD_DEADLINE
    for attempt in range(3):
        if attempt:
            time.sleep(_RETRY_DELAYS[attempt - 1])
        if time.monotonic() >= deadline:
            break
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
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


class Onboarding:
    """备份配置引导服务；安装、显式登录与最终应用复用 manager.operation() 互斥。"""

    def __init__(self, manager):
        self.manager = manager

    # ---------- 专用临时目录 ----------

    def _root(self):
        # 引导下载与候选探测放在项目根目录 .XMTEMP 下，与安装、探测任务彼此分开。
        root = config.BASE_DIR / ".XMTEMP" / "sync-onboarding"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _work_dir(self, label):
        work = self._root() / label / uuid.uuid4().hex
        work.mkdir(parents=True)
        return work

    @staticmethod
    def _spawn(argv, env=None, timeout=30, data=None):
        """后台子进程：隐藏窗口、默认不给标准输入，避免任何交互式提示挂起。"""
        return subprocess.run(argv, input=data, capture_output=True, timeout=timeout,
                              stdin=None if data is not None else subprocess.DEVNULL,
                              env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # ---------- 环境检测 ----------

    def environment(self):
        system, machine = platform.system(), platform.machine()
        result = {
            "os": system, "arch": machine,
            "one_click_install": system == "Windows" and machine in ("AMD64", "x86_64"),
            "git": {"available": False, "version": None, "path": None},
            "gcm": {"available": False, "version": None},
        }
        git_exe = gitstore.locate_git()
        if git_exe:
            result["git"] = {"available": True, "version": self._first_line([git_exe, "--version"]),
                             "path": None if git_exe == "git" else git_exe}
            prefix = gitstore.locate_gcm()
            if prefix:
                result["gcm"] = {"available": True, "version": self._first_line(prefix + ["--version"])}
        return result

    @classmethod
    def _first_line(cls, argv, env=None):
        try:
            result = cls._spawn(argv, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        text = result.stdout.decode("utf-8", "replace").strip()
        return text.splitlines()[0].strip() if text else ""

    # ---------- 程序内安装 ----------

    def install(self, component):
        if component not in ("git", "gcm"):
            fail("ONBOARDING_INVALID", "component 必须是 git 或 gcm",
                 user_message="要安装的组件不明确",
                 user_hint="请在备份配置向导中重新操作")
        with self.manager.operation():
            env = self.environment()
            # 收到安装请求后再次检测：组件已可用就直接返回结果，不重复执行安装器。
            if env[component]["available"]:
                return env
            if component == "gcm" and not env["git"]["available"]:
                fail("ONBOARDING_INSTALL_PRE", "安装登录组件前需要先安装 Git",
                     user_message="还需要先准备好 Git",
                     user_hint="请先在上一步安装 Git，再安装登录组件")
            if not env["one_click_install"]:
                fail("ONBOARDING_INSTALL_UNSUPPORTED", "当前系统或架构不支持程序内一键安装",
                     user_message="当前系统暂不支持自动安装",
                     user_hint="请从官网手动安装后，再点击「重新检测」")
            if component == "git":
                url = self._asset_url("https://api.github.com/repos/git-for-windows/git/releases/latest",
                                      _GIT_ASSET_PATTERN)
                # Git for Windows 安装器为用户级最低权限：静默安装不请求管理员提升。
                target = Path(os.environ.get("LOCALAPPDATA", str(config.BASE_DIR))) / "Programs" / "Git"
                args = ["/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-", "/DIR=" + str(target)]
            else:
                url = self._asset_url("https://api.github.com/repos/git-ecosystem/git-credential-manager/releases/latest",
                                      _GCM_ASSET_PATTERN)
                # gcmuser 用户级安装包：无需管理员，安装完成会自动执行凭据组件 configure。
                args = ["/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-"]
            work = self._work_dir("install")
            try:
                installer = work / Path(url).name
                _http_download(url, installer)
                try:
                    code = self._spawn([str(installer), *args], timeout=_INSTALLER_TIMEOUT).returncode
                except (OSError, subprocess.TimeoutExpired):
                    code = None
            finally:
                remove_tree(work, self._root())
            if code not in (0, 3010):
                fail("ONBOARDING_INSTALL_FAILED", f"安装器未成功完成（退出码 {code}）",
                     user_message="安装没有成功",
                     user_hint="请重试一次；若系统策略阻止安装，请从官网手动安装后再点击「重新检测」")
            # 安装完成后重新定位，避免应用进程继承的旧 PATH 造成「安装成功但仍无法使用」。
            gitstore.locate_git(refresh=True)
            gitstore.locate_gcm(refresh=True)
            new_env = self.environment()
            if not new_env[component]["available"]:
                fail("ONBOARDING_INSTALL_FAILED", "安装完成但组件仍不可用",
                     user_message="安装没有成功",
                     user_hint="请重试一次，或从官网手动安装后点击「重新检测」")
            return new_env

    @staticmethod
    def _asset_url(releases_url, pattern):
        try:
            release = _http_json(releases_url, timeout=20)
        except _GitHubHttpError as exc:
            fail("ONBOARDING_RELEASE_UNAVAILABLE", f"获取官方发布信息失败（HTTP {exc.status}）：{releases_url}",
                 user_message="获取官方安装包信息失败",
                 user_hint="请检查网络后重试，或从官网手动安装")
        if isinstance(release, dict):
            for asset in release.get("assets", []):
                name = asset.get("name", "") if isinstance(asset, dict) else ""
                if pattern.fullmatch(name) and asset.get("browser_download_url"):
                    return asset["browser_download_url"]
        fail("ONBOARDING_RELEASE_UNAVAILABLE", f"官方最新发布中没有匹配的安装包：{releases_url}",
             user_message="没有找到适合本机的官方安装包",
             user_hint="请从官网手动安装后，再点击「重新检测」")

    # ---------- GitHub 账号 ----------

    def github_status(self, account=None):
        prefix = gitstore.locate_gcm()
        result = {"gcm_available": bool(prefix), "accounts": [], "selected": None,
                  "verified": False, "verified_as": None, "error": None}
        if not prefix:
            return result
        result["accounts"] = self._github_accounts()
        if account:
            if not isinstance(account, str) or account not in result["accounts"]:
                fail("ONBOARDING_GITHUB_ACCOUNT", f"账号 {account!r} 不在本机已保存的 GitHub 账号中",
                     user_message="选择的账号还没有在本机登录",
                     user_hint="请选择已登录的账号，或点击「登录 GitHub」")
            result["selected"] = account
            try:
                result["verified_as"] = self._github_verify(account)
                result["verified"] = True
            except ApiError as exc:
                # 凭据缺失、失效或网络波动作为阶段性结果返回，由向导引导重新登录。
                result["error"] = error_dict(exc.code, exc.message, exc.hint, exc.user_message, exc.user_hint)
        return result

    def _github_accounts(self):
        prefix = gitstore.locate_gcm()
        if not prefix:
            return []
        try:
            result = self._spawn(prefix + ["github", "list"], timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        # 命令失败与真正的空账号列表是两回事：失败必须报错，否则已登录也会显示为空。
        if result is None or result.returncode:
            fail("ONBOARDING_GCM_FAILED", "读取本机已登录的 GitHub 账号失败",
                 user_message="暂时读不到已登录的 GitHub 账号",
                 user_hint="请重新安装登录组件，或重新点击「登录 GitHub」")
        text = result.stdout.decode("utf-8", "replace")
        # 输出除账号行外还有说明文字；只取整行形如 GitHub 登录名的条目。
        return sorted({line.strip() for line in text.splitlines()
                       if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", line.strip())})

    def github_login(self, account, force):
        with self.manager.operation():
            prefix = gitstore.locate_gcm()
            if not prefix:
                fail("ONBOARDING_GCM_MISSING", "本机还没有可用的登录组件（Git Credential Manager）",
                     user_message="还没有安装登录组件",
                     user_hint="请先在向导中安装登录组件，再登录 GitHub")
            env = os.environ.copy()
            prepends = []
            git_exe = gitstore.locate_git()
            if git_exe and git_exe != "git":
                prepends.append(str(Path(git_exe).parent))
            extra = gitstore.gcm_path_dir()
            if extra:
                prepends.append(extra)
            if prepends:
                env["PATH"] = os.pathsep.join(prepends) + os.pathsep + env.get("PATH", "")
            env["GIT_TERMINAL_PROMPT"] = "0"
            args = prefix + ["github", "login", "--browser"]
            if account:
                args += ["--username", account]
            if force:
                args += ["--force"]
            # 显式登录是唯一允许 GCM 打开浏览器的入口；授权页由系统浏览器呈现，程序内不代理网页。
            try:
                result = self._spawn(args, env=env, timeout=_LOGIN_TIMEOUT)
            except subprocess.TimeoutExpired:
                fail("ONBOARDING_GITHUB_LOGIN_TIMEOUT", "浏览器授权在限定时间内未完成",
                     user_message="这次登录没有完成",
                     user_hint="请重试并留意浏览器中的 GitHub 授权页面，完成后回到本页")
            except OSError:
                fail("ONBOARDING_GITHUB_LOGIN_FAILED", "无法启动登录组件",
                     user_message="这次登录没有完成",
                     user_hint="请重试一次；若持续失败，请重新安装登录组件")
            if result.returncode:
                fail("ONBOARDING_GITHUB_LOGIN_FAILED", "浏览器授权未完成或被拒绝",
                     user_message="这次登录没有完成",
                     user_hint="请在浏览器中完成 GitHub 授权；若浏览器没有打开或已关闭授权页，请重试")
            return self.github_status(account)

    def _github_token(self, account):
        """通过 Git 凭据协议非交互读取指定账号的凭据；Token 只留在本进程内存。"""
        env, git_exe = gitstore.git_env()
        if not git_exe:
            fail("GIT_UNAVAILABLE", "Git 不可用，无法读取本机凭据",
                 user_message="无法使用本机的版本管理工具",
                 user_hint="请先完成 Git 的安装或检测")
        query = f"protocol=https\nhost=github.com\nusername={account}\n\n".encode("utf-8")
        # 与 GitStore 的 GitHub HTTPS 命令固定同一 GCM helper：清空继承链后只走 manager，
        # 避免系统 helper 配置与 GCM 不一致时，登录成功却读取不到凭据或推送失败。
        try:
            result = self._spawn([git_exe, "-c", "credential.helper=",
                                  "-c", "credential.https://github.com.helper=manager",
                                  "credential", "fill"], env=env, timeout=30, data=query)
        except (OSError, subprocess.TimeoutExpired):
            fail("ONBOARDING_GITHUB_CREDENTIAL", "读取本机 GitHub 凭据失败",
                 user_message="暂时读不到 GitHub 登录信息",
                 user_hint="请重试一次；若持续失败，请在本步骤重新登录")
        token = None
        if result.returncode == 0:
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                if line.startswith("password="):
                    token = line[len("password="):].strip()
                    break
        if not token:
            fail("ONBOARDING_GITHUB_CREDENTIAL", f"本机没有账号 {account} 的可用 GitHub 凭据",
                 user_message="这个账号还没有可用的登录状态",
                 user_hint="请点击「登录 GitHub」完成授权后再试")
        return token

    def _github_verify(self, account):
        """凭据存在不等于仍有效：用官方 GET /user 确认实际登录身份。"""
        token = self._github_token(account)
        try:
            profile = _http_json(GITHUB_API + "/user", token=token, timeout=20)
        except _GitHubHttpError as exc:
            if exc.status == 401:
                fail("ONBOARDING_GITHUB_CREDENTIAL", f"账号 {account} 的凭据已被 GitHub 拒绝",
                     user_message="这个账号的登录状态已经失效",
                     user_hint="请点击「重新登录」完成一次新的授权")
            fail("ONBOARDING_GITHUB_VERIFY", f"验证 GitHub 账号失败（HTTP {exc.status}）",
                 user_message="暂时无法确认 GitHub 登录状态",
                 user_hint="请稍后重试；若持续出现，请重新登录")
        login = profile.get("login") if isinstance(profile, dict) else None
        if not login or str(login).lower() != str(account).lower():
            fail("ONBOARDING_GITHUB_CREDENTIAL", f"凭据对应的账号与所选账号不一致（{account}）",
                 user_message="登录状态与所选账号不一致",
                 user_hint="请重新选择账号，或点击「重新登录」")
        return login

    # ---------- 候选草稿 ----------

    def _draft(self, payload):
        mode = payload.get("mode")
        if mode not in ("existing", "github-new"):
            fail("ONBOARDING_INVALID", "mode 必须是 existing 或 github-new",
                 user_message="备份方式选择有误",
                 user_hint="请回到「选择备份方式」重新选择")
        settings = self.manager.settings
        device = payload.get("device", settings["device"])
        interval = payload.get("interval", settings["interval"])
        auto = payload.get("auto", settings["auto"])
        if type(interval) is not int or interval < 1 or type(auto) is not bool:
            fail("SETTINGS_INVALID", "自动同步间隔必须是至少 1 分钟的整数",
                 user_message="自动同步间隔至少为 1 分钟",
                 user_hint="请填写一个不小于 1 的整数")
        if not isinstance(device, str) or not 1 <= len(device.strip()) <= 80:
            fail("SETTINGS_INVALID", "设备名称长度应为 1–80 个字符",
                 user_message="设备名称需为 1–80 个字符",
                 user_hint="请缩短或补全设备名称")
        github_account = payload.get("github_account") or ""
        if not isinstance(github_account, str) or len(github_account) > 100:
            fail("ONBOARDING_INVALID", "github_account 必须是不超过 100 字符的文本",
                 user_message="GitHub 账号填写有误",
                 user_hint="请回到「登录 GitHub」步骤重新选择账号")
        draft = {"mode": mode, "device": device.strip(), "interval": interval, "auto": auto,
                 "github_account": github_account, "remote": "", "branch": "main", "repository": None}
        if mode == "existing":
            remote = payload.get("remote", "")
            if not isinstance(remote, str) or not remote.strip():
                fail("SETTINGS_INVALID", "仓库地址必填",
                     user_message="请填写仓库地址",
                     user_hint="请填写以 https:// 或 git@ 开头的仓库地址")
            remote = remote.strip()
            GitStore.validate_remote(remote, self.manager.allow_file)
            branch = payload.get("branch", settings["branch"])
            if not isinstance(branch, str) or not branch.strip() or branch.strip().startswith("-"):
                fail("SETTINGS_INVALID", "分支名无效",
                     user_message="分支名无效",
                     user_hint="请填写一个合法的分支名，例如 main")
            draft["remote"], draft["branch"] = remote, branch.strip()
            parts = urlsplit(remote)
            if not (parts.scheme == "https" and parts.hostname == "github.com"):
                draft["github_account"] = ""
        else:
            repository = payload.get("repository")
            if not isinstance(repository, str) or not _REPO_NAME_PATTERN.fullmatch(repository):
                fail("ONBOARDING_REPO_NAME", "仓库名称只能包含字母、数字、点、下划线和连字符",
                     user_message="仓库名称格式不正确",
                     user_hint="请使用字母或数字开头，只含字母、数字、点、下划线或连字符")
            draft["repository"] = repository
            if not draft["github_account"]:
                fail("ONBOARDING_GITHUB_ACCOUNT", "创建 GitHub 仓库前需要先选择已验证的账号",
                     user_message="还没有选择 GitHub 账号",
                     user_hint="请回到「登录 GitHub」步骤选择已验证的账号")
        return draft

    # ---------- 只读候选检查 ----------

    def check(self, payload):
        draft = self._draft(payload)
        if draft["mode"] == "github-new":
            owner = self._github_verify(draft["github_account"])
            self._assert_repo_free(owner, draft["repository"])
            draft["remote"] = f"https://github.com/{owner}/{draft['repository']}.git"
            return {**draft, "owner": owner, "remote_tip": None, "snapshot": None, "sources": ["local"]}
        tip, snapshot_info, sources = None, None, ["local"]
        work = self._work_dir("check")
        try:
            store = GitStore(work / "repo", draft["remote"], draft["branch"],
                             allow_file=self.manager.allow_file, github_account=draft["github_account"])
            tip = store.fetch()
            if tip:
                # 能连接 Git 不等于能恢复数据：导出到独立目录后按真实快照校验。
                store.checkout_snapshot(tip, work / "snapshot")
                manifest = snapshot.validate_files(work / "snapshot")
                snapshot_info = {"dataset": manifest["dataset"]}
                sources = ["remote", "local"]
        finally:
            remove_tree(work, self._root())
        return {**draft, "remote_tip": tip, "snapshot": snapshot_info, "sources": sources}

    def _assert_repo_free(self, owner, repository):
        try:
            _http_json(f"{GITHUB_API}/repos/{owner}/{repository}", timeout=15)
        except _GitHubHttpError as exc:
            if exc.status == 404:
                return
            fail("ONBOARDING_REPO_CHECK", f"检查同名仓库失败（HTTP {exc.status}）",
                 user_message="暂时无法确认仓库名称是否可用",
                 user_hint="请稍后重试")
        fail("ONBOARDING_REPO_EXISTS", f"账号 {owner} 下已存在同名仓库 {repository}",
             user_message="这个仓库名已经被使用",
             user_hint="请换一个仓库名称，或改用「连接已有仓库」的方式")

    # ---------- 最终确认 ----------

    def complete(self, payload):
        if payload.get("confirm") is not True:
            raise ApiError("CONFIRM_REQUIRED", "缺少最终确认", status=409,
                           user_message="请先确认后再执行",
                           user_hint="只有明确确认后，最后一步才会应用配置")
        draft = self._draft(payload)
        manager = self.manager
        expected_remote, expected_branch = payload.get("expected_remote"), payload.get("expected_branch")
        if not isinstance(expected_remote, str) or not isinstance(expected_branch, str):
            fail("ONBOARDING_INVALID", "缺少打开向导时的 expected_remote / expected_branch",
                 user_message="向导状态不完整",
                 user_hint="请关闭向导，刷新页面后重新打开")
        previously_ready = db.business_ready
        with manager.operation():
            current = (manager.settings["remote"], manager.settings["branch"])
            if current != (expected_remote, expected_branch):
                fail("ONBOARDING_STALE", "向导打开后同步配置已发生变化，请关闭向导重新打开",
                     user_message="同步配置在向导打开后发生了变化",
                     user_hint="请关闭本向导，刷新页面后重新打开")
            # 只有连接已有仓库、地址分支都未变且已完成过首次备份时才走「仅改设置」；
            # 首次备份从未完成时，即使地址未变也必须完整走一遍绑定流程，不能报告完成。
            same_target = draft["mode"] == "existing" and (draft["remote"], draft["branch"]) == current \
                and manager.state.get("baseline")
            if same_target:
                self._apply_settings_only(draft)
                return {"completed": True, "settings_applied": True, "created_repository_url": None,
                        "error": None, "status": manager.status()}
            # 实际绑定阶段：先关闭业务写入，再做守卫检查与联网探测，避免检查窗口内的新写入
            # 被随后的恢复或初始化覆盖；中途失败时恢复原门禁。同步任务锁不能代替业务门禁。
            db.set_ready(False)
            try:
                self._guard_rebind(manager)
                if draft["mode"] == "github-new":
                    self._github_verify(draft["github_account"])
                    self._assert_repo_free(draft["github_account"], draft["repository"])
                else:
                    tip = self._probe_tip(draft, payload)
                    self._require_source(draft, payload, tip)
            except ApiError:
                db.set_ready(previously_ready)
                raise
            created_url = None
            if draft["mode"] == "github-new":
                # 副作用从这里开始：之后不再以异常形式报错，改用阶段性结果返回。
                try:
                    created_url, clone_url = self._create_repository(draft["github_account"], draft["repository"])
                except ApiError as exc:
                    db.set_ready(previously_ready)
                    return {"completed": False, "settings_applied": False, "created_repository_url": None,
                            "error": error_payload(exc), "status": manager.status()}
                # 使用 API 返回的标准 HTTPS clone 地址，不自行拼接。
                draft["remote"] = clone_url
            values = {"remote": draft["remote"], "branch": draft["branch"], "device": draft["device"],
                      "interval": draft["interval"], "auto": draft["auto"],
                      "github_account": draft["github_account"]}
            try:
                manager._settings_update_locked(values)
            except ApiError as exc:
                db.set_ready(previously_ready)
                return {"completed": False, "settings_applied": False, "created_repository_url": created_url,
                        "error": error_payload(exc), "status": manager.status()}
            try:
                source = "local" if draft["mode"] == "github-new" else payload.get("source")
                manager._initialize_body(source)
            except ApiError as exc:
                manager._record_initialize_failure(exc, previously_ready)
                return {"completed": False, "settings_applied": True, "created_repository_url": created_url,
                        "error": error_payload(exc), "status": manager.status()}
            return {"completed": True, "settings_applied": True, "created_repository_url": created_url,
                    "error": None, "status": manager.status()}

    def _guard_rebind(self, manager):
        """换库前的硬性门槛：待上传提交、冲突草稿与尚未捕获成提交的本地修改都不允许绕过。"""
        if any(manager.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
            fail("SYNC_PENDING", "请先同步未上传提交或处理冲突，再更换数据仓库",
                 user_message="还有没有上传的改动或待处理的冲突",
                 user_hint="请先完成同步或处理完冲突，再更换仓库")
        # 不能只看 pending 数量：新编辑可能尚未生成 Git 提交，用快照内容对比。
        # 与最近一次已确认上传/恢复的基线比较；committed_hash 只在本机提交时更新，
        # 从远端恢复后的干净数据用它判断会误报未上传。
        if manager.state.get("baseline") and manager.active:
            digest = manager.capture()
            if digest and digest != manager.state.get("baseline"):
                fail("SYNC_UNPUSHED", "本地快照与最近一次同步的基线不一致，请先完成同步再更换数据仓库",
                     user_message="本机还有没有上传的修改",
                     user_hint="请先点击「立即同步」上传本地修改，再更换仓库")

    def _probe_tip(self, draft, payload):
        """最终应用前重新探测远端：与用户检查时看到的提交一致才允许继续。"""
        if "remote_tip" not in payload:
            fail("ONBOARDING_INVALID", "缺少候选检查结果（remote_tip）",
                 user_message="向导状态不完整",
                 user_hint="请回到「检查并确认」重新检查")
        work = self._work_dir("probe")
        try:
            store = GitStore(work / "repo", draft["remote"], draft["branch"],
                             allow_file=self.manager.allow_file, github_account=draft["github_account"])
            tip = store.fetch()
        finally:
            remove_tree(work, self._root())
        if tip != payload.get("remote_tip"):
            fail("ONBOARDING_REMOTE_CHANGED", "远端内容在检查后发生了变化，请重新检查",
                 user_message="远端又有新的变化",
                 user_hint="请回到「检查并确认」重新检查后再继续")
        return tip

    @staticmethod
    def _require_source(draft, payload, tip):
        source = payload.get("source")
        if tip is None:
            # 空仓库只允许用本机数据开始；远端出现内容的变化已被提交一致性检查拦下。
            if source != "local":
                fail("ONBOARDING_SOURCE_REQUIRED", "该仓库为空，只能使用本机数据开始备份",
                     user_message="这个仓库还是空的",
                     user_hint="请选择「使用本机数据开始备份」")
            return
        if source not in ("remote", "local"):
            fail("ONBOARDING_SOURCE_REQUIRED", "远端已有数据，必须明确选择从远端恢复或使用本机数据",
                 user_message="请选择使用哪一侧的数据",
                 user_hint="请在检查结果中选择「从远端恢复」或「使用本机数据」")

    def _apply_settings_only(self, draft):
        """地址与分支都未变：只保存确实修改的辅助设置，不触碰工作树与基线。"""
        settings = self.manager.settings
        values = {key: draft[key] for key in ("device", "interval", "auto") if draft[key] != settings[key]}
        if draft["github_account"] != settings.get("github_account", ""):
            values["github_account"] = draft["github_account"]
        if values:
            self.manager._settings_update_locked(values)

    def _create_repository(self, account, repository):
        """POST /user/repos：固定私有、不初始化；不自动重试，避免重复建库。"""
        token = self._github_token(account)
        try:
            created = _http_json(f"{GITHUB_API}/user/repos", method="POST", token=token,
                                 payload={"name": repository, "private": True, "auto_init": False},
                                 timeout=30, retry=False)
        except _GitHubHttpError as exc:
            if exc.status == 422:
                fail("ONBOARDING_REPO_EXISTS", f"创建失败：账号下已存在同名仓库 {repository}",
                     user_message="这个仓库名已经被使用",
                     user_hint="请检查 GitHub 上的同名仓库；可改用「连接已有仓库」方式，或换一个名称")
            if exc.status == 401:
                fail("ONBOARDING_GITHUB_FORBIDDEN", "GitHub 拒绝了创建仓库请求（登录状态或权限不足）",
                     user_message="GitHub 拒绝了创建仓库的请求",
                     user_hint="请重新登录 GitHub 后重试")
            fail("ONBOARDING_REPO_CREATE", f"创建仓库失败（HTTP {exc.status}）",
                 user_message="创建 GitHub 仓库失败",
                 user_hint="请稍后重试；若提示名称冲突，请换一个仓库名称")
        clone_url = created.get("clone_url") if isinstance(created, dict) else None
        if not clone_url or not created.get("private"):
            fail("ONBOARDING_REPO_CREATE", "创建仓库的响应不完整",
                 user_message="创建 GitHub 仓库的结果异常",
                 user_hint="请到 GitHub 确认仓库是否已创建，再回到向导继续")
        return created.get("html_url") or clone_url, clone_url
