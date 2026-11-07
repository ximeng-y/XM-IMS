"""单品（SKU）管理：创建/查询/改名/换挂 SPU/自定义字段值修改

改价 = 修改价格类自定义字段的值（记审计 old/new），与改其他字段值同一路径。

创建时可带初始库存：在同一事务内补记一条 type=in 的入库流水（库存仍是流水之和）。

对外标识：SKU 的 id 呈现为「SPU 号-组内序号」（如 1-2），由 items.spu_id 与
items.seq 实时拼出；内部仍以 items.id（整数）作为流水与字段值的外键。因此换挂
SPU 时 SKU 的显示编号会随之变化，历史流水显示的是当前归属，而非当时归属。

数学计算字段：值不落库，由公式按字段定义实时算出，读接口统一经 catalog.fields
的 load_values 出口给出（values 放显示数值，calculations 放格式化文本与单元格
错误）；其值不可手填或清除，set_value 一律拒绝。
"""
from .. import config, db
from ..catalog import fields
from ..catalog.item_refs import existing_hint, existing_user_hint, format_item_ref, resolve_item_ref
from ..config import now_iso
from ..errors import ApiError
from ..inventory.ledger import append_movement
from ..schemas import ItemCreate, ItemPatch
from . import spus

# 创建单品时填了初始库存，所记那一条入库流水的备注：事后在流水里能分辨出
# 「建号时带的数量」与「后来补录的入库」，两者的业务含义不同
INITIAL_QTY_NOTE = "新建单品时的初始库存"


def _next_seq(conn, spu_id: int) -> int:
    """该 SPU 内的下一个序号。只取 max+1，不复用空位（流水里的旧编号不应指向新 SKU）"""
    row = conn.execute("SELECT MAX(seq) AS mx FROM items WHERE spu_id = ?",
                       (spu_id,)).fetchone()
    return (row["mx"] or 0) + 1


