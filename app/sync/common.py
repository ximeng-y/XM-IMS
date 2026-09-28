"""同步公共工具：原子文件、进程锁和受限清理。"""
import contextlib
import json
import os
import shutil
from pathlib import Path
from ..errors import ApiError


def fail(code, message, hint=None):
    raise ApiError(code, message, hint=hint, status=409)


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
    os.replace(temporary, path)


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


class ProcessLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            fail("DATA_IN_USE", "该数据目录已有业务后端运行，请关闭重复实例")

    def close(self):
        if self.file:
            self.file.close()
            self.file = None
