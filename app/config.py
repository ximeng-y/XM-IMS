"""应用配置：路径常量与通用工具"""
from datetime import datetime
from pathlib import Path
import os

# 项目根目录（app/ 的上一级）。桌面模式下它仍是 Python 应用资源根（resources/backend），
# 不是安装根；安装根由桌面外壳单独持有，两者的区别不要混用。
BASE_DIR = Path(__file__).resolve().parent.parent

# 桌面部署：只由随安装包交付的外壳注入，源码模式（start.bat / launcher.py）不设置
DESKTOP_MODE = os.environ.get("XM_DESKTOP") == "1"

# 程序根：桌面模式为安装根（xm-ims.exe 所在目录），源码模式为应用资源根。
# 局部安装的 Git / GCM 与临时下载都围绕它定位。
PROGRAM_ROOT = Path(os.environ.get("XM_PROGRAM_ROOT") or BASE_DIR) if DESKTOP_MODE else BASE_DIR

# 临时根：桌面模式下位于安装根，源码模式仍为项目内的 .XMTEMP
TEMP_ROOT = PROGRAM_ROOT / ".XMTEMP"

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
