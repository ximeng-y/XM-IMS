"""SPU（顶层分类）管理：创建、列表（含 SKU 计数）、改名（一处改全局生效）"""
from .. import db
from ..config import now_iso
from ..errors import ApiError


def create_spu(shop_id: int, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ApiError("SPU_NAME_INVALID", "SPU 名称不能为空白", status=400)
    d = db.shop_db(shop_id)
    ts = now_iso()
    with d.lock, d.conn:
        cur = d.conn.execute(
            "INSERT INTO spus (name, created_at, updated_at) VALUES (?, ?, ?)", (name, ts, ts))
        spu_id = cur.lastrowid
        db.write_audit(d.conn, "spu_create", "spu", spu_id, {"name": name})
    return {"id": spu_id, "name": name, "sku_count": 0, "created_at": ts, "updated_at": ts}


def list_spus(shop_id: int) -> dict:
    d = db.shop_db(shop_id)
    with d.lock:
        rows = d.conn.execute(
            """SELECT s.id, s.name, s.created_at, s.updated_at, COUNT(i.id) AS sku_count
               FROM spus s LEFT JOIN items i ON i.spu_id = s.id
               GROUP BY s.id ORDER BY s.id""").fetchall()
    return {"spus": [dict(r) for r in rows]}


def rename_spu(shop_id: int, spu_id: int, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ApiError("SPU_NAME_INVALID", "SPU 名称不能为空白", status=400)
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = d.conn.execute("SELECT * FROM spus WHERE id = ?", (spu_id,)).fetchone()
        if row is None:
            raise ApiError("SPU_NOT_FOUND", f"SPU {spu_id} 不存在",
                           hint="GET /api/v1/spus 查看现有 SPU", status=404)
        ts = now_iso()
        d.conn.execute("UPDATE spus SET name = ?, updated_at = ? WHERE id = ?", (name, ts, spu_id))
        db.write_audit(d.conn, "spu_rename", "spu", spu_id,
                       {"old": row["name"], "new": name})
        sku_count = d.conn.execute(
            "SELECT COUNT(*) FROM items WHERE spu_id = ?", (spu_id,)).fetchone()[0]
    return {"id": spu_id, "name": name, "sku_count": sku_count,
            "created_at": row["created_at"], "updated_at": ts}


def resolve_spu(conn, spu_id: int | None, spu_name: str | None) -> tuple[int, bool]:
    """解析单品应挂载的 SPU：spu_id 优先；spu_name 精确匹配则复用，无则自动新建。

    返回 (spu_id, 是否新建)。必须在已持店铺锁的事务内调用。
    """
    if spu_id is not None:
        row = conn.execute("SELECT id FROM spus WHERE id = ?", (spu_id,)).fetchone()
        if row is None:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM spus ORDER BY id LIMIT 5")]
            hint = ("现有 SPU：" + "、".join(names)) if names else \
                "该店铺还没有 SPU，可通过 spu_name 自动创建"
            raise ApiError("SPU_NOT_FOUND", f"SPU {spu_id} 不存在", hint=hint, status=404)
        return spu_id, False
    if spu_name:
        spu_name = spu_name.strip()
        row = conn.execute("SELECT id FROM spus WHERE name = ? ORDER BY id LIMIT 1",
                           (spu_name,)).fetchone()
        if row:
            return row["id"], False
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO spus (name, created_at, updated_at) VALUES (?, ?, ?)",
            (spu_name, ts, ts))
        db.write_audit(conn, "spu_create", "spu", cur.lastrowid,
                       {"name": spu_name, "via": "item_auto_create"})
        return cur.lastrowid, True
    raise ApiError("ITEM_SPU_REQUIRED", "单品必须指定 SPU",
                   hint="请求体中 spu_id 与 spu_name 二选一必填；"
                        "spu_name 精确匹配已有 SPU 则复用，否则自动创建", status=400)
