"""只操作专用数据仓库的 Git 适配；不执行文本自动合并。"""
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path
from urllib.parse import urlsplit
from .common import fail
from .. import config
from ..platform_tools import locations
from ..storage.files import remove_tree, replace_path


# ---------- Git / GCM 定位 ----------
# 进程继承的 PATH 可能不含新装组件；以实际执行 --version 成功为准定位，只补充子进程环境。
# 桌面模式只使用 locations 记录的安装目标（全局模式才走系统候选），源码模式保持原顺序。

# RLock：locate_gcm / gcm_path_dir 会在持锁状态下再进 locate_git，必须可重入。
_locate_lock = threading.RLock()
_git_exe = None        # 已验证可执行的 Git："git" 或绝对路径；None 表示尚未检出或不可用
_gcm_prefix = None     # 已验证可用的 GCM 命令前缀（list）
_gcm_path_dir = None   # 独立安装的 GCM 所在目录（需要补充进子进程 PATH 时使用）


def _exe_works(argv, timeout=15):
    try:
        result = subprocess.run(argv, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _git_in_dir(base):
    if not base:
        return
    for sub in ("cmd", "bin"):
        exe = Path(base) / sub / ("git.exe" if os.name == "nt" else "git")
        if exe.is_file():
            yield str(exe)


def _system_git_candidates():
    """系统级 Git 位置：注册表与标准安装目录；不遍历磁盘。"""
    if os.name != "nt":
        return
    import winreg
    # Git for Windows 安装时写入的注册表信息（HKCU / HKLM，兼顾 32/64 位视图）
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0)):
            try:
                with winreg.OpenKey(hive, r"Software\GitForWindows", 0, winreg.KEY_READ | view) as key:
                    install, _ = winreg.QueryValueEx(key, "InstallPath")
            except OSError:
                continue
            yield from _git_in_dir(install)
    # 标准用户级 / 系统级安装位置
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield from _git_in_dir(Path(local) / "Programs" / "Git")
    yield from _git_in_dir(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git")


def _git_candidates():
    if config.DESKTOP_MODE:
        selected = locations.mode()
        if selected == locations.MODE_GLOBAL:
            recorded = locations.global_git()
            if recorded:
                yield from _git_in_dir(recorded)
            yield from _system_git_candidates()
        else:
            # 局部 / 自选目标：只认这一处，不让系统里已装的 Git 顶替
            yield from _git_in_dir(locations.git_root(selected))
        return
    yield "git"
    yield from _system_git_candidates()


def locate_git(refresh=False):
    """定位可实际执行的 Git；组件安装后用 refresh=True 重新检测。"""
    global _git_exe
    with _locate_lock:
        if refresh or _git_exe is None:
            _git_exe = next((c for c in _git_candidates() if _exe_works([c, "--version"])), None)
        return _git_exe


def locate_git_from(base):
    """指定目录下是否有可执行的 Git；用于判断局部目标是否已完成安装。"""
    return next((c for c in _git_in_dir(base) if _exe_works([c, "--version"])), None)


def locate_system_git():
    """系统候选里第一个可执行的 Git；不看 locations 记录。"""
    return next((c for c in _system_git_candidates() if _exe_works([c, "--version"])), None)


def locate_gcm(refresh=False):
    """GCM 命令前缀：优先 Git 自带（git credential-manager），其次本实例选定的独立安装。"""
    global _gcm_prefix, _gcm_path_dir
    with _locate_lock:
        if refresh or _gcm_prefix is None:
            _gcm_prefix, _gcm_path_dir = None, None
            git_exe = locate_git()
            if git_exe and _exe_works([git_exe, "credential-manager", "--version"]):
                _gcm_prefix = [git_exe, "credential-manager"]
            elif os.name == "nt":
                for exe in _gcm_candidates():
                    if exe.is_file() and _exe_works([str(exe), "--version"]):
                        _gcm_prefix, _gcm_path_dir = [str(exe)], str(exe.parent)
                        break
        return _gcm_prefix


def _gcm_candidates():
    """独立安装的 GCM 位置：桌面模式只看选定目标，源码模式保留用户级安装位置。"""
    if config.DESKTOP_MODE:
        selected = locations.mode()
        if selected in (locations.MODE_LOCAL, locations.MODE_CUSTOM):
            root = locations.gcm_root(selected)
            if root:
                yield root / "git-credential-manager.exe"
        return
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "Programs" / "Git Credential Manager" / "git-credential-manager.exe"



def gcm_path_dir():
    """独立安装的 GCM 目录；git 调用凭据助手时需要它能出现在 PATH 中。"""
    locate_gcm()
    return _gcm_path_dir


def git_env():
    """子进程环境：补充定位到的 Git / GCM 目录到 PATH，并强制后台非交互。"""
    env = os.environ.copy()
    prepends = []
    git_exe = locate_git()
    if git_exe and git_exe != "git":
        prepends.append(str(Path(git_exe).parent))
    extra = gcm_path_dir()
    if extra:
        prepends.append(extra)
    if prepends:
        env["PATH"] = os.pathsep.join(prepends) + os.pathsep + env.get("PATH", "")
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never",
               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes")
    return env, git_exe


class GitStore:
    def __init__(self, root, remote, branch="main", device="本机", allow_file=False, github_account=""):
        self.root, self.remote, self.branch, self.device = Path(root), remote, branch, device
        self.allow_file = allow_file
        self.local_only = remote is None
        if self.local_only:
            # 本地历史仓库没有远端：不校验地址，也不设置 origin。
            self.github_user = ""
        else:
            self.validate_remote(remote, allow_file)
            # 只对 GitHub HTTPS 远端附加账号提示；其他平台与 SSH 沿用系统凭证 / 密钥。
            parts = urlsplit(remote)
            self.github_user = str(github_account or "") if parts.scheme == "https" and parts.hostname == "github.com" else ""
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self.run("init", "-b", branch)
        if self.run("check-ref-format", "--branch", branch, check=False).returncode or branch.startswith("-"):
            fail("GIT_BRANCH", "Git 分支名无效",
                 user_message="分支名无效", user_hint="请填写一个合法的分支名，例如 main")
        self.run("config", "user.name", "XM-IMS " + device)
        self.run("config", "user.email", "xm-ims@localhost")
        self.run("config", "core.autocrlf", "false")
        self.run("config", "core.hooksPath", str(self.root / "disabled-hooks"))
        if self.local_only:
            return
        if self.run("remote", "get-url", "origin", check=False).returncode:
            self.run("remote", "add", "origin", remote)
        else:
            self.run("remote", "set-url", "origin", remote)

    @classmethod
    def local(cls, root, branch="history", device="本机"):
        """无远端的本地历史仓库；fetch/push/compact 不适用于它。"""
        return cls(root, None, branch, device)

    @staticmethod
    def validate_remote(value, allow_file=False):
        if allow_file and (value.startswith("file://") or Path(value).is_absolute()):
            return
        if re.fullmatch(r"[\w.-]+@[\w.-]+:[\w./-]+", value):
            return
        parts = urlsplit(value)
        if parts.scheme not in ("https", "ssh") or not parts.hostname or parts.password or parts.query or parts.fragment:
            fail("GIT_URL", "请输入不含 Token 的 HTTPS 或 SSH 仓库地址",
                 user_message="仓库地址格式不正确",
                 user_hint="请填写 https:// 或 git@ 开头的仓库地址，地址里不要包含账号、密码或令牌")
        if parts.scheme == "https" and parts.username:
            fail("GIT_URL", "HTTPS URL 不允许内嵌用户名或访问令牌",
                 user_message="仓库地址里不能包含用户名或令牌",
                 user_hint="请填写不含账号信息的仓库地址，账号请交给本机的凭证管理")
        if any(c.isspace() for c in value):
            fail("GIT_URL", "仓库地址不能包含空白字符",
                 user_message="仓库地址不能包含空格",
                 user_hint="请检查地址中是否误输入了空格")

    def config_args(self):
        args = ["-c", "core.autocrlf=false", "-c", "core.hooksPath=" + str(self.root / "disabled-hooks")]
        if self.github_user:
            # 命令级指定 GitHub 账号，让探测、首次推送、后续拉取与收缩使用同一账号的已存凭据；
            # 只影响本进程子进程，不改系统级 Git 配置。同时清空继承的 helper 链后固定走
            # GCM（manager），保证与引导登录、凭据验证使用同一凭据来源；helper 名 manager
            # 与 locate_gcm 依赖同一套解析机制（exec-path / PATH 中的 git-credential-manager）。
            args += ["-c", "credential.helper=",
                     "-c", "credential.https://github.com.helper=manager",
                     "-c", "credential.https://github.com.username=" + self.github_user]
        return args

    def run(self, *args, check=True, data=None, cwd=None):
        env, git_exe = git_env()
        if not git_exe:
            fail("GIT_UNAVAILABLE", "Git 不可用或未安装，请检查安装、网络和系统凭证",
                 user_message="无法使用本机的版本管理工具",
                 user_hint="请确认已安装 Git、网络可用，且本机已保存仓库的登录凭证")
        # 不使用 shell，不把远端响应直接展示给用户，以免泄漏凭证。
        try:
            result = subprocess.run([git_exe, *self.config_args(), *args],
                                    cwd=cwd or self.root, input=data, capture_output=True, env=env, timeout=60,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            fail("GIT_UNAVAILABLE", "Git 不可用或操作超时，请检查安装、网络和系统凭证",
                 user_message="无法使用本机的版本管理工具",
                 user_hint="请确认已安装 Git、网络可用，且本机已保存仓库的登录凭证")
        if check and result.returncode:
            fail("GIT_FAILED", "Git 操作失败，请检查网络、凭证、写权限及远端是否已变化",
                 user_message="与远端仓库通信失败",
                 user_hint="请检查网络、本机保存的仓库凭证与仓库写入权限，并确认远端没有他人新的改动")
        return result

    def text(self, *args):
        return self.run(*args).stdout.decode("utf-8").strip()

    def head(self):
        result = self.run("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def has_commit(self, commit):
        """本地历史按索引里的提交标识取版本前的存在性检查。"""
        if not commit or not re.fullmatch(r"[0-9a-f]{40,64}", str(commit)):
            return False
        return self.run("cat-file", "-e", commit + "^{commit}", check=False).returncode == 0

    def require_remote(self):
        """本地模式下误用远端操作时明确失败，不静默跳过。"""
        if self.local_only:
            fail("GIT_LOCAL_ONLY", "本地历史仓库不支持远端操作",
                 user_message="本地备份不能访问远端",
                 user_hint="请重新点击「立即备份」；若持续出现，请反馈该问题")

    def fetch(self):
        self.require_remote()
        result = self.text("ls-remote", "origin", "refs/heads/" + self.branch)
        if not result:
            if self.text("ls-remote", "origin"):
                fail("GIT_BRANCH_MISSING", "远端已有内容但目标分支不存在，请选择正确分支或空仓库",
                     user_message="远端没有这个分支",
                     user_hint="请填写远端已有的分支名，或改用远端上的正确分支")
            return None
        tip = result.split()[0]
        if self.run("cat-file", "-e", tip + "^{commit}", check=False).returncode == 0:
            return tip
        self.run("fetch", "--depth=1", "origin", "+refs/heads/" + self.branch + ":refs/remotes/origin/" + self.branch)
        return self.text("rev-parse", "refs/remotes/origin/" + self.branch)

    def checkout_snapshot(self, commit, target):
        # git archive 不包含 .git；逐成员校验路径，不执行远端代码和过滤器。
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        archive = self.run("archive", "--format=tar", commit).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            for member in stream:
                p = (target / member.name).resolve()
                if not p.is_relative_to(target.resolve()) or member.issym() or member.islnk():
                    fail("SNAPSHOT_INVALID", "远端包含不安全路径",
                         user_message="远端数据中包含不安全的路径",
                         user_hint="请不要手工改动数据仓库，改为在本程序里重新同步")
                if member.isdir():
                    p.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(member) as source, p.open("wb") as out:
                        shutil.copyfileobj(source, out)
                else:
                    fail("SNAPSHOT_INVALID", "远端包含不受支持的文件",
                         user_message="远端数据中包含本程序不认识的文件",
                         user_hint="请不要在数据仓库里手动添加文件")

    def commit(self, snapshot, message, parents=None, update=True):
        source, root = Path(snapshot).resolve(), self.root.resolve()
        if source == root or source.is_relative_to(root) or root.is_relative_to(source):
            fail("SNAPSHOT_SOURCE", "提交快照必须独立于 Git 工作树",
                 user_message="同步数据的工作目录位置不正确",
                 user_hint="请回到同步页面重试；若持续出现，请反馈该问题")
        for child in self.root.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                remove_tree(child, self.root)
            else:
                child.unlink()
        for child in Path(snapshot).iterdir():
            if child.is_dir():
                shutil.copytree(child, self.root / child.name)
            else:
                shutil.copy2(child, self.root / child.name)
        self.run("add", "--all", "--", ".")
        tree = self.text("write-tree")
        previous = self.head()
        if parents is None:
            parents = [previous] if previous else []
            if previous and tree == self.text("rev-parse", previous + "^{tree}"):
                return previous
        args = ["commit-tree", tree]
        for parent in dict.fromkeys(parents):
            args += ["-p", parent]
        commit = self.run(*args, data=(message + "\n").encode()).stdout.decode().strip()
        if update:
            self.run("update-ref", "refs/heads/" + self.branch, commit)
            self.run("symbolic-ref", "HEAD", "refs/heads/" + self.branch)
        else:
            self.run("update-ref", "refs/xm/pending", commit)
        return commit

    def push(self, commit):
        self.require_remote()
        self.run("push", "origin", commit + ":refs/heads/" + self.branch)
        actual = self.text("ls-remote", "origin", "refs/heads/" + self.branch)
        if not actual or actual.split()[0] != commit:
            fail("REMOTE_CHANGED", "推送后远端再次变化，请重新检查同步",
                 user_message="上传后远端又出现了新的改动",
                 user_hint="请点击「立即同步」重新比较后再决定")

    def is_ancestor(self, before, after):
        return bool(before and after and self.run("merge-base", "--is-ancestor", before, after, check=False).returncode == 0)

    def compact(self, confirmed):
        self.require_remote()
        fresh = self.root.with_name("repo-new")
        old = self.root.with_name("repo-old")
        if fresh.exists():
            remove_tree(fresh, self.root.parent)
        self.run("clone", "--depth=1", "--single-branch", "--branch", self.branch, "--", self.remote, str(fresh))
        tip = self.run("rev-parse", "HEAD", cwd=fresh).stdout.decode().strip()
        if tip != confirmed:
            remove_tree(fresh, self.root.parent)
            return False
        if old.exists():
            remove_tree(old, self.root.parent)
        replace_path(self.root, old, self.root.parent)
        try:
            replace_path(fresh, self.root, self.root.parent)
        except OSError:
            replace_path(old, self.root, self.root.parent)
            raise
        remove_tree(old, self.root.parent)
        # 新仓库继续使用确定性的行尾和禁用 hooks。
        self.run("config", "core.autocrlf", "false")
        self.run("config", "core.hooksPath", str(self.root / "disabled-hooks"))
        self.run("config", "user.name", "XM-IMS " + self.device)
        self.run("config", "user.email", "xm-ims@localhost")
        return True