def _load_item(conn, item_id: int) -> dict | None:
    row = conn.execute(
        """SELECT i.id, i.seq, i.name, i.spu_id, s.name AS spu_name, i.created_at, i.updated_at,
                  COALESCE((SELECT SUM(m.qty) FROM movements m WHERE m.item_id = i.id), 0) AS qty
           FROM items i JOIN spus s ON s.id = i.spu_id WHERE i.id = ?""",
        (item_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    # 内部行上下文须在替换成对外编号之前交给求值器：公式关联的是 items.id
    context = {"id": item["id"], "qty": item["qty"], "spu_id": item["spu_id"]}
    values, calculations = fields.load_values(conn, [context])
    item["id"] = format_item_ref(item["spu_id"], item.pop("seq"))
    item["values"] = values[context["id"]]
    item["calculations"] = calculations[context["id"]]
    return item


def create_item(shop_id: int, payload: ItemCreate) -> dict:
    name = payload.name.strip()
    if not name:
        raise ApiError("ITEM_NAME_INVALID", "单品名不能为空白", status=400)
    null_fields = [k for k, v in payload.values.items() if v is None]
    if null_fields:
        raise ApiError("FIELD_VALUE_INVALID",
                       f"创建时字段值不能为 null：{'、'.join(null_fields)}",
                       hint="null（清除值）仅用于 PATCH 修改", status=400,
                       user_message=f"新建时这些字段不能留空值：{'、'.join(null_fields)}",
                       user_hint="新建时请直接填写字段值；清除值只在编辑已有单品时使用")
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        spu_id, spu_created = spus.resolve_spu(d.conn, payload.spu_id, payload.spu_name)
        by_name = fields.resolve_value_fields(d.conn, payload.values)
        blocked = [k for k in payload.values if by_name[k]["formula_json"]]
        if blocked:
            blocked_text = "、".join(blocked)
            raise ApiError("FIELD_READONLY",
                           f"计算字段不能直接赋值：{blocked_text}",
                           hint="计算字段的值由公式自动算出；请填写公式引用的源字段", status=409,
                           user_message=f"计算字段不能直接填写：{blocked_text}",
                           user_hint="计算字段的值由公式自动算出；请填写公式引用的源字段")
        ts = now_iso()
        cur = d.conn.execute(
            "INSERT INTO items (sync_uid, spu_id, seq, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_uid(), spu_id, _next_seq(d.conn, spu_id), name, ts, ts))
        item_id = cur.lastrowid
        for fname, value in payload.values.items():
            fields.set_value(d.conn, item_id, by_name[fname], value)
        # 初始库存与建号在同一事务内完成：要么一起留下，要么一起回滚，不会出现
        # 「有单品却没记上数量」的中间态。库存是流水之和，故这里只追加一条入库
        # 流水，不直接写任何数量字段。
        if payload.qty > 0:
            append_movement(d.conn, item_id=item_id, mtype="in", qty_delta=payload.qty,
                            note=INITIAL_QTY_NOTE, actor=config.ACTOR, created_at=ts)
        db.write_audit(d.conn, "item_create", "item", item_id,
                       {"name": name, "spu_id": spu_id, "spu_auto_created": spu_created,
                        "initial_qty": payload.qty})
        item = _load_item(d.conn, item_id)
    item["spu_auto_created"] = spu_created
    return item


def list_items(shop_id: int, offset: int = 0, limit: int = 50,
               q: str | None = None, spu_id: int | None = None,
               name: str | None = None) -> dict:
    where, params = [], []
    if q:
        where.append("i.name LIKE ?")
        params.append(f"%{q}%")
    # name 为精确匹配：前端创建单品前按「SPU + 完整名称」查重（不设唯一约束，
    # 只做创建时的一道把关）
    if name:
        where.append("i.name = ?")
        params.append(name)
    if spu_id is not None:
        where.append("i.spu_id = ?")
        params.append(spu_id)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    d = db.shop_db(shop_id)
    with d.lock:
        total = d.conn.execute(
            "SELECT COUNT(*) FROM items i JOIN spus s ON s.id = i.spu_id" + cond,
            params).fetchone()[0]
        # 按归属 SPU 与组内序号排序，与对外标识的次序一致
        rows = d.conn.execute(
            "SELECT i.id, i.seq, i.name, i.spu_id, s.name AS spu_name, i.created_at, i.updated_at, "
            "COALESCE((SELECT SUM(m.qty) FROM movements m WHERE m.item_id = i.id), 0) AS qty "
            "FROM items i JOIN spus s ON s.id = i.spu_id" + cond +
            " ORDER BY i.spu_id, i.seq LIMIT ? OFFSET ?",
            [*params, limit, offset]).fetchall()
        values, calculations = fields.load_values(d.conn, [dict(r) for r in rows])
    items = [{**dict(r), "id": format_item_ref(r["spu_id"], r["seq"]),
              "values": values[r["id"]], "calculations": calculations[r["id"]]} for r in rows]
    for it in items:
        it.pop("seq", None)
    return {"items": items, "total": total}


def get_item(shop_id: int, item_ref: str) -> dict:
    d = db.shop_db(shop_id)
    with d.lock:
        item = _load_item(d.conn, resolve_item_ref(d.conn, item_ref))
    return item


def patch_item(shop_id: int, item_ref: str, payload: ItemPatch) -> dict:
    body = payload.model_dump(exclude_unset=True)
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        item_id = resolve_item_ref(d.conn, item_ref)
        row = d.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        changed = False

        new_name = body.get("name")
        if new_name is not None:
            new_name = new_name.strip()
            if not new_name:
                raise ApiError("ITEM_NAME_INVALID", "单品名不能为空白", status=400)
            if new_name != row["name"]:
                d.conn.execute("UPDATE items SET name = ?, updated_at = ? WHERE id = ?",
                               (new_name, now_iso(), item_id))
                db.write_audit(d.conn, "item_rename", "item", item_id,
                               {"old": row["name"], "new": new_name})
                changed = True

        if body.get("spu_id") is not None or body.get("spu_name") is not None:
            new_spu, _ = spus.resolve_spu(d.conn, body.get("spu_id"), body.get("spu_name"))
            if new_spu != row["spu_id"]:
                # 换挂后落在目标 SPU 组内末尾，编号变为「新 SPU 号-新序号」
                new_seq = _next_seq(d.conn, new_spu)
                d.conn.execute(
                    "UPDATE items SET spu_id = ?, seq = ?, updated_at = ? WHERE id = ?",
                    (new_spu, new_seq, now_iso(), item_id))
                db.write_audit(d.conn, "item_spu_relink", "item", item_id,
                               {"old_spu_id": row["spu_id"], "new_spu_id": new_spu,
                                "old_ref": format_item_ref(row["spu_id"], row["seq"]),
                                "new_ref": format_item_ref(new_spu, new_seq)})
                changed = True

        values = body.get("values")
        if values:
            by_name = fields.resolve_value_fields(d.conn, values)
            for fname, v in values.items():
                detail = fields.set_value(d.conn, item_id, by_name[fname], v)
                if detail.get("action") == "clear":
                    db.write_audit(d.conn, "field_clear", "item", item_id, detail)
                else:
                    db.write_audit(d.conn, "field_value_change", "item", item_id, detail)
                changed = True

        if changed:
            d.conn.execute("UPDATE items SET updated_at = ? WHERE id = ?",
                           (now_iso(), item_id))
        item = _load_item(d.conn, item_id)
    return item


def delete_item(shop_id: int, item_ref: str, confirm: bool) -> dict:
    """删除单品（仅限从未产生过流水的 SKU）

    流水是不可变账本，一旦该 SKU 有 movement 就不能删除——否则历史流水会指向
    一个不存在的单品。有流水时按 item_type 给出可操作的纠正方向，不提供
    「连同流水一起删」的路径。
    """
    if not confirm:
        raise ApiError("CONFIRM_REQUIRED", "删除单品需要显式确认",
                       hint="删除后不可恢复；确认请附带 ?confirm=true", status=400,
                       user_message="删除单品需要再次确认",
                       user_hint="删除后不可恢复，请在确认框中确认删除")
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        item_id = resolve_item_ref(d.conn, item_ref)
        row = d.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ApiError("ITEM_NOT_FOUND", f"单品 {item_ref} 不存在",
                           hint=existing_hint(d.conn), status=404,
                           user_message=f"单品 {item_ref} 不存在",
                           user_hint=existing_user_hint(d.conn))
        used = [r["type"] for r in d.conn.execute(
            "SELECT DISTINCT type FROM movements WHERE item_id = ?", (item_id,))]
        if used:
            kinds = {"in": "入库", "out": "出库", "adjust": "盘点调整"}
            used_text = "、".join(kinds.get(t, t) for t in used)
            raise ApiError(
                "ITEM_HAS_MOVEMENTS",
                f"单品 {item_ref} 已有流水，不能删除",
                hint="已有流水类型：" + used_text +
                     "；请用反向操作或盘点调整修正库存，而不是删除该单品", status=409,
                user_message=f"单品 {item_ref} 已有流水，不能删除",
                user_hint="已有流水类型：" + used_text +
                          "；请用反向操作或盘点调整修正库存，而不是删除该单品")
        values_deleted = d.conn.execute(
            "SELECT COUNT(*) FROM custom_values WHERE item_id = ?", (item_id,)).fetchone()[0]
        # custom_values 的 item_id 外键没有级联，必须先清值再删单品
        d.conn.execute("DELETE FROM custom_values WHERE item_id = ?", (item_id,))
        db.write_audit(d.conn, "item_delete", "item", item_id,
                       {"name": row["name"], "spu_id": row["spu_id"],
                        "ref": format_item_ref(row["spu_id"], row["seq"]),
                        "deleted_values": values_deleted})
        d.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    return {"deleted": True, "item": item_ref, "name": row["name"],
            "spu_id": row["spu_id"], "deleted_values": values_deleted}
