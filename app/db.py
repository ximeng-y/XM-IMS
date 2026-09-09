"""数据库连接管理：索引库 + 各店铺库连接缓存

- 每个库一个 sqlite3 连接（check_same_thread=False）+ 一把线程锁，
  同一店铺的所有操作持锁串行执行，FastAPI 同步端点跑线程池时保证写安全
- 每个连接开启 WAL 与外键约束
- 库存数量永不落列：读取一律 SUM(movements.qty)（见 service/inventory.py）
"""
import json
import sqlite3
import threading

from . import config, schema
from .errors import ApiError


class Database:
    """单库连接 + 互斥锁"""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.Lock()


_index_db: Database | None = None
_shop_dbs: dict[int, Database] = {}
_cache_lock = threading.Lock()


def init() -> None:
    """应用启动时调用：建数据目录、建索引库表"""
    global _index_db
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SHOPS_DIR.mkdir(parents=True, exist_ok=True)
    _index_db = Database(config.INDEX_DB)
    with _index_db.lock, _index_db.conn:
        _index_db.conn.executescript(schema.INDEX_DDL)


def index_db() -> Database:
    if _index_db is None:
        init()
    return _index_db


def get_shop_row(shop_id: int) -> sqlite3.Row | None:
    idx = index_db()
    with idx.lock:
        return idx.conn.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()


def shop_db(shop_id: int) -> Database:
    """取店铺库连接（带缓存）；店铺不存在或库文件缺失时抛 404"""
    with _cache_lock:
        cached = _shop_dbs.get(shop_id)
    if cached is not None:
        return cached
    row = get_shop_row(shop_id)
    if row is None:
        raise ApiError("SHOP_NOT_FOUND", f"店铺 {shop_id} 不存在",
                       hint="GET /api/v1/shops 查看店铺列表", status=404)
    path = config.DATA_DIR / row["db_file"]
    if not path.is_file():
        raise ApiError("SHOP_NOT_FOUND", f"店铺 {shop_id} 的数据库文件缺失：{row['db_file']}",
                       hint="请从备份恢复或检查 data/shops/ 目录", status=404)
    db = Database(path)
    with _cache_lock:
        # 并发下可能已有他人放入，统一取缓存实例
        db = _shop_dbs.setdefault(shop_id, db)
    return db


def create_shop_db_file(shop_id: int, db_file: str) -> Database:
    """按模板创建一个新的店铺库文件并纳入缓存"""
    db = Database(config.DATA_DIR / db_file)
    with db.lock, db.conn:
        db.conn.executescript(schema.SHOP_DDL)
    with _cache_lock:
        _shop_dbs[shop_id] = db
    return db


def write_audit(conn: sqlite3.Connection, action: str, entity_type: str,
                entity_id: int | None, detail: dict) -> None:
    """主数据变更审计，与业务变更在同一事务内调用"""
    conn.execute(
        "INSERT INTO audit_log (actor, action, entity_type, entity_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (config.ACTOR, action, entity_type, entity_id,
         json.dumps(detail, ensure_ascii=False), config.now_iso()),
    )
