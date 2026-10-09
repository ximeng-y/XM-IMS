"""数据库连接管理：索引库与店铺库按整套代次切换。

- 全部连接查找、事务和快照捕获共用可重入协调锁
- 每个连接开启 WAL 与外键约束，过期代次的连接拒绝继续访问
- 库存数量由 SUM(movements.qty) 计算，不存储可变库存列
- 新版同步目录只接受当前结构版本，不自动打开旧目录数据库
"""
import json
import sqlite3
import threading
import contextlib
import uuid

from . import config, schema
from .errors import ApiError
from .migrate import init_shop_db, upgrade_shop_db


_coordinator = threading.RLock()
_internal = threading.local()
business_ready = False
_write_epoch = 0
_generation = 0


@contextlib.contextmanager
def internal_access():
    previous = getattr(_internal, "allowed", False)
    _internal.allowed = True
    try:
        yield
    finally:
        _internal.allowed = previous


def set_ready(ready):
    global business_ready
    with _coordinator:
        business_ready = ready


def require_ready():
    if not business_ready and not getattr(_internal, "allowed", False):
        raise ApiError("SYNC_REQUIRED", "请先完成初始化或数据同步", status=503)


class DatabaseLock:
    def __init__(self, database):
        self.database = database

    def __enter__(self):
        _coordinator.acquire()
        try:
            require_ready()
            if self.database.generation != _generation:
                raise ApiError("SYNC_RETRY", "数据版本已更新，请重新执行操作", status=409)
            self.before = getattr(self, "before", [])
            self.before.append(self.database.conn.total_changes)
        except BaseException:
            _coordinator.release()
            raise
        return self

    def __exit__(self, *args):
        global _write_epoch
        if self.database.conn.total_changes != self.before.pop():
            _write_epoch += 1
        _coordinator.release()


def coordinated(function):
    """连接查找和创建也纳入代次锁，避免切换期间打开混合版本。"""
    import functools
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with _coordinator:
            require_ready()
            return function(*args, **kwargs)
    return wrapped


def new_uid():
    return uuid.uuid4().hex


def close_all():
    global _index_db, _generation
    with _coordinator:
        for database in [_index_db, *_shop_dbs.values()]:
            if database:
                database.conn.close()
        _shop_dbs.clear()
        _index_db = None
        _generation += 1


def epoch():
    return (_generation, _write_epoch)


class Database:
    """单库连接 + 互斥锁"""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.generation = _generation
        self.lock = DatabaseLock(self)


_index_db: Database | None = None
_shop_dbs: dict[int, Database] = {}
_cache_lock = threading.Lock()


@coordinated
def init() -> None:
    """应用启动时调用：建数据目录、建索引库表"""
    global _index_db
    require_ready()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.SHOPS_DIR.mkdir(parents=True, exist_ok=True)
    existing = config.INDEX_DB.exists()
    _index_db = Database(config.INDEX_DB)
    if existing and _index_db.conn.execute("PRAGMA user_version").fetchone()[0] != 1:
        _index_db.conn.close()
        _index_db = None
        raise ApiError("SCHEMA_UNSUPPORTED", "索引库版本不兼容", status=409)
    with _index_db.lock, _index_db.conn:
        _index_db.conn.executescript(schema.INDEX_DDL)
        _index_db.conn.execute("PRAGMA user_version=1")


@coordinated
def index_db() -> Database:
    if _index_db is None:
        init()
    return _index_db


@coordinated
def get_shop_row(shop_id: int) -> sqlite3.Row | None:
    idx = index_db()
    with idx.lock:
        return idx.conn.execute("SELECT * FROM shops WHERE id = ?", (shop_id,)).fetchone()


@coordinated
def shop_db(shop_id: int) -> Database:
    """取店铺库连接（带缓存）；店铺不存在或库文件缺失时抛 404"""
    with _cache_lock:
        cached = _shop_dbs.get(shop_id)
    if cached is not None:
        return cached
    row = get_shop_row(shop_id)
    if row is None:
        raise ApiError("SHOP_NOT_FOUND", f"店铺 {shop_id} 不存在",
                       hint="GET /api/v1/shops 查看店铺列表", status=404,
                       user_message=f"店铺 {shop_id} 不存在",
                       user_hint="该店铺可能已被删除，请在顶栏的「店铺」下拉框中重新选择")
    path = config.DATA_DIR / row["db_file"]
    if not path.is_file():
        raise ApiError("SHOP_NOT_FOUND", f"店铺 {shop_id} 的数据库文件缺失：{row['db_file']}",
                       hint="请从备份恢复或检查 data/shops/ 目录", status=404,
                       user_message=f"店铺「{row['name']}」的数据文件缺失",
                       user_hint="请用备份或数据同步恢复该店铺的数据后再打开")
    db = Database(path)
    try:
        upgrade_shop_db(db)
    except BaseException:
        db.conn.close()
        raise
    with _cache_lock:
        # 并发下可能已有他人放入，统一取缓存实例
        existing = _shop_dbs.setdefault(shop_id, db)
        if existing is not db:
            db.conn.close()
        db = existing
    return db


@coordinated
def create_shop_db_file(shop_id: int, db_file: str) -> Database:
    """按模板创建一个新的店铺库文件并纳入缓存"""
    db = Database(config.DATA_DIR / db_file)
    init_shop_db(db)
    with _cache_lock:
        _shop_dbs[shop_id] = db
    return db


def write_audit(conn: sqlite3.Connection, action: str, entity_type: str,
                entity_id: int | None, detail: dict, entity_uid: str | None = None) -> None:
    """主数据变更审计，与业务变更在同一事务内调用"""
    table = {"item": "items", "spu": "spus", "field": "custom_fields"}.get(entity_type)
    if table and entity_uid is None:
        record = conn.execute(f"SELECT sync_uid FROM {table} WHERE id=?", (entity_id,)).fetchone()
        entity_uid = record[0] if record else None
    conn.execute(
        "INSERT INTO audit_log (sync_uid, actor, action, entity_type, entity_id, entity_uid, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_uid(), config.ACTOR, action, entity_type, entity_id, entity_uid,
         json.dumps(detail, ensure_ascii=False), config.now_iso()),
    )
