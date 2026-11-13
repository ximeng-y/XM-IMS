"""环境检测与程序内安装 Git / Git Credential Manager。

只关心本机工具是否可用，不知道任何同步状态；安装动作的同步互斥由门面外层负责。

源码模式保持原有实现（Git 官方安装器装到用户级目录、gcmuser 安装器）。
桌面模式改为按 locations 记录的依赖位置安装：默认局部装到软件目录下的 tools，
可改为用户自选文件夹或明确的系统安装。局部方案使用 PortableGit 自解压包，
不在系统写 PATH 与注册表；只有用户明确选择系统安装时才通过 UAC 运行官方安装器。
"""
import os
import platform
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from .. import config
from ..platform_tools import locations, windows_install
from ..sync import gitstore
from ..sync.common import fail
from . import transport

# 安装包文件名模式：只接受官方发布里符合既有命名的资产。
_GIT_ASSET_PATTERN = re.compile(r"Git-\d[\w.]*-64-bit\.exe")
_GCM_ASSET_PATTERN = re.compile(r"gcmuser-win-x64-[\w.]+\.exe")
# 桌面局部方案：PortableGit 自解压包（排除 ARM64、32 位与符号文件）与 GCM 压缩包
_PORTABLE_GIT_PATTERN = re.compile(r"PortableGit-\d[\w.]*-64-bit\.7z\.exe")
_GCM_ZIP_PATTERN = re.compile(r"gcm-win-x64-[\w.]+(?<!-symbols)\.zip")

_GIT_RELEASES = "https://api.github.com/repos/git-for-windows/git/releases/latest"
_GCM_RELEASES = "https://api.github.com/repos/git-ecosystem/git-credential-manager/releases/latest"

# 系统安装的默认目录；用户明确选择系统安装时使用
SYSTEM_GIT_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git"

# 路径规则提示：只在目标完整路径含中文时出现
_PATH_CHOICE_MESSAGE = "当前 Git 安装位置包含中文，请选择不含中文的文件夹，或安装到系统位置。"
_PATH_CHOICE_HINT = "也可以取消本次安装，之后重新选择安装位置"


