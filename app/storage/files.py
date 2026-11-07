"""确定性 JSON 编码与受限文件操作。

同步、更新与引导共用这些工具：原子写入、目录内移动、目录内清理。
不做通用的临时文件管理，也不判断调用方的业务语义。
"""
import json
import os
import shutil
import time
from pathlib import Path


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("w", encoding="utf-8", newline="\n") as out:
        out.write(dump(value) + "\n")
        out.flush()
        os.fsync(out.fileno())
    replace_path(temporary, path, path.parent)


def replace_path(source, target, workspace):
    """仅在指定目录内移动；短暂文件占用时有限重试，失败保留源文件。"""
    source, target, workspace = Path(source).resolve(), Path(target).resolve(), Path(workspace).resolve()
    if any(p == workspace or not p.is_relative_to(workspace) for p in (source, target)):
        raise ValueError("移动路径必须位于指定数据目录内")
    for attempt in range(3):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))


def remove_tree(target, workspace):
    target, workspace = Path(target).resolve(), Path(workspace).resolve()
    if target == workspace or not target.is_relative_to(workspace):
        raise ValueError("清理路径必须位于专用数据目录内")
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        def writable(function, filename, exc):
            os.chmod(filename, 0o700)
            function(filename)
        shutil.rmtree(target, onerror=writable)
