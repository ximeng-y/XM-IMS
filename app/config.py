"""应用配置：路径常量与通用工具"""
from datetime import datetime
from pathlib import Path

# 项目根目录（app/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent

# 数据目录：索引库 + 各店铺库（已进 .gitignore，代码与数据彻底分离）
DATA_DIR = BASE_DIR / "data"
SHOPS_DIR = DATA_DIR / "shops"
INDEX_DB = DATA_DIR / "index.db"

# 当前轮次无登录体系，actor 固定占位；后续接入账号/Token 后替换
ACTOR = "local"


def now_iso() -> str:
    """统一时间格式：ISO8601 本地时区带偏移"""
    return datetime.now().astimezone().isoformat()
