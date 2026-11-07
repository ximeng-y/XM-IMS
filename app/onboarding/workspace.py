"""引导专用临时目录：下载、探测与候选检查各用独立子目录，用完即清理。

仍使用原引导专用目录（项目根 .XMTEMP/sync-onboarding），不改用系统临时目录，
也不建立新的全局临时文件管理框架。
"""
import uuid
from pathlib import Path

from .. import config
from ..storage.files import remove_tree


class Workspace:
    """引导临时工作区；root 为原专用目录，子目录按标签隔离。"""

    def __init__(self, root=None):
        self.root = Path(root) if root else config.BASE_DIR / ".XMTEMP" / "sync-onboarding"

    def root_dir(self):
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def work_dir(self, label):
        work = self.root_dir() / label / uuid.uuid4().hex
        work.mkdir(parents=True)
        return work

    def cleanup(self, work):
        remove_tree(work, self.root_dir())
