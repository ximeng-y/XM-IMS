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
from ..sync import gitstore, snapshot
from ..sync.common import fail, read_json, remove_tree
from ..sync.gitstore import GitStore

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


class BackupOnboarding:
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

    # ---------- 目标驱动的新手流程；旧 check/complete 保持原契约 ----------

    def _flow_draft(self, payload):
        intent = payload.get("intent")
        if intent not in ("backup", "restore"):
            fail("ONBOARDING_INTENT", "intent 必须为 backup 或 restore",
                 user_message="请选择配置备份或使用已有备份")
        draft = self._draft(payload)
        source = "local" if intent == "backup" else "remote"
        if payload.get("source", source) != source or (intent == "restore" and draft["mode"] != "existing"):
            fail("ONBOARDING_SOURCE_INVALID", "新手流程的 source/mode 与 intent 不一致",
                 user_message="请重新选择要使用的备份仓库")
        return {**draft, "intent": intent, "source": source}

    @staticmethod
    def _write_identity(payload):
        if type(payload.get("dry_run", False)) is not bool:
            fail("ONBOARDING_INVALID", "dry_run 必须为布尔值",
                 user_message="请刷新页面后重试")
        if payload.get("confirm") is not True and payload.get("dry_run") is not True:
            fail("CONFIRM_REQUIRED", "缺少最终确认",
                 user_message="请确认后继续")
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or not 1 <= len(key) <= 100:
            fail("ONBOARDING_INVALID", "idempotency_key 需为 1–100 字符",
                 user_message="请刷新页面后重新确认")
        if not isinstance(payload.get("expected_remote"), str) or not isinstance(payload.get("expected_branch"), str):
            fail("ONBOARDING_INVALID", "缺少 expected_remote/expected_branch",
                 user_message="请刷新页面后重新打开配置")
        if "remote_tip" not in payload:
            fail("ONBOARDING_INVALID", "缺少 remote_tip",
                 user_message="请先检查备份仓库")

    def _require_first_shop(self):
        from ..service import shops
        with db._coordinator:
            if not self.manager.active or not config.INDEX_DB.is_file():
                fail("FIRST_USE_SHOP_REQUIRED", "备份前需建立活动数据与首店",
                     user_message="请先创建第一间店铺")
            with db.internal_access():
                if not shops.list_shops()["shops"]:
                    fail("FIRST_USE_SHOP_REQUIRED", "备份前需创建首店",
                         user_message="请先创建第一间店铺")

    def _require_restore(self):
        from ..service import shops
        manager = self.manager
        if any(manager.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
            fail("ONBOARDING_MAINTENANCE_REQUIRED", "新手恢复拒绝未上传提交或冲突状态",
                 user_message="请先处理现有改动",
                 user_hint="进入数据同步页面完成同步或处理冲突")
        with db._coordinator:
            if manager.active and config.INDEX_DB.is_file():
                with db.internal_access():
                    if shops.list_shops()["shops"]:
                        fail("ONBOARDING_MAINTENANCE_REQUIRED", "新手恢复不能替换已有店铺",
                             user_message="本机已有可用店铺",
                             user_hint="如需替换，请进入数据同步页面的「高级维护」")

    def _repository_info(self, account, repository):
        """结果不确定时用选中账号的凭据读取私有仓库，不用匿名 404 判断创建结果。"""
        owner = self._github_verify(account)
        token = self._github_token(account)
        try:
            info = _http_json(f"{GITHUB_API}/repos/{owner}/{repository}", token=token, timeout=20)
        except _GitHubHttpError as exc:
            if exc.status == 404:
                return None
            fail("ONBOARDING_REPO_CHECK", f"读取创建结果失败（HTTP {exc.status}）",
                 user_message="暂时无法确认仓库创建结果",
                 user_hint="请稍后点击「检查此仓库」")
        actual_owner = info.get("owner", {}).get("login", "")
        if str(actual_owner).lower() != owner.lower() or str(info.get("name", "")).lower() != repository.lower() \
                or not info.get("private") or not info.get("clone_url"):
            fail("ONBOARDING_REPO_CHECK", "仓库所有者、名称或私有性与本次目标不符",
                 user_message="这个仓库与本次创建目标不一致",
                 user_hint="请确认所选账号和私有仓库名称")
        GitStore.validate_remote(info["clone_url"], self.manager.allow_file)
        return {"remote": info["clone_url"], "url": info.get("html_url") or info["clone_url"]}

    def _snapshot_summary(self, root, manifest, store, tip):
        shop_keys = {item["key"] for item in manifest["databases"] if item["kind"] == "shop"}
        summary = []
        for row in snapshot.iter_rows(root, "index", "shops"):
            key = row["sync_uid"]
            if key not in shop_keys:
                fail("SNAPSHOT_INVALID", "店铺登记与数据库清单不一致",
                     user_message="备份中的店铺信息需要重新核对",
                     user_hint="请在已有数据的电脑上重新同步一次")
            tables = read_json(root / key / "schema.json")["tables"]
            summary.append({"name": row["name"], "sku_count": tables["items"]["rows"],
                            "movement_count": tables["movements"]["rows"]})
        return {"dataset": manifest["dataset"], "shop_count": len(summary), "shops": summary,
                "sku_count": sum(row["sku_count"] for row in summary),
                "movement_count": sum(row["movement_count"] for row in summary),
                "commit_time": store.text("show", "-s", "--format=%cI", tip)}

    def _inspect_flow(self, draft):
        work = self._work_dir("flow-check")
        try:
            store = GitStore(work / "repo", draft["remote"], draft["branch"],
                             allow_file=self.manager.allow_file, github_account=draft["github_account"])
            tip = store.fetch()
            info = None
            if tip:
                target = work / "snapshot"
                store.checkout_snapshot(tip, target)
                manifest = snapshot.validate_files(target)
                info = self._snapshot_summary(target, manifest, store, tip)
            return tip, info
        except (OSError, ValueError, KeyError, TypeError):
            fail("SNAPSHOT_INVALID", "候选快照摘要无法读取",
                 user_message="这份备份暂时无法读取",
                 user_hint="请确认仓库地址，或在已有数据的电脑上重新同步一次")
        finally:
            remove_tree(work, self._root())

    def _initial_upload(self, draft, store):
        """仅识别可关联到同一个初始提交的 journal；未知状态全部留给高级维护。"""
        manager, state = self.manager, self.manager.state
        if (draft["remote"], draft["branch"]) != (manager.settings["remote"], manager.settings["branch"]) \
                or state.get("baseline") or state.get("session") or state.get("requires_resolution"):
            return None
        journal = state.get("journal")
        if not journal:
            return None
        commit, digest = journal.get("commit"), journal.get("hash")
        if journal.get("apply") is not False or type(journal.get("confirmed")) is not bool \
                or not commit or not digest or state.get("committed_head") != commit \
                or state.get("committed_hash") != digest or state.get("pending") != [commit] \
                or store.head() != commit:
            return None
        if store.run("cat-file", "-e", commit + "^{commit}", check=False).returncode:
            return None
        # 首次空库上传只有一个根提交，拒绝把别的历史或额外 pending 当成它。
        if len(store.text("rev-list", "--parents", "-n", "1", commit).split()) != 1:
            return None
        work = self._work_dir("resume-verify")
        try:
            store.checkout_snapshot(commit, work / "snapshot")
            snapshot.validate_files(work / "snapshot")
            if snapshot.sha(work / "snapshot" / "manifest.json") != digest:
                return None
            manager._snapshot_source(digest)
            return dict(journal)
        finally:
            remove_tree(work, self._root())

    def _check_flow_draft(self, draft):
        manager = self.manager
        if draft["intent"] == "restore":
            self._require_restore()
        else:
            self._require_first_shop()
            same_target = draft["mode"] == "existing" and (draft["remote"], draft["branch"]) == (
                manager.settings["remote"], manager.settings["branch"])
            if not manager.state.get("journal") and any(manager.state.get(k) for k in (
                    "pending", "session", "requires_resolution")) and not (same_target and manager.state.get("baseline")):
                fail("ONBOARDING_MAINTENANCE_REQUIRED", "存在非首次上传的待处理状态",
                     user_message="请先处理现有同步任务",
                     user_hint="进入数据同步页面继续处理，现有改动已保留")
        if draft["mode"] == "github-new":
            owner = self._github_verify(draft["github_account"])
            draft = {**draft, "remote": f"https://github.com/{owner}/{draft['repository']}.git", "owner": owner}
            # 已绑定这个新目标且存在 journal 时先识别续接，不能误报为陌生数据仓库。
            current = (manager.settings["remote"], manager.settings["branch"])
            if (draft["remote"], draft["branch"]) != current or not manager.state.get("journal"):
                if self._repository_info(draft["github_account"], draft["repository"]):
                    fail("ONBOARDING_REPO_EXISTS", "创建目标已存在，请明确采用已有仓库",
                         user_message="这个仓库名已经被使用",
                         user_hint="请换一个名称，或选择「连接已有仓库」")
                return {**draft, "decision": "ready", "remote_tip": None, "snapshot": None}
        if draft["intent"] == "backup" and manager.state.get("journal"):
            store = manager.git()
            journal = self._initial_upload(draft, store)
            if journal:
                tip, info = self._inspect_flow(draft)
                if tip not in (None, journal["commit"]):
                    fail("ONBOARDING_REMOTE_CHANGED", "首次上传后远端已改为其他提交",
                         user_message="仓库里出现了新的变化",
                         user_hint="请进入数据同步页面重新核对，现有改动已保留")
                return {**draft, "decision": "resume_required", "remote_tip": tip, "snapshot": info}
            fail("ONBOARDING_MAINTENANCE_REQUIRED", "journal 不属于可续接的首次上传",
                 user_message="请先处理现有同步任务",
                 user_hint="进入数据同步页面继续处理")
        tip, info = self._inspect_flow(draft)
        same = (draft["remote"], draft["branch"]) == (manager.settings["remote"], manager.settings["branch"])
        if draft["intent"] == "restore":
            decision = "ready" if info else "remote_empty"
        elif same and manager.state.get("baseline"):
            decision = "settings_only"
        else:
            decision = "remote_has_data" if info else "ready"
        return {**draft, "decision": decision, "remote_tip": tip, "snapshot": info}

    def check_flow(self, payload):
        return self._check_flow_draft(self._flow_draft(payload))

    def _flow_result(self, draft, *, completed=False, applied=False, created=None, error=None,
                     action="none", **extra):
        return {"completed": completed, "settings_applied": applied,
                "created_repository_url": created, "error": error_payload(error) if error else None,
                "status": self.manager.status(), "resume_action": action,
                "target_remote": draft.get("remote") or "", "target_branch": draft["branch"], **extra}

    def _apply_flow_settings_only(self, draft):
        self._apply_settings_only(draft)
        # 前次写设置失败时内存可能已更新；再次确认后仍需保证辅助设置确实落盘。
        saved = read_json(self.manager.directory / "settings.json", {})
        keys = ("device", "interval", "auto", "github_account")
        if any(saved.get(key) != draft[key] for key in keys):
            self.manager._settings_update_locked({key: draft[key] for key in keys})

    @staticmethod
    def _storage_error():
        return ApiError("SYNC_STORAGE_FAILED", "引导保存或初始化数据失败", status=409,
                        user_message="数据保存暂未完成",
                        user_hint="请确认磁盘空间和目录访问正常后重试")

    @staticmethod
    def _require_decision(check):
        if check["decision"] == "remote_has_data":
            fail("ONBOARDING_REMOTE_HAS_DATA", "新手备份拒绝覆盖有效远端数据",
                 user_message="这个仓库已有一份备份",
                 user_hint="请选择其他仓库；如需采用这份备份，请明确选择恢复")
        if check["decision"] == "remote_empty":
            fail("REMOTE_EMPTY", "恢复来源没有有效快照",
                 user_message="这个仓库尚无可恢复备份",
                 user_hint="请确认仓库地址，或先在已有数据的电脑上完成备份")

    def complete_flow(self, payload):
        self._write_identity(payload)
        draft = self._flow_draft(payload)
        manager = self.manager
        if payload.get("dry_run") is True:
            if (manager.settings["remote"], manager.settings["branch"]) != (
                    payload["expected_remote"], payload["expected_branch"]):
                fail("ONBOARDING_STALE", "dry_run 时已保存目标发生变化",
                     user_message="备份设置已更新，请刷新页面")
            check = self.check_flow(payload)
            self._require_decision(check)
            if check["remote_tip"] != payload["remote_tip"]:
                fail("ONBOARDING_REMOTE_CHANGED", "dry_run 候选提交发生变化",
                     user_message="备份内容有新的变化，请重新检查")
            if check["decision"] not in ("settings_only", "resume_required"):
                if any(manager.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")) \
                        or (manager.state.get("baseline") and manager.status()["dirty"]):
                    fail("SYNC_PENDING", "dry_run 时存在待处理的本机改动",
                         user_message="请先完成现有数据的同步")
            return self._flow_result(check, check=check, dry_run=True)
        previously_ready = db.business_ready
        with manager.operation():
            db.set_ready(False)
            created, applied = None, False
            try:
                if (manager.settings["remote"], manager.settings["branch"]) != (
                        payload["expected_remote"], payload["expected_branch"]):
                    fail("ONBOARDING_STALE", "当前配置与打开流程时不一致",
                         user_message="备份设置已更新",
                         user_hint="请刷新页面后继续配置")
                check = self._check_flow_draft(draft)
                draft = check
                self._require_decision(check)
                if check["remote_tip"] != payload["remote_tip"]:
                    fail("ONBOARDING_REMOTE_CHANGED", "远端提交与检查结果不一致",
                         user_message="备份内容有新的变化",
                         user_hint="请重新检查后确认")
                if check["decision"] == "resume_required":
                    db.set_ready(previously_ready)
                    return self._flow_result(draft, applied=True, action="retry_upload")
                if check["decision"] == "settings_only":
                    self._apply_flow_settings_only(draft)
                    db.set_ready(previously_ready)
                    return self._flow_result(draft, completed=True, applied=True)
                self._guard_rebind(manager)
                if draft["mode"] == "github-new":
                    try:
                        created, draft["remote"] = self._create_repository(draft["github_account"], draft["repository"])
                    except ApiError as exc:
                        db.set_ready(previously_ready)
                        return self._flow_result(draft, error=exc, action="recheck_remote")
                values = {key: draft[key] for key in (
                    "remote", "branch", "device", "interval", "auto", "github_account")}
                manager._settings_update_locked(values)
                applied = True
                manager._initialize_body(
                    draft["source"], checked_remote=(draft["remote"], draft["branch"], payload["remote_tip"]))
                return self._flow_result(draft, completed=True, applied=True, created=created)
            except (ApiError, OSError) as exc:
                error = exc if isinstance(exc, ApiError) else self._storage_error()
                # 设置文件可能已写成功、后续状态保存才失败；以磁盘读回而非返回码判断。
                try:
                    saved = read_json(manager.directory / "settings.json", {})
                    applied = applied or all(saved.get(key) == draft[key] for key in (
                        "remote", "branch", "device", "interval", "auto", "github_account"))
                except (OSError, ValueError):
                    pass
                manager._record_initialize_failure(error, previously_ready)
                db.set_ready(previously_ready)
                action = "retry_check" if error.code == "ONBOARDING_REMOTE_CHANGED" else \
                    "retry_upload" if applied and manager.state.get("journal") else \
                    "retry_initialize" if applied else "reuse_created" if created else "retry_check"
                return self._flow_result(draft, applied=applied, created=created, error=error, action=action)

    def resume_flow(self, payload):
        self._write_identity(payload)
        draft = self._flow_draft(payload)
        manager = self.manager
        target = payload.get("target_remote")
        if not isinstance(target, str) or not target:
            fail("ONBOARDING_INVALID", "续接缺少 target_remote",
                 user_message="请重新检查要使用的仓库")
        GitStore.validate_remote(target, manager.allow_file)
        # 不确定的创建结果只读核实，返回摘要后由用户再次确认采用，绝不自动再次 POST。
        if draft["mode"] == "github-new":
            info = self._repository_info(draft["github_account"], draft["repository"])
            owner = self._github_verify(draft["github_account"])
            expected = f"https://github.com/{owner}/{draft['repository']}.git"
            if target.lower() != expected.lower():
                fail("ONBOARDING_STALE", "创建续接目标与所选账号、仓库名不一致",
                     user_message="请核对本次选择的账号和仓库")
            if not info:
                return self._flow_result({**draft, "remote": expected}, action="retry_check",
                                         repository_exists=False)
            if info["remote"].lower() != target.lower():
                fail("ONBOARDING_STALE", "创建结果的 clone_url 与续接目标不一致",
                     user_message="请重新检查仓库地址")
            draft = {**draft, "mode": "existing", "remote": info["remote"]}
            created = info["url"]
        else:
            created = None
            if target != draft["remote"]:
                fail("ONBOARDING_STALE", "续接目标与草稿地址不一致",
                     user_message="仓库地址已更改，请重新检查")
        current = (manager.settings["remote"], manager.settings["branch"])
        identity = (draft["remote"], draft["branch"])
        opened = (payload["expected_remote"], payload["expected_branch"])
        if current != identity:
            if current != opened:
                fail("ONBOARDING_STALE", "续接期间配置已更换",
                     user_message="备份设置已更新，请刷新页面")
            check = self._check_flow_draft(draft)
            return self._flow_result(draft, created=created, action="reuse_created" if created else "retry_check",
                                     check=check, dry_run=payload.get("dry_run") is True)
        previously_ready = db.business_ready
        with manager.operation():
            if (manager.settings["remote"], manager.settings["branch"]) != current:
                fail("ONBOARDING_STALE", "取得锁后目标发生变化",
                     user_message="备份设置已更新，请刷新页面")
            if payload.get("dry_run") is not True:
                db.set_ready(False)
            try:
                tip, summary = self._inspect_flow(draft)
                if manager.state.get("baseline"):
                    # 恢复/上传已成功但界面没有收到回执：读回同一结果，不再次切代次。
                    if tip == manager.state.get("base_commit") and summary \
                            and manager.active and summary["dataset"] == manager.active["dataset"] \
                            and not any(manager.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
                        if (manager.error or {}).get("code") not in (None, "SYNC_STORAGE_FAILED", "GIT_FAILED", "GIT_UNAVAILABLE") \
                                or not config.INDEX_DB.is_file():
                            fail("ONBOARDING_MAINTENANCE_REQUIRED", "已有同步结果的数据可用性仍需处理",
                                 user_message="请继续处理本机数据",
                                 user_hint="进入数据同步页面查看当前状态")
                        if payload.get("dry_run") is not True:
                            self._apply_flow_settings_only(draft)
                            current_digest = manager.capture(force=True)
                            changed = current_digest != manager.state["baseline"]
                            manager.mark_synced(manager.state["baseline"], tip, compact=False)
                            if changed:
                                manager.invalidate_capture_cache()
                            manager.error = None
                            db.set_ready(True)
                        else:
                            changed = manager.status()["dirty"]
                        return self._flow_result(draft, completed=True, applied=True, created=created,
                                                 dry_run=payload.get("dry_run") is True,
                                                 local_changes_pending=changed)
                    fail("ONBOARDING_MAINTENANCE_REQUIRED", "已有基线或待处理状态与续接目标不一致",
                         user_message="备份已有新的变化",
                         user_hint="请进入数据同步页面继续处理")
                if draft["intent"] == "backup" and manager.state.get("journal"):
                    store = manager.git()
                    journal = self._initial_upload(draft, store)
                    if not journal:
                        fail("ONBOARDING_MAINTENANCE_REQUIRED", "首次上传 journal 关联校验失败",
                             user_message="请先处理现有同步任务",
                             user_hint="进入数据同步页面继续处理，现有改动已保留")
                    if tip not in (None, journal["commit"]):
                        fail("ONBOARDING_REMOTE_CHANGED", "远端不是空库或本次初始提交",
                             user_message="仓库里出现了新的变化",
                             user_hint="请进入数据同步页面重新核对")
                    if payload.get("dry_run") is True:
                        return self._flow_result(draft, applied=True, action="retry_upload", dry_run=True)
                    # 同目标只更新辅助设置；实际续传使用本次选中的账号，原提交与 journal 保留。
                    self._apply_flow_settings_only(draft)
                    store = manager.git()
                    # 只续传已确认的初始提交；失败期间新增业务数据留在本机，后续正常同步。
                    current_digest = manager.capture(force=True)
                    if tip is None:
                        store.push(journal["commit"])
                    manager.state["write_verified"] = True
                    manager.state["journal"]["confirmed"] = True
                    manager.save()
                    manager.mark_synced(journal["hash"], journal["commit"])
                    changed = current_digest != journal["hash"]
                    if changed:
                        manager.invalidate_capture_cache()
                    manager.error = None
                    db.set_ready(True)
                    return self._flow_result(draft, completed=True, applied=True, created=created,
                                             local_changes_pending=changed)
                if draft["intent"] == "restore":
                    self._require_restore()
                else:
                    self._require_first_shop()
                if any(manager.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
                    fail("ONBOARDING_MAINTENANCE_REQUIRED", "存在非首次上传的 pending/journal",
                         user_message="请先处理现有同步任务",
                         user_hint="进入数据同步页面继续处理")
                check = {**draft, "remote_tip": tip, "snapshot": summary,
                         "decision": ("ready" if summary else "remote_empty") if draft["intent"] == "restore"
                         else ("remote_has_data" if summary else "ready")}
                self._require_decision(check)
                if tip != payload["remote_tip"]:
                    if payload.get("dry_run") is not True:
                        db.set_ready(previously_ready)
                    return self._flow_result(draft, applied=True, created=created, action="retry_check", check=check)
                if payload.get("dry_run") is True:
                    return self._flow_result(draft, applied=True, check=check, dry_run=True)
                # 同目标保留绑定与工作仓，只应用本次账号等辅助设置，再核对实际获取的提交。
                self._apply_flow_settings_only(draft)
                manager._initialize_body(
                    draft["source"], checked_remote=(draft["remote"], draft["branch"], payload["remote_tip"]))
                return self._flow_result(draft, completed=True, applied=True, created=created)
            except (ApiError, OSError) as exc:
                error = exc if isinstance(exc, ApiError) else self._storage_error()
                if payload.get("dry_run") is not True:
                    manager._record_initialize_failure(error, previously_ready)
                    db.set_ready(previously_ready)
                action = "recheck_remote" if error.code in (
                    "ONBOARDING_REMOTE_CHANGED", "ONBOARDING_MAINTENANCE_REQUIRED",
                    "SNAPSHOT_INVALID", "SCHEMA_UNSUPPORTED", "GIT_BRANCH_MISSING", "BASE_MISSING") else \
                    "retry_upload" if manager.state.get("journal") else "retry_initialize"
                return self._flow_result(draft, applied=True, created=created, error=error, action=action)