class Environment:
    def __init__(self, workspace):
        self.workspace = workspace

    def environment(self, refresh=False):
        system, machine = platform.system(), platform.machine()
        result = {
            "os": system, "arch": machine,
            "one_click_install": system == "Windows" and machine in ("AMD64", "x86_64"),
            "git": {"available": False, "version": None, "path": None},
            "gcm": {"available": False, "version": None},
        }
        git_exe = gitstore.locate_git(refresh=refresh)
        if git_exe:
            result["git"] = {"available": True, "version": self._first_line([git_exe, "--version"]),
                             "path": None if git_exe == "git" else git_exe}
            prefix = gitstore.locate_gcm(refresh=refresh)
            if prefix:
                result["gcm"] = {"available": True, "version": self._first_line(prefix + ["--version"])}
        if config.DESKTOP_MODE:
            # installation 始终描述 Git 目标；gcm_installation 只描述独立的登录组件目标。
            result["installation"] = locations.view()
            result["gcm_installation"] = locations.view(locations.COMPONENT_GCM)
        return result

    @classmethod
    def _first_line(cls, argv, env=None):
        try:
            result = transport.run_hidden(argv, env=env)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        text = result.stdout.decode("utf-8", "replace").strip()
        return text.splitlines()[0].strip() if text else ""

    def install(self, component, mode=None, path=None):
        if component not in ("git", "gcm"):
            fail("ONBOARDING_INVALID", "component 必须是 git 或 gcm",
                 user_message="要安装的组件不明确",
                 user_hint="请在备份配置向导中重新操作")
        env = self.environment()
        # 收到安装请求后再次检测：组件已可用就直接返回结果，不重复执行安装器。
        if env[component]["available"]:
            return env
        if not env["one_click_install"]:
            fail("ONBOARDING_INSTALL_UNSUPPORTED", "当前系统或架构不支持程序内一键安装",
                 user_message="当前系统暂不支持自动安装",
                 user_hint="请从官网手动安装后，再点击「重新检测」")
        if component == "gcm" and not env["git"]["available"]:
            fail("ONBOARDING_INSTALL_PRE", "安装登录组件前需要先安装 Git",
                 user_message="还需要先准备好 Git",
                 user_hint="请先在上一步安装 Git，再安装登录组件")
        if config.DESKTOP_MODE:
            return self._install_desktop(component, mode, path)
        if component == "git":
            url = self._asset_url(_GIT_RELEASES, _GIT_ASSET_PATTERN)
            target = Path(os.environ.get("LOCALAPPDATA", str(config.BASE_DIR))) / "Programs" / "Git"
            self._run_installer(url, ["/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-",
                                      "/DIR=" + str(target)])
        else:
            url = self._asset_url(_GCM_RELEASES, _GCM_ASSET_PATTERN)
            self._run_installer(url, ["/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-"])
        return self._finish(component)

    # ---------- 桌面模式 ----------

    def _install_desktop(self, component, mode, path):
        try:
            selected = mode or (locations.mode() if component == locations.COMPONENT_GIT
                                else locations.gcm_target()["mode"])
            target = locations.resolve(selected, path, component)
        except ValueError as exc:
            fail("ONBOARDING_INSTALL_MODE", str(exc),
                 user_message="安装位置无法确定",
                 user_hint="请重新选择安装位置后重试")
        if selected == locations.MODE_GLOBAL:
            return self._install_global(component)
        if target["requires_path_choice"]:
            fail("ONBOARDING_PATH_CHOICE", f"安装目标路径含中文：{target['root']}",
                 user_message=_PATH_CHOICE_MESSAGE,
                 user_hint=_PATH_CHOICE_HINT)
        if component == locations.COMPONENT_GIT:
            self._install_portable_git(target)
        else:
            self._install_local_gcm(target)
        return self._finish_desktop(component, selected, path, target)

    def _finish_desktop(self, component, mode, path, target):
        """局部 / 自选目标安装后的核验：按本次目标定位，成功才保存位置。

        不能在保存前用 locations.mode() 的旧值定位——用户改选了 custom 时，
        旧定位器仍指向原目标，会误判安装失败。系统安装走 _finish_global。

        独立登录组件与 Git 共存：GCM 只核验本次 GCM 目标与统一定位的当前 Git，
        不要求本次 GCM 根下另有一份 Git，也不改动 Git 的顶层选择。
        """
        if component == locations.COMPONENT_GIT:
            git_exe = gitstore.locate_git_from(target["git_root"])
            gcm_ok = True
        else:
            git_exe = gitstore.locate_git()
            gcm_ok = self._gcm_in_root(target["gcm_root"]) or bool(
                git_exe and self._first_line([git_exe, "credential-manager", "--version"]))
        if not git_exe:
            fail("ONBOARDING_INSTALL_FAILED", "安装完成但 Git 未在目标目录找到",
                 user_message="安装没有成功",
                 user_hint="请重试一次，或从官网手动安装后点击「重新检测」")
        if not gcm_ok:
            fail("ONBOARDING_INSTALL_FAILED", "安装完成但登录组件不可用",
                 user_message="安装没有成功",
                 user_hint="请重试一次；若持续失败，请从官网手动安装后点击「重新检测」")
        # 保存后统一缓存按新记录重新定位，后续业务使用同一位置
        try:
            if component == locations.COMPONENT_GIT:
                locations.save(mode, path=path)
            else:
                locations.save_gcm(mode, path=path)
        except ValueError:
            pass  # 记录写入失败不影响本次已可用的组件
        gitstore.locate_git(refresh=True)
        gitstore.locate_gcm(refresh=True)
        env = self.environment()
        if not env[component]["available"]:
            fail("ONBOARDING_INSTALL_FAILED", "安装完成但组件仍不可用",
                 user_message="安装没有成功",
                 user_hint="请重试一次，或从官网手动安装后点击「重新检测」")
        return env

    @classmethod
    def _gcm_in_root(cls, root):
        """目标目录里是否有可执行的独立登录组件。"""
        if root is None:
            return False
        for name in ("git-credential-manager.exe", "git-credential-manager"):
            exe = Path(root) / name
            if exe.is_file() and cls._first_line([str(exe), "--version"]):
                return True
        return False

    def _install_portable_git(self, target):
        """局部 Git：PortableGit 自解压到目标 git 目录，由 SFX 执行自带 post-install。"""
        git_root = target["git_root"]
        if git_root is None:
            fail("ONBOARDING_INSTALL_MODE", "缺少局部 Git 安装目录",
                 user_message="安装位置无法确定",
                 user_hint="请重新选择安装位置后重试")
        if gitstore.locate_git_from(git_root):
            # 该目录已有可执行的 Git：直接复用，不重复下载安装
            return
        if git_root.exists() and any(git_root.iterdir()):
            fail("ONBOARDING_TARGET_NOT_EMPTY", f"安装目录非空且不是可用的 Git：{git_root}",
                 user_message="这个文件夹里已经有其他文件",
                 user_hint="请选择其他文件夹，或清空该文件夹后重试")
        git_root.mkdir(parents=True, exist_ok=True)
        url = self._asset_url(_GIT_RELEASES, _PORTABLE_GIT_PATTERN)
        work = self.workspace.work_dir("install")
        try:
            installer = work / Path(url).name
            transport.download(url, installer)
            command = [str(installer), "-y", "-o" + str(git_root)]
            try:
                code = transport.run_hidden(command, timeout=transport.INSTALLER_TIMEOUT).returncode
            except (OSError, subprocess.TimeoutExpired):
                code = None
        finally:
            self.workspace.cleanup(work)
        if code not in (0, 3010):
            fail("ONBOARDING_INSTALL_FAILED", f"PortableGit 解压未成功完成（退出码 {code}）",
                 user_message="安装没有成功",
                 user_hint="请重试一次；若持续失败，请选择其他文件夹后重试")

    def _install_local_gcm(self, target):
        """局部 GCM：PortableGit 未带内置组件时，把官方 zip 解到目标 gcm 目录。"""
        gcm_root = target["gcm_root"]
        if gcm_root is None:
            fail("ONBOARDING_INSTALL_MODE", "缺少局部登录组件安装目录",
                 user_message="安装位置无法确定",
                 user_hint="请重新选择安装位置后重试")
        # 只看本次目标：改选目录时旧的 GCM 位置不能当作已经装好
        if self._gcm_in_root(gcm_root):
            return
        # 使用统一定位的当前 Git：复用的系统 Git 可能自带登录组件，不要求本次目标下另有 Git。
        git_exe = gitstore.locate_git()
        if git_exe and self._first_line([git_exe, "credential-manager", "--version"]):
            return  # 当前 Git 自带登录组件，无需单独解压
        url = self._asset_url(_GCM_RELEASES, _GCM_ZIP_PATTERN)
        work = self.workspace.work_dir("install")
        try:
            archive = work / Path(url).name
            transport.download(url, archive)
            gcm_root.mkdir(parents=True, exist_ok=True)
            self._extract_zip(archive, gcm_root)
        finally:
            self.workspace.cleanup(work)

    @staticmethod
    def _extract_zip(archive, target):
        """只接受落在目标目录内的普通文件条目，避免借解压覆盖外部文件。"""
        root = Path(target).resolve()
        with zipfile.ZipFile(archive) as stream:
            for member in stream.infolist():
                name = member.filename.replace("\\", "/")
                parts = Path(name).parts
                unsafe = (not name or name.startswith("/") or ":" in parts[0]
                          or ".." in parts)
                destination = (root / name).resolve() if not unsafe else None
                if unsafe or not destination.is_relative_to(root):
                    fail("ONBOARDING_ARCHIVE_INVALID", "登录组件压缩包包含不安全路径",
                         user_message="安装包内容无法校验",
                         user_hint="请重试；若持续出现，请反馈该问题")
                if name.endswith("/"):
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with stream.open(member) as source, destination.open("wb") as out:
                    shutil.copyfileobj(source, out)

    def _install_global(self, component):
        """明确的系统安装：官方安装器 + UAC 提升；已存在可用 Git 时直接复用。

        系统安装的目标不在 locations 记录里（此时记录仍是 local/custom），
        核验必须直接走系统候选，不能按旧记录定位。
        """
        recorded = locations.global_git()
        if recorded and gitstore.locate_git_from(recorded):
            return self._finish_global(component, recorded)
        system_exe = self._system_git_exe()
        if system_exe:
            return self._finish_global(component, self._git_home(system_exe))
        if component == "gcm":
            # 系统安装只有 Git 需要提升；登录组件随 Git 自带
            fail("ONBOARDING_INSTALL_PRE", "系统安装已在 Git 步骤完成，登录组件应随 Git 提供",
                 user_message="登录组件需要随 Git 一起安装",
                 user_hint="请在上一步选择系统安装完成 Git，再点击「重新检测」")
        url = self._asset_url(_GIT_RELEASES, _GIT_ASSET_PATTERN)
        work = self.workspace.work_dir("install")
        try:
            installer = work / Path(url).name
            transport.download(url, installer)
            parameters = ('/VERYSILENT /NORESTART /NOCANCEL /SP- /DIR="'
                          + str(SYSTEM_GIT_DIR) + '"')
            started, code = windows_install.run_elevated(installer, parameters)
        finally:
            self.workspace.cleanup(work)
        if not started:
            fail("ONBOARDING_INSTALL_CANCELLED", "用户取消了权限提升或安装未能启动",
                 user_message="没有继续系统安装",
                 user_hint="已保留原来的安装位置；需要时可以重新选择安装方式")
        if code not in (0, 3010):
            fail("ONBOARDING_INSTALL_FAILED", f"系统安装器未成功完成（退出码 {code}）",
                 user_message="安装没有成功",
                 user_hint="请重试一次；若系统策略阻止安装，请从官网手动安装后再点击「重新检测」")
        # 双击安装器可能改了 PATH，但当前进程继承不到；直接重新扫描系统候选
        system_exe = self._system_git_exe()
        if not system_exe:
            fail("ONBOARDING_INSTALL_FAILED", "系统安装完成但 Git 仍不可用",
                 user_message="安装没有成功",
                 user_hint="请重启程序后点击「重新检测」；若仍不可用，请从官网手动安装")
        return self._finish_global(component, self._git_home(system_exe))

    def _finish_global(self, component, git_path):
        """保存系统安装位置后再按新记录核验；失败不改动已记录的位置。"""
        try:
            locations.save(locations.MODE_GLOBAL, git_path=git_path)
        except ValueError:
            fail("ONBOARDING_INSTALL_FAILED", f"系统 Git 位置无法记录：{git_path}",
                 user_message="安装没有成功",
                 user_hint="请从官网手动安装后点击「重新检测」")
        gitstore.locate_git(refresh=True)
        gitstore.locate_gcm(refresh=True)
        env = self.environment()
        if not env[component]["available"]:
            fail("ONBOARDING_INSTALL_FAILED", "系统安装完成但组件仍不可用",
                 user_message="安装没有成功",
                 user_hint="请重试一次，或从官网手动安装后点击「重新检测」")
        return env

    @staticmethod
    def _system_git_exe():
        """系统候选里第一个可执行的 Git；不看 locations 记录。"""
        return gitstore.locate_system_git()

    @staticmethod
    def _git_home(git_exe):
        """Git 可执行文件位于 <安装目录>\\cmd\\git.exe 或 <安装目录>\\bin\\git.exe。"""
        return Path(git_exe).parent.parent

    def _finish(self, component):
        """源码模式安装后重新定位并返回环境快照。"""
        gitstore.locate_git(refresh=True)
        gitstore.locate_gcm(refresh=True)
        new_env = self.environment()
        if not new_env[component]["available"]:
            fail("ONBOARDING_INSTALL_FAILED", "安装完成但组件仍不可用",
                 user_message="安装没有成功",
                 user_hint="请重试一次，或从官网手动安装后点击「重新检测」")
        return new_env

    def _run_installer(self, url, args):
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
