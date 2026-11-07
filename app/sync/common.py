"""同步公共工具：同步域专用的错误构造与进程锁。

通用文件工具（JSON 编码、原子写入、受限移动和清理）已归入 app.storage.files。
"""
import os
from pathlib import Path
from ..errors import ApiError


def fail(code, message, hint=None, user_message=None, user_hint=None):
    raise ApiError(code, message, hint=hint, status=409,
                   user_message=user_message, user_hint=user_hint)


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
            fail("DATA_IN_USE", "该数据目录已有业务后端运行，请关闭重复实例",
                 user_message="这个数据目录已经在使用中",
                 user_hint="请先关闭另一个正在运行的程序窗口，再重新打开")

    def close(self):
        if self.file:
            self.file.close()
            self.file = None
