"""GitHub 账号：已登录账号列表、显式登录、凭据读取与身份验证，仓库占用检查与创建。

接收实际的 Git 配置（allow_file）与仓库名规则，不调用设置更新或数据库；
显式登录是本模块唯一允许打开系统授权浏览器的入口。
"""
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import ApiError, error_dict
from ..sync import gitstore
from ..sync.common import fail
from ..sync.gitstore import GitStore
from . import transport

# 仓库名只允许常规字母、数字、点、下划线、连字符，避免拼出任意 API 路径。
REPO_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class GitHub:
    def __init__(self, allow_file=False):
        self.allow_file = allow_file

    # ---------- 账号 ----------

    def status(self, account=None):
        prefix = gitstore.locate_gcm()
        result = {"gcm_available": bool(prefix), "accounts": [], "selected": None,
                  "verified": False, "verified_as": None, "error": None}
        if not prefix:
            return result
        result["accounts"] = self._accounts()
        if account:
            if not isinstance(account, str) or account not in result["accounts"]:
                fail("ONBOARDING_GITHUB_ACCOUNT", f"账号 {account!r} 不在本机已保存的 GitHub 账号中",
                     user_message="选择的账号还没有在本机登录",
                     user_hint="请选择已登录的账号，或点击「登录 GitHub」")
            result["selected"] = account
            try:
                result["verified_as"] = self.verify(account)
                result["verified"] = True
            except ApiError as exc:
                # 凭据缺失、失效或网络波动作为阶段性结果返回，由向导引导重新登录。
                result["error"] = error_dict(exc.code, exc.message, exc.hint, exc.user_message, exc.user_hint)
        return result

    def _accounts(self):
        prefix = gitstore.locate_gcm()
        if not prefix:
            return []
        try:
            result = transport.run_hidden(prefix + ["github", "list"], timeout=30)
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

    def login(self, account, force):
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
            result = transport.run_hidden(args, env=env, timeout=transport.LOGIN_TIMEOUT)
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
        return self.status(account)

    def token(self, account):
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
            result = transport.run_hidden([git_exe, "-c", "credential.helper=",
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

    def verify(self, account):
        """凭据存在不等于仍有效：用官方 GET /user 确认实际登录身份。"""
        token = self.token(account)
        try:
            profile = transport.request_json(transport.GITHUB_API + "/user", token=token, timeout=20)
        except transport.GitHubHttpError as exc:
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

    # ---------- 仓库 ----------

    @staticmethod
    def remote_owner(remote):
        """HTTPS GitHub 地址返回所有者登录名，其他地址返回空串。"""
        parts = urlsplit(remote)
        return parts.path.strip("/").split("/")[0] if parts.scheme == "https" and parts.hostname == "github.com" else ""
    def assert_repo_free(self, owner, repository):
        try:
            transport.request_json(f"{transport.GITHUB_API}/repos/{owner}/{repository}", timeout=15)
        except transport.GitHubHttpError as exc:
            if exc.status == 404:
                return
            fail("ONBOARDING_REPO_CHECK", f"检查同名仓库失败（HTTP {exc.status}）",
                 user_message="暂时无法确认仓库名称是否可用",
                 user_hint="请稍后重试")
        fail("ONBOARDING_REPO_EXISTS", f"账号 {owner} 下已存在同名仓库 {repository}",
             user_message="这个仓库名已经被使用",
             user_hint="请换一个仓库名称，或改用「连接已有仓库」的方式")

    def create_repository(self, account, repository):
        """POST /user/repos：固定私有、不初始化；不自动重试，避免重复建库。"""
        token = self.token(account)
        try:
            created = transport.request_json(f"{transport.GITHUB_API}/user/repos", method="POST",
                                             token=token,
                                             payload={"name": repository, "private": True,
                                                      "auto_init": False},
                                             timeout=30, retry=False)
        except transport.GitHubHttpError as exc:
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

    def repository_info(self, account, repository):
        """结果不确定时用选中账号的凭据读取私有仓库，不用匿名 404 判断创建结果。"""
        owner = self.verify(account)
        token = self.token(account)
        try:
            info = transport.request_json(f"{transport.GITHUB_API}/repos/{owner}/{repository}",
                                          token=token, timeout=20)
        except transport.GitHubHttpError as exc:
            if exc.status == 404:
                return None
            fail("ONBOARDING_REPO_CHECK", f"读取创建结果失败（HTTP {exc.status}）",
                 user_message="暂时无法确认仓库创建结果",
                 user_hint="请稍后点击「检查此仓库」")
        actual_owner = info.get("owner", {}).get("login", "")
        if str(actual_owner).lower() != owner.lower() \
                or str(info.get("name", "")).lower() != repository.lower() \
                or not info.get("private") or not info.get("clone_url"):
            fail("ONBOARDING_REPO_CHECK", "仓库所有者、名称或私有性与本次目标不符",
                 user_message="这个仓库与本次创建目标不一致",
                 user_hint="请确认所选账号和私有仓库名称")
        GitStore.validate_remote(info["clone_url"], self.allow_file)
        return {"remote": info["clone_url"], "url": info.get("html_url") or info["clone_url"]}
