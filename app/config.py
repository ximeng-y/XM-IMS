"""应用配置：路径常量与通用工具"""
from datetime import datetime
from pathlib import Path
import os

# 项目根目录（app/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent

# 旧 data/index.db 与 data/shops 不自动读取；活动代次由同步管理器选择
DATA_ROOT = Path(os.environ.get("XM_DATA_ROOT", str(BASE_DIR / "data")))
DATA_DIR = DATA_ROOT / "runtime" / "uninitialized"
SHOPS_DIR = DATA_DIR / "shops"
INDEX_DB = DATA_DIR / "index.db"

def use_generation(directory):
    global DATA_DIR, SHOPS_DIR, INDEX_DB
    DATA_DIR = Path(directory)
    SHOPS_DIR = DATA_DIR / "shops"
    INDEX_DB = DATA_DIR / "index.db"

# 当前轮次无登录体系，actor 固定占位；后续接入账号/Token 后替换
ACTOR = "local"


def now_iso() -> str:
    """统一时间格式：ISO8601 本地时区带偏移"""
    return datetime.now().astimezone().isoformat()
