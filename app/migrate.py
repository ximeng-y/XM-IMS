"""店铺库版本迁移

用 SQLite 的 `PRAGMA user_version` 记录结构版本，店铺库每次打开时按版本逐步升级。
新建库由 SHOP_DDL 直接建成当前版本，不经过迁移。

约定：每步迁移只依赖「上一步已完成」的状态；迁移与 user_version 写入在同一事务内，
中途失败则整体回滚，下次打开重跑。
"""
from . import schema

# 当前店铺库结构版本；SHOP_DDL 建出的新库直接标记为该值
CURRENT_VERSION = 2

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
    """
    conn = database.conn
    with database.lock:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= CURRENT_VERSION:
            return
        if version == 0:
            # 建库时尚未记录版本的骨架库，结构等同 v1 基线
            version = BASELINE_VERSION
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
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
