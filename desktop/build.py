"""Windows 桌面发行版构建：组装运行时、依赖、便携目录或安装包。

用法（固定入口）：
    .venv\\Scripts\\python.exe -B desktop\\build.py [--portable]
    .venv\\Scripts\\python.exe -B desktop\\build.py [--nsis <makensis.exe 所在目录>]

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

# 打进 resources/backend 的源码范围：明确列出，不递归拷整个仓库。
BACKEND_EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}
BACKEND_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite"}
DESKTOP_FILES = ("main.js", "preload.js", "paths.js", "backend.js", "update.js",
                 "backend_runner.py")


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
    """安装器管理的程序文件清单：EXE、官方运行文件与 resources 下的文件。

    不列 data、tools、.XMTEMP 或用户文件；清单自身与 uninstall.exe 单独登记。
    """
    lines = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file():
            continue
        # 清单交给 NSIS 后直接拼进 Delete，必须使用 Windows 反斜杠分隔符
        relative = str(path.relative_to(stage))
        if relative in ("installed-program-files.txt", "uninstall.exe"):
            continue
        lines.append(relative)
    lines.extend(["installed-program-files.txt", "uninstall.exe"])
    target = stage / "installed-program-files.txt"
    # 不带 BOM 的 UTF-16LE；NSIS 侧用 FileReadUTF16LE 读取。
    target.write_bytes("\r\n".join(lines).encode("utf-16-le"))
    print(f"程序文件 {len(lines)} 项，版本 {version}")


def read_program_manifest(target):
    """读取便携目录上一版程序文件；用户数据不在清单内。"""
    manifest = target / "installed-program-files.txt"
    if not manifest.is_file():
        return []
    return [line.strip() for line in manifest.read_text(encoding="utf-16-le").splitlines()
            if line.strip()]


def sync_portable(stage):
    """把暂存程序同步到固定便携目录，保留 data、tools 与 .XMTEMP。"""
    target = PORTABLE_ROOT
    target.mkdir(parents=True, exist_ok=True)

    # 先按上一版清单删除程序文件，避免旧版本残留；用户数据从未列入清单。
    for relative in read_program_manifest(target):
        item = Path(relative)
        if item.is_absolute() or ".." in item.parts:
            fail(f"便携目录程序清单包含非法路径：{relative!r}")
        path = target / item
        if path.is_file() or path.is_symlink():
            path.unlink()

    # 顶层程序项整体替换；data、tools、.XMTEMP 和用户自有文件不在此循环中。
    for source in stage.iterdir():
        if source.name == "installed-program-files.txt":
            continue
        destination = target / source.name
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        elif destination.exists() or destination.is_symlink():
            destination.unlink()
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)

    shutil.copy2(stage / "installed-program-files.txt",
                 target / "installed-program-files.txt")
    print(f"便携测试版已更新：{target}")


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
    parser = argparse.ArgumentParser(description="构建 XM-IMS Windows 便携目录或安装包")
    parser.add_argument("--portable", action="store_true",
                        help="构建到项目根目录 desktop-test，不生成安装包")
    parser.add_argument("--nsis", help="已有 NSIS 编译器所在目录或 makensis.exe 路径")
    args = parser.parse_args(argv)

    if sys.version_info[:2] != (3, 12) or platform.machine() not in ("AMD64", "x86_64"):
        fail(f"构建解释器必须是 Python 3.12 x64，当前为 {platform.python_version()} {platform.machine()}")
    lock = read_lock()
    version = app_version()
    reset_stage()

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

    # 2) 应用代码与入口放进 resources/backend
    backend_dir = resources / "backend"
    copy_backend(backend_dir)
    shutil.copy2(DESKTOP_DIR / "backend_runner.py", backend_dir / "backend_runner.py")

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

    program_manifest(electron_pkg, version)

    if args.portable:
        sync_portable(electron_pkg)
        print(f"完成：{PORTABLE_ROOT}")
        print(f"版本：{version}；程序入口：{PORTABLE_ROOT / ELECTRON_EXE}")
        return 0

    # 6) 安装包
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    makensis = find_nsis(args.nsis)
    output = DIST_ROOT / f"XM-IMS-{version}-win-x64-Setup.exe"
    # 脚本正文为 UTF-8（含中文提示），显式声明输入编码
    command = [str(makensis), "/V2", "/INPUTCHARSET", "UTF8",
               f"/DAPP_VERSION={version}",
               f"/DSTAGE_DIR={electron_pkg}",
               f"/DOUT_FILE={output}",
               str(DESKTOP_DIR / "installer.nsi")]
    print("调用 NSIS 生成安装包")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode or not output.is_file():
        fail("NSIS 构建失败：\n" + (result.stdout or "") + (result.stderr or ""))
    size = output.stat().st_size
    print(f"完成：{output}")
    print(f"版本：{version}；大小：{size / (1024 * 1024):.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
