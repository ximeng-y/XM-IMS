"""环境检测与程序内安装 Git / Git Credential Manager。

只关心本机工具是否可用，不知道任何同步状态；安装动作的同步互斥由门面外层负责。
"""
import os
import platform
import re
import subprocess
from pathlib import Path

from .. import config
from ..sync import gitstore
from ..sync.common import fail
from . import transport

# 安装包文件名模式：只接受官方发布里符合既有命名的资产。
_GIT_ASSET_PATTERN = re.compile(r"Git-\d[\w.]*-64-bit\.exe")
_GCM_ASSET_PATTERN = re.compile(r"gcmuser-win-x64-[\w.]+\.exe")


class Environment:
    def __init__(self, workspace):
        self.workspace = workspace

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
            result = transport.run_hidden(argv, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        text = result.stdout.decode("utf-8", "replace").strip()
        return text.splitlines()[0].strip() if text else ""

    def install(self, component):
        if component not in ("git", "gcm"):
            fail("ONBOARDING_INVALID", "component 必须是 git 或 gcm",
                 user_message="要安装的组件不明确",
                 user_hint="请在备份配置向导中重新操作")
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
        work = self.workspace.work_dir("install")
        try:
            installer = work / Path(url).name
            transport.download(url, installer)
            try:
                code = transport.run_hidden([str(installer), *args],
                                            timeout=transport.INSTALLER_TIMEOUT).returncode
            except (OSError, subprocess.TimeoutExpired):
                code = None
        finally:
            self.workspace.cleanup(work)
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
            release = transport.request_json(releases_url, timeout=20)
        except transport.GitHubHttpError as exc:
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
