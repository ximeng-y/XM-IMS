"""共享进程执行器与真实本地备份的回归测试。"""
import os
from pathlib import Path


def workspace():
    """只允许测试运行器准备的隔离目录，避免直接运行时使用真实店铺数据。"""
    value = os.environ.get("XM_BACKUP_TEST_ROOT")
    if not value:
        raise RuntimeError("请通过 python -B tests/run.py 运行这组测试")
    root = Path(value).resolve()
    temporary_root = (Path(__file__).resolve().parent.parent / ".XMTEMP").resolve()
    if not root.is_relative_to(temporary_root) or root == temporary_root:
        raise RuntimeError("测试目录必须位于项目 .XMTEMP 的独立子目录")
    if os.environ.get("XM_DESKTOP") != "1" or (
        Path(os.environ.get("XM_PROGRAM_ROOT", "")).resolve() != root
        or Path(os.environ.get("XM_DATA_ROOT", "")).resolve() != root / "data"
    ):
        raise RuntimeError("测试数据与程序目录未隔离")
    return root
