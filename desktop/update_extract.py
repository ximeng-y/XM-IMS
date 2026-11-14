"""更新包解压入口：把完整版本 ZIP 解到同目录暂存槽并输出实际安装进度。

只使用标准库，由当前稳定版的内置 Python 以隐藏子进程运行；不启动业务服务、
不接触数据、共享工具、profile 或固定根组件。

用法：
    python -B update_extract.py --zip <更新包> --target <暂存目录> --slot <版本槽名> --slot-root <versions 目录>

进度输出（stdout，每行一次）：
    XM-IMS-EXTRACT <阶段> <已写出字节> <总字节>
阶段为 checking / extracting / done。所有载荷与元数据写完后才把暂存目录提升为
正式版本槽；中断不会留下被选择资料当作完整版本的半成品。
"""
import argparse
import json
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

MANIFEST = "version-manifest.json"
# 版本载荷只能包含程序文件；数据、工具、固定根组件与卸载器一律拒绝
FORBIDDEN_TOP = {"data", "tools", ".xmtemp", "bootstrap-support", "versions",
                 "uninstall.exe", "desktop"}
MANIFEST_REQUIRED = ("format", "version", "build", "platform", "arch", "protocol", "files")


def report(phase, done=0, total=0, extra=""):
    line = f"XM-IMS-EXTRACT {phase} {done} {total}"
    print(line + (f" {extra}" if extra else ""), flush=True)


def fail(message):
    print(f"XM-IMS-EXTRACT failed 0 0 {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def safe_entries(archive):
    """检查每个成员的相对路径、目录越界、符号链接与重复目标。"""
    seen = set()
    entries = []
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if (path.is_absolute() or not path.parts or "\\" in name or ":" in path.parts[0]
                or any(part in (".", "..") or part.endswith((" ", ".")) for part in path.parts)):
            fail("更新包包含不安全路径")
        if stat.S_ISLNK(info.external_attr >> 16):
            fail("更新包包含符号链接")
        if path.parts[0].casefold() in FORBIDDEN_TOP:
            fail("更新包包含不允许的目录或文件")
        key = name.casefold()
        if key in seen:
            fail("更新包包含重复路径")
        seen.add(key)
        entries.append((info, path))
    return entries


def check_manifest(archive, names):
    """ZIP 必须带完整版本清单，且清单登记的载荷相对路径全部存在。"""
    if MANIFEST not in names:
        fail("更新包缺少版本清单")
    try:
        manifest = json.loads(archive.read(MANIFEST).decode("utf-8"))
    except (KeyError, ValueError):
        fail("更新包版本清单无法读取")
    if not isinstance(manifest, dict) or any(key not in manifest for key in MANIFEST_REQUIRED):
        fail("更新包版本清单缺少字段")
    if manifest["format"] != 1 or manifest["protocol"] != 1:
        fail("更新包版本清单格式不支持")
    if manifest["platform"] != "win32" or manifest["arch"] != "x64":
        fail("更新包与当前系统不匹配")
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        fail("更新包版本清单缺少文件列表")
    missing = sorted(name for name in files if name not in names)
    if missing:
        fail("更新包缺少版本清单登记的文件")
    return manifest


def promote(staging, slot_root, slot):
    """暂存目录提升为完整版本槽；同名槽已存在时不覆盖。"""
    target = slot_root / slot
    if target.exists():
        raise RuntimeError("同名版本槽已存在")
    slot_root.mkdir(parents=True, exist_ok=True)
    os.replace(staging, target)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="解压 XM-IMS 更新包")
    parser.add_argument("--zip", required=True)
    parser.add_argument("--target", required=True, help="暂存目录（将被提升为版本槽）")
    parser.add_argument("--slot", required=True)
    parser.add_argument("--slot-root", required=True)
    args = parser.parse_args(argv)

    archive_path = os.path.abspath(args.zip)
    staging = os.path.abspath(args.target)
    slot_root = Path(os.path.abspath(args.slot_root))
    if not os.path.isfile(archive_path):
        fail("更新包不存在")
    if os.path.exists(staging):
        shutil.rmtree(staging)
    with zipfile.ZipFile(archive_path) as archive:
        entries = safe_entries(archive)
        report("checking")
        names = {info.filename for info, _ in entries}
        check_manifest(archive, names)
        total = sum(info.file_size for info, _ in entries if not info.is_dir()) or 1
        done = 0
        for info, path in entries:
            destination = os.path.join(staging, *path.parts)
            if not os.path.abspath(destination).startswith(staging + os.sep):
                fail("更新包路径越界")
            if info.is_dir():
                os.makedirs(destination, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with archive.open(info) as source, open(destination, "wb") as output:
                while True:
                    block = source.read(1024 * 256)
                    if not block:
                        break
                    output.write(block)
                    done += len(block)
                    report("extracting", done, total)
    try:
        promote(staging, slot_root, args.slot)
    except (OSError, RuntimeError) as error:
        fail(str(error))
    report("done", done, total)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
