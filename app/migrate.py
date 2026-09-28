"""同步数据目录的店铺库版本检查与新库初始化。

新版库使用持久同步标识，结构版本为 3；旧目录不自动迁移。
历史迁移函数保留供旧版本维护参考，当前入口只接受当前版本。
"""
from . import schema

# 当前店铺库结构版本；SHOP_DDL 建出的新库直接标记为该值
CURRENT_VERSION = 3

# v0 表示建库时尚未记录版本（最早的骨架库），结构等同 v1 基线
BASELINE_VERSION = 1


def _columns(conn, table: str) -> set:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_shop_db(database) -> None:
    """建表并标定版本（仅新库调用）

    SHOP_DDL 全部是 CREATE ... IF NOT EXISTS，重复执行幂等，故直接标当前版本。
    已存在的旧版本库不能走这里——那会跳过迁移，须走 upgrade_shop_db。
    """
    conn = database.conn
    with database.lock, conn:
        conn.executescript(schema.SHOP_DDL)
        conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")


def upgrade_shop_db(database) -> None:
    """按 user_version 把已有店铺库升到 CURRENT_VERSION

    迁移显式开一个事务：Python sqlite3 只在 DML 前隐式开事务，纯 DDL 的迁移
    不会自动获得原子性，因此这里用 BEGIN IMMEDIATE 自己开，保证「结构变更 +
    版本号」要么都生效、要么都回滚。

    版本号必须在拿到写锁之后再读：db.shop_db() 在缓存未命中时会并发各建一个
    连接，两个连接都会走到这里，若在锁外读版本，后到的那个会拿着过期的版本号
    重复执行 ADD COLUMN，撞上 duplicate column name。
    """
    conn = database.conn
    with database.lock:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > CURRENT_VERSION:
                raise ValueError("数据库来自较新版本，请升级应用")
            if version < CURRENT_VERSION:
                raise ValueError("此版本使用带同步标识的新库，不自动迁移旧数据库")
            if version == CURRENT_VERSION:
                conn.commit()
                return
            if version == 0:
                # 建库时尚未记录版本的骨架库，结构等同 v1 基线
                version = BASELINE_VERSION
            for target in range(version + 1, CURRENT_VERSION + 1):
                UPGRADES[target](conn)
                # PRAGMA 不接受参数占位，target 是循环内的整数
                conn.execute(f"PRAGMA user_version = {target}")
        except Exception:
            conn.rollback()
            raise
        conn.commit()


# ---------- 迁移步骤：键为「升级到的目标版本」 ----------


def _v2_sku_seq(conn) -> None:
    """v1 → v2：items 增加 seq 列，构建对外标识「SPU 号-组内序号」

    回填口径与旧数据的整数 ID 顺序一致：按每个 SPU 内的 id 升序分配 1..n，
    与「重建 ID」的结果相同，但流水与字段值仍挂在原 items.id 上，无需重建。
    """
    if "seq" not in _columns(conn, "items"):
        conn.execute("ALTER TABLE items ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")
    counter: dict[int, int] = {}
    for r in conn.execute("SELECT id, spu_id FROM items ORDER BY spu_id, id").fetchall():
        counter[r["spu_id"]] = counter.get(r["spu_id"], 0) + 1
        conn.execute("UPDATE items SET seq = ? WHERE id = ?", (counter[r["spu_id"]], r["id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_spu_seq ON items(spu_id, seq)")


UPGRADES = {2: _v2_sku_seq}
