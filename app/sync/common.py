"""同步公共工具：同步域专用的错误构造与进程锁。

通用文件工具（JSON 编码、原子写入、受限移动和清理）已归入 app.storage.files。
进程文件锁本身落在 app.storage.process_lock，同步入口只负责把锁冲突包装成
既有的 DATA_IN_USE 错误；独立恢复入口直接使用同一份标准库实现。
"""
from ..errors import ApiError
from ..storage.process_lock import LockBusy, ProcessLock as FileProcessLock


def fail(code, message, hint=None, user_message=None, user_hint=None):
    raise ApiError(code, message, hint=hint, status=409,
                   user_message=user_message, user_hint=user_hint)


class ProcessLock:
    def __init__(self, path):
        self.lock = FileProcessLock(path)

    def acquire(self):
        try:
            self.lock.acquire()
        except LockBusy:
            fail("DATA_IN_USE", "该数据目录已有业务后端运行，请关闭重复实例",
                 user_message="这个数据目录已经在使用中",
                 user_hint="请先关闭另一个正在运行的程序窗口，再重新打开")

    def close(self):
        self.lock.close()
