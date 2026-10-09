"""SPU（顶层分类）管理：创建、列表（含 SKU 计数）、改名（一处改全局生效）、删除"""
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
            "INSERT INTO spus (sync_uid, name, created_at, updated_at) VALUES (?, ?, ?, ?)", (db.new_uid(), name, ts, ts))
        spu_id = cur.lastrowid
        db.write_audit(d.conn, "spu_create", "spu", spu_id, {"name": name})
    return {"id": spu_id, "name": name, "sku_count": 0, "created_at": ts, "updated_at": ts}


def list_spus(shop_id: int) -> dict:
    """SPU 列表：每个 SPU 带 SKU 数与库存合计。

    流水先按 SKU 聚合再与 SKU 关联，避免某一 SKU 有多条流水时把 SKU 计数放大。
    仍是一次查询返回全部 SPU，不逐个 SPU 查询，也不把派生库存落库。
    """
    d = db.shop_db(shop_id)
    with d.lock:
        rows = d.conn.execute(
            """SELECT s.id, s.name, s.created_at, s.updated_at,
                      COUNT(i.id) AS sku_count, COALESCE(SUM(q.total), 0) AS total_qty
               FROM spus s
               LEFT JOIN items i ON i.spu_id = s.id
               LEFT JOIN (SELECT item_id, SUM(qty) AS total FROM movements GROUP BY item_id) q
                      ON q.item_id = i.id
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
                           hint="GET /api/v1/spus 查看现有 SPU", status=404,
                           user_message=f"SPU {spu_id} 不存在",
                           user_hint="该 SPU 可能已被删除，请刷新页面或改用「选择已有 SPU」")
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
            user_hint = ("现有 SPU：" + "、".join(names)) if names else \
                "该店铺还没有 SPU，可在「SPU 名称」中输入新名称自动创建"
            raise ApiError("SPU_NOT_FOUND", f"SPU {spu_id} 不存在", hint=hint, status=404,
                           user_message=f"SPU {spu_id} 不存在", user_hint=user_hint)
        return spu_id, False
    if spu_name:
        spu_name = spu_name.strip()
        row = conn.execute("SELECT id FROM spus WHERE name = ? ORDER BY id LIMIT 1",
                           (spu_name,)).fetchone()
        if row:
            return row["id"], False
        ts = now_iso()
        cur = conn.execute(
            "INSERT INTO spus (sync_uid, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (db.new_uid(), spu_name, ts, ts))
        db.write_audit(conn, "spu_create", "spu", cur.lastrowid,
                       {"name": spu_name, "via": "item_auto_create"})
        return cur.lastrowid, True
    raise ApiError("ITEM_SPU_REQUIRED", "单品必须指定 SPU",
                   hint="请求体中 spu_id 与 spu_name 二选一必填；"
                        "spu_name 精确匹配已有 SPU 则复用，否则自动创建", status=400,
                   user_message="请填写或选择 SPU",
                   user_hint="请在下拉框中选择已有 SPU，或输入一个新名称自动创建")


def delete_spu(shop_id: int, spu_id: int, confirm: bool) -> dict:
    """删除 SPU（仅限其下已无任何 SKU）

    SPU 没有自己的流水，删除条件只取决于是否还挂着单品；有单品时让调用方
    先处理单品，避免出现「单品挂在不存在的 SPU 上」。
    """
    if not confirm:
        raise ApiError("CONFIRM_REQUIRED", "删除 SPU 需要显式确认",
                       hint="删除后不可恢复；确认请附带 ?confirm=true", status=400,
                       user_message="删除 SPU 需要再次确认",
                       user_hint="删除后不可恢复，请在确认框中确认删除")
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = d.conn.execute("SELECT * FROM spus WHERE id = ?", (spu_id,)).fetchone()
        if row is None:
            raise ApiError("SPU_NOT_FOUND", f"SPU {spu_id} 不存在",
                           hint="GET /api/v1/spus 查看现有 SPU", status=404,
                           user_message=f"SPU {spu_id} 不存在",
                           user_hint="该 SPU 可能已被删除，请刷新页面后重试")
        sku_count = d.conn.execute(
            "SELECT COUNT(*) FROM items WHERE spu_id = ?", (spu_id,)).fetchone()[0]
        if sku_count:
            raise ApiError("SPU_HAS_ITEMS", f"SPU「{row['name']}」下还有 {sku_count} 个单品，不能删除",
                           hint="请先删除该 SPU 下的全部单品（仅从未产生过流水的单品可删除）", status=409,
                           user_message=f"SPU「{row['name']}」下还有 {sku_count} 个单品，不能删除",
                           user_hint="请先删除该 SPU 下的全部单品（仅从未产生过流水的单品可删除）")
        db.write_audit(d.conn, "spu_delete", "spu", spu_id, {"name": row["name"]})
        d.conn.execute("DELETE FROM spus WHERE id = ?", (spu_id,))
    return {"deleted": True, "spu_id": spu_id, "name": row["name"]}
