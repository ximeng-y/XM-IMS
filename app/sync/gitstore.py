"""只操作专用数据仓库的 Git 适配；不执行文本自动合并。"""
import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from urllib.parse import urlsplit
from .common import fail, remove_tree, replace_path


class GitStore:
    def __init__(self, root, remote, branch="main", device="本机", allow_file=False):
        self.root, self.remote, self.branch, self.device = Path(root), remote, branch, device
        self.allow_file = allow_file
        self.validate_remote(remote, allow_file)
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
        if self.run("remote", "get-url", "origin", check=False).returncode:
            self.run("remote", "add", "origin", remote)
        else:
            self.run("remote", "set-url", "origin", remote)

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

    def run(self, *args, check=True, data=None, cwd=None):
        env = os.environ.copy()
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes")
        # 不使用 shell，不把远端响应直接展示给用户，以免泄漏凭证。
        try:
            result = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.hooksPath=" + str(self.root / "disabled-hooks"), *args],
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

    def fetch(self):
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
        self.run("push", "origin", commit + ":refs/heads/" + self.branch)
        actual = self.text("ls-remote", "origin", "refs/heads/" + self.branch)
        if not actual or actual.split()[0] != commit:
            fail("REMOTE_CHANGED", "推送后远端再次变化，请重新检查同步",
                 user_message="上传后远端又出现了新的改动",
                 user_hint="请点击「立即同步」重新比较后再决定")

    def is_ancestor(self, before, after):
        return bool(before and after and self.run("merge-base", "--is-ancestor", before, after, check=False).returncode == 0)

    def compact(self, confirmed):
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
