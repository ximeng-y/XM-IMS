"""Windows 桌面发行版构建：组装版本载荷、固定根启动器、安装包与更新 ZIP。

用法（固定入口）：
    .venv\\Scripts\\python.exe -B desktop\\build.py [--portable] [--nsis <makensis.exe 所在目录>]

输出：
    .XMTEMP\\electron-windows\\dist\\XM-IMS-<版本>-win-x64-Setup.exe
    .XMTEMP\\electron-windows\\dist\\XM-IMS-<版本>-win-x64.zip

全部工作文件位于项目内 .XMTEMP/electron-windows/，不安装全局工具。
网络不可用时可手工把 Electron / Python / NSIS 官方包放进
.XMTEMP/electron-windows/cache/ 后重跑，脚本不会扫描磁盘寻找替代品。
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DESKTOP_DIR.parent
WORK_ROOT = PROJECT_ROOT / ".XMTEMP" / "electron-windows"
CACHE_ROOT = WORK_ROOT / "cache"
STAGE_ROOT = WORK_ROOT / "stage"
DIST_ROOT = WORK_ROOT / "dist"
PORTABLE_ROOT = PROJECT_ROOT / "desktop-test"

APP_NAME = "XM-IMS"
ELECTRON_EXE = "xm-ims.exe"
LAUNCHER_EXE = "xm-ims.exe"
LEGACY_EXE = "xm-ims-legacy.exe"
VERSION_MANIFEST = "version-manifest.json"
PROTOCOL = 1

# 打进 resources/backend 的源码范围：明确列出，不递归拷整个仓库。
BACKEND_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
BACKEND_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite"}
DESKTOP_FILES = ("main.js", "preload.js", "paths.js", "backend.js", "update.js",
                 "version-store.js", "backend_runner.py")
# 固定根独立恢复入口的源码：只复制，不另写一套恢复算法
BOOTSTRAP_SUPPORT = (("app/sync/update_trial.py", "update_trial.py"),
                     ("app/storage/process_lock.py", "process_lock.py"),
                     ("desktop/installer_repair.py", "installer_repair.py"))


def fail(message):
    print(f"构建失败：{message}", file=sys.stderr)
    raise SystemExit(1)


def read_lock():
    return json.loads((DESKTOP_DIR / "runtime-lock.json").read_text(encoding="utf-8"))


def app_version():
    version_file = PROJECT_ROOT / "app" / "version.py"
    namespace = {}
    exec(compile(version_file.read_text(encoding="utf-8"), str(version_file), "exec"), namespace)
    version = str(namespace.get("APP_VERSION") or "").strip()
    if not version.count(".") == 2 or not all(part.isdigit() for part in version.split(".")):
        fail(f"APP_VERSION 不是 X.Y.Z 格式：{version!r}")
    return version


def build_id():
    """每次构建产生唯一标识：Setup 与 ZIP 使用同一标识，槽名因此不会互相覆盖。"""
    return uuid.uuid4().hex


def slot_name(version, build):
    return f"{version}-{build}"


def fetch(url, filename):
    """下载到缓存目录；已存在则复用，不重复下载。"""
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    target = CACHE_ROOT / filename
    if target.is_file() and target.stat().st_size > 0:
        return target
    print(f"下载 {url}")
    temporary = target.with_name(target.name + ".part")
    # 部分官方下载地址对非浏览器客户端会给出说明页，用明确 UA 取真实文件
    request = urllib.request.Request(url, headers={"User-Agent": "XM-IMS-Build"})
    with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as out:
        shutil.copyfileobj(response, out)
    temporary.replace(target)
    return target


def extract(archive, target):
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as stream:
        stream.extractall(target)
    return target


def reset_stage():
    """只清理本次构建的暂存与输出目录，不动 .XMTEMP 里的其它内容。"""
    for folder in (STAGE_ROOT, DIST_ROOT):
        if folder.exists():
            shutil.rmtree(folder)
    STAGE_ROOT.mkdir(parents=True)
    DIST_ROOT.mkdir(parents=True)


def copy_backend(target):
    """应用代码只复制发行需要的文件；数据、缓存和已编译文件不进安装包。"""
    def ignore(directory, names):
        skipped = set()
        for name in names:
            full = Path(directory) / name
            if name in BACKEND_EXCLUDE_DIRS or full.suffix.lower() in BACKEND_EXCLUDE_SUFFIXES:
                skipped.add(name)
        return skipped

    for name in ("app",):
        shutil.copytree(PROJECT_ROOT / name, target / name, ignore=ignore)
    shutil.copy2(PROJECT_ROOT / "requirements.txt", target / "requirements.txt")


def write_package(app_dir, version):
    template = json.loads((DESKTOP_DIR / "package.template.json").read_text(encoding="utf-8"))
    template["version"] = version
    (app_dir / "package.json").write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_python(python_dir, lock):
    archive = fetch(lock["python"]["url"], f"python-{lock['python']['version']}-embed-amd64.zip")
    extract(archive, python_dir)
    pth = python_dir / "python312._pth"
    # 嵌入解释器默认忽略 site-packages；本发行包的依赖在 Lib\\site-packages，
    # 应用代码在 resources\\backend（相对 python 目录为 ..\\backend）。
    pth.write_text(
        "python312.zip\n.\nLib\\site-packages\n..\\backend\nimport site\n",
        encoding="utf-8",
    )
    site_packages = python_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    pip = Path(sys.executable).with_name("pip.exe")
    if not pip.is_file():
        fail("构建解释器缺少 pip，无法准备依赖")
    command = [str(Path(sys.executable)), "-m", "pip", "install", "--no-warn-script-location",
               "--no-compile", "--only-binary=:all:", "--disable-pip-version-check",
               "--target", str(site_packages), "-r", str(PROJECT_ROOT / "requirements.txt")]
    print("准备 Python 依赖到暂存目录")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        fail("安装 Python 依赖失败：\n" + (result.stderr or result.stdout).strip())
    # 依赖里自带的测试与缓存文件不进入发行包
    for cache in site_packages.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def program_manifest(stage, version):
    """版本载荷的受管理程序文件清单：EXE、官方运行文件与 resources 下的文件。

    不列 data、tools、.XMTEMP 或用户文件；清单自身单独登记，避免自引用大小。
    """
    lines = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file():
            continue
        # 清单交给 NSIS 与启动器后直接拼进 Delete，必须使用 Windows 反斜杠分隔符
        relative = str(path.relative_to(stage))
        if relative in ("installed-program-files.txt",):
            continue
        lines.append(relative)
    lines.append("installed-program-files.txt")
    target = stage / "installed-program-files.txt"
    # 不带 BOM 的 UTF-16LE；NSIS 侧用 FileReadUTF16LE 读取。
    target.write_bytes("\r\n".join(lines).encode("utf-16-le"))
    print(f"版本程序文件 {len(lines)} 项，版本 {version}")
    return lines


def version_manifest(stage, version, build, entry):
    """版本清单：格式、版本、构建标识、平台、入口与载荷相对路径及大小。

    直接从载荷目录枚举（ZIP 成员用正斜杠，清单键必须一致）；清单自身不登记。
    不增加逐文件 SHA。
    """
    files = {}
    for path in sorted(stage.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(stage).as_posix()
        if name in ("installed-program-files.txt", VERSION_MANIFEST):
            continue
        files[name] = path.stat().st_size
    manifest = {
        "format": 1,
        "version": version,
        "build": build,
        "platform": "win32",
        "arch": "x64",
        "protocol": PROTOCOL,
        "entry": entry,
        "files": files,
    }
    (stage / VERSION_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def read_program_manifest(target):
    """读取便携目录上一版程序文件；用户数据不在清单内。"""
    manifest = target / "installed-program-files.txt"
    if not manifest.is_file():
        return []
    return [line.strip() for line in manifest.read_text(encoding="utf-16-le").splitlines()
            if line.strip()]


def sync_portable(stage, slot, launcher):
    """把固定根启动器与完整版本载荷同步到便携目录，保留 data、tools 与 .XMTEMP。

    仍然不写安装注册表、卸载器或快捷方式；旧平铺程序文件保留为 legacy 回退目标。
    """
    target = PORTABLE_ROOT
    target.mkdir(parents=True, exist_ok=True)

    # 已有平铺程序：改名为 legacy 并保留原文件归属，作为第一跳回退目标
    flat_exe = target / ELECTRON_EXE
    legacy = target / LEGACY_EXE
    if flat_exe.is_file() and flat_exe.resolve() != launcher.resolve():
        previous = read_program_manifest(target)
        if legacy.is_file():
            legacy.unlink()
        flat_exe.replace(legacy)
        wer = target / "xm-ims_wer.dll"
        if wer.is_file() and not (target / "xm-ims-legacy_wer.dll").is_file():
            wer.replace(target / "xm-ims-legacy_wer.dll")
        legacy_lines = []
        for relative in previous:
            item = Path(relative)
            if item.is_absolute() or ".." in item.parts:
                fail(f"便携目录程序清单包含非法路径：{relative!r}")
            if relative == ELECTRON_EXE:
                legacy_lines.append(LEGACY_EXE)
            else:
                legacy_lines.append(relative)
        (target / "installed-program-files.txt").write_bytes(
            "\r\n".join(legacy_lines).encode("utf-16-le"))
        assets = target / "bootstrap-support"
        if assets.exists():
            shutil.rmtree(assets)

    versions = target / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    destination = versions / slot
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(stage, destination)
    # 固定根启动器：ZIP 与便携共用同一份编译结果
    shutil.copy2(launcher, target / LAUNCHER_EXE)
    # 手动构建刷新同协议恢复支持，避免继续保留已修复的旧脚本。
    support_source = CACHE_ROOT / "bootstrap-support"
    support = target / "bootstrap-support"
    if support_source.is_dir():
        support.mkdir(parents=True, exist_ok=True)
        for _, name in BOOTSTRAP_SUPPORT:
            source = support_source / name
            if source.is_file():
                shutil.copy2(source, support / name)
    # 只有首次迁移或尚无选择资料时才写稳定选择；已有准备/待应用状态不覆盖
    selection = target / "data" / "update" / "desktop" / "selection.ini"
    if not selection.is_file():
        selection.parent.mkdir(parents=True, exist_ok=True)
        selection.write_text(
            "[selection]\r\nprotocol=1\r\nphase=stable\r\nactive=" + slot +
            "\r\nprevious=" + ("legacy" if legacy.is_file() else "") +
            "\r\ncandidate=\r\ncandidate_version=\r\ncandidate_tag=\r\nattempt=\r\n" +
            "fallback_error=\r\nfallback_target=\r\nfallback_version=\r\n", encoding="ascii")
    print(f"便携测试版已更新：{target}")


def copy_bootstrap_support():
    """固定根独立恢复入口的源码复制到缓存目录，供启动器与便携共用。"""
    target = CACHE_ROOT / "bootstrap-support"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for source, name in BOOTSTRAP_SUPPORT:
        shutil.copy2(PROJECT_ROOT / source, target / name)
    return target


def build_launcher(makensis, version, build, output, icon):
    """编译固定根启动器；不改写编译产物的资源表。"""
    command = [str(makensis), "/V2", "/INPUTCHARSET", "UTF8",
               f"/DAPP_VERSION={version}",
               f"/DLAUNCHER_OUT={output}",
               f"/DICON_FILE={icon}",
               str(DESKTOP_DIR / "bootstrap.nsi")]
    print("调用 NSIS 生成固定根启动器")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode or not output.is_file():
        fail("启动器构建失败：\n" + (result.stdout or "") + (result.stderr or ""))
    return output


def build_zip(stage, output):
    """更新 ZIP：直接是版本载荷，不套一层版本目录，也不含固定根组件。"""
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    print(f"更新包：{output}（{output.stat().st_size / (1024 * 1024):.1f} MB）")


def find_nsis(explicit):
    if explicit:
        candidate = Path(explicit)
        if candidate.is_dir():
            candidate = candidate / "makensis.exe"
        if candidate.is_file():
            return candidate
        fail(f"指定的 NSIS 编译器不存在：{candidate}")
    lock = read_lock()
    archive = fetch(lock["nsis"]["url"], f"nsis-{lock['nsis']['version']}.zip")
    nsis_root = WORK_ROOT / "nsis"
    if not nsis_root.exists():
        extract(archive, nsis_root)
    candidates = list(nsis_root.rglob("makensis.exe"))
    if not candidates:
        fail("解压后的 NSIS 中没有 makensis.exe")
    return candidates[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="构建 XM-IMS Windows 便携目录、安装包与更新包")
    parser.add_argument("--portable", action="store_true",
                        help="构建到项目根目录 desktop-test，不生成安装包")
    parser.add_argument("--nsis", help="已有 NSIS 编译器所在目录或 makensis.exe 路径")
    args = parser.parse_args(argv)

    if sys.version_info[:2] != (3, 12) or platform.machine() not in ("AMD64", "x86_64"):
        fail(f"构建解释器必须是 Python 3.12 x64，当前为 {platform.python_version()} {platform.machine()}")
    lock = read_lock()
    version = app_version()
    build = build_id()
    slot = slot_name(version, build)
    reset_stage()
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)

    electron_pkg = STAGE_ROOT / "electron"
    archive = fetch(lock["electron"]["url"],
                    f"electron-v{lock['electron']['version']}-win32-x64.zip")
    extract(archive, electron_pkg)

    # 1) 主程序重命名；官方包存在 electron_wer.dll 时同步改名，否则不生成。
    electron_exe = electron_pkg / "electron.exe"
    if not electron_exe.is_file():
        fail("Electron 官方包缺少 electron.exe")
    electron_exe.replace(electron_pkg / ELECTRON_EXE)
    wer = electron_pkg / "electron_wer.dll"
    if wer.is_file():
        wer.replace(electron_pkg / "xm-ims_wer.dll")

    resources = electron_pkg / "resources"
    if (resources / "default_app.asar").exists():
        (resources / "default_app.asar").unlink()
    app_dir = resources / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    for name in DESKTOP_FILES:
        shutil.copy2(DESKTOP_DIR / name, app_dir / name)
    write_package(app_dir, version)

    # 2) 应用代码与入口放进 resources/backend；解压入口放在可执行脚本位置
    backend_dir = resources / "backend"
    copy_backend(backend_dir)
    shutil.copy2(DESKTOP_DIR / "backend_runner.py", backend_dir / "backend_runner.py")
    shutil.copy2(DESKTOP_DIR / "update_extract.py", backend_dir / "update_extract.py")

    # 3) 嵌入 Python 与依赖
    prepare_python(resources / "python", lock)

    # 4) 图标与随包许可说明
    icons = resources / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "icon.png", icons / "icon.png")
    shutil.copy2(PROJECT_ROOT / "icon.ico", icons / "icon.ico")
    licenses = resources / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "LICENSES.chromium.html"):
        source = electron_pkg / name
        if source.is_file():
            shutil.copy2(source, licenses / name)

    # 5) EXE 图标与产品版本：失败必须打断构建
    resources_writer = DESKTOP_DIR / "windows_resources.py"
    sys.path.insert(0, str(DESKTOP_DIR))
    import windows_resources
    windows_resources.write_resources(electron_pkg / ELECTRON_EXE, PROJECT_ROOT / "icon.ico",
                                      APP_NAME, "XM-IMS 进销存", version)

    # 6) 清单：先写版本清单，再写程序文件清单，让后者也登记版本清单文件，
    #    清理与卸载按清单删文件时不留残余
    version_manifest(electron_pkg, version, build, ELECTRON_EXE)
    program_manifest(electron_pkg, version)

    makensis = find_nsis(args.nsis)
    icon = str(electron_pkg / "resources" / "icons" / "icon.ico")

    if args.portable:
        launcher = build_launcher(makensis, version, build, STAGE_ROOT / "launcher.exe", icon)
        copy_bootstrap_support()
        sync_portable(electron_pkg, slot, launcher)
        print(f"完成：{PORTABLE_ROOT}")
        print(f"版本：{version}；构建：{build}；程序入口：{PORTABLE_ROOT / LAUNCHER_EXE}")
        return 0

    # 7) 固定根启动器
    launcher = build_launcher(makensis, version, build, STAGE_ROOT / "launcher.exe", icon)
    copy_bootstrap_support()

    # 8) 安装包：Setup 读暂存目录组装新布局
    output = DIST_ROOT / f"XM-IMS-{version}-win-x64-Setup.exe"
    # 脚本正文为 UTF-8（含中文提示），显式声明输入编码
    command = [str(makensis), "/V2", "/INPUTCHARSET", "UTF8",
               f"/DAPP_VERSION={version}",
               f"/DBUILD_ID={build}",
               f"/DSLOT={slot}",
               f"/DSTAGE_DIR={electron_pkg}",
               f"/DLAUNCHER_FILE={launcher}",
               f"/DSUPPORT_DIR={CACHE_ROOT / 'bootstrap-support'}",
               f"/DOUT_FILE={output}",
               str(DESKTOP_DIR / "installer.nsi")]
    print("调用 NSIS 生成安装包")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode or not output.is_file():
        fail("NSIS 构建失败：\n" + (result.stdout or "") + (result.stderr or ""))
    size = output.stat().st_size
    print(f"完成：{output}")
    print(f"版本：{version}；构建：{build}；大小：{size / (1024 * 1024):.1f} MB")

    # 9) 更新 ZIP：与 Setup 使用同一构建标识
    zip_output = DIST_ROOT / f"XM-IMS-{version}-win-x64.zip"
    build_zip(electron_pkg, zip_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
