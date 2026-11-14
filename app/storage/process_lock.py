"""标准库进程文件锁：同步入口与独立恢复入口共用。

只依赖标准库，不导入应用错误类型或业务模块，因此可被复制到固定安装根的
bootstrap-support 下，在候选版本不可导入时仍能由旧版 Python 运行。
锁冲突用 LockBusy 表达，由各入口自行包装面向用户的错误。
"""
import os
from pathlib import Path


class LockBusy(RuntimeError):
    """同一数据目录已有另一个进程持有锁。"""


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
        except OSError as exc:
            self.file.close()
            self.file = None
            raise LockBusy("该数据目录已有业务后端运行") from exc

    def close(self):
        if self.file:
            self.file.close()
            self.file = None
