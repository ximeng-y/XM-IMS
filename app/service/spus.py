"""SPU 管理：创建、详情、列表、属性更新、改名与删除。

SPU 自身没有库存流水，库存仍由所属 SKU 的不可变流水汇总得到。SPU 普通属性
存放于 spu_custom_values，计算属性由 catalog.fields 按所属 SPU 上下文求值。
"""
from .. import db
from ..catalog import fields
from ..catalog.item_refs import format_item_ref
from ..config import now_iso
from ..errors import ApiError


def _ensure_name_free(conn, name: str, exclude_id: int | None = None) -> None:
    """SPU 名称在店内唯一；改名时用 exclude_id 排除自身。"""
    row = conn.execute("SELECT id FROM spus WHERE name = ? AND id IS NOT ?",
                       (name, exclude_id)).fetchone()
    if row is not None:
        raise ApiError("SPU_NAME_EXISTS", f"SPU「{name}」已存在",
                       hint="SPU 名称在店内唯一；请改用已有 SPU 或换一个名称", status=409,
                       user_message=f"SPU「{name}」已存在",
                       user_hint="请直接使用已有的这个 SPU，或换一个名称")


def _validate_create_values(values: dict) -> None:
    null_fields = [key for key, value in values.items() if value is None]
    if null_fields:
        raise ApiError(
            "FIELD_VALUE_INVALID",
            f"创建时字段值不能为 null：{'、'.join(null_fields)}",
            hint="null（清除值）仅用于 PATCH 修改", status=400,
            user_message=f"新建时这些字段不能留空值：{'、'.join(null_fields)}",
            user_hint="新建时请直接填写字段值；清除值只在编辑已有 SPU 时使用")


def _resolve_writable_values(conn, values: dict) -> dict:
    by_name = fields.resolve_value_fields(conn, values, target="spu")
    blocked = [name for name in values if by_name[name]["formula_json"]]
    if blocked:
        blocked_text = "、".join(blocked)
        raise ApiError(
            "FIELD_READONLY",
            f"计算属性不能直接赋值：{blocked_text}",
            hint="计算属性的值由公式自动算出；请填写公式引用的源属性", status=409,
            user_message=f"计算属性不能直接填写：{blocked_text}",
            user_hint="计算属性的值由公式自动算出；请填写公式引用的源属性")
    return by_name


def _load_spu(conn, spu_id: int, *, include_total: bool = False) -> dict | None:
    row = conn.execute(
        "SELECT id, name, created_at, updated_at FROM spus WHERE id = ?",
        (spu_id,)).fetchone()
    if row is None:
        return None
    sku_rows = conn.execute(
        """SELECT i.id, i.seq, i.name,
                  COALESCE((SELECT SUM(m.qty) FROM movements m WHERE m.item_id = i.id), 0) AS qty
           FROM items i WHERE i.spu_id = ? ORDER BY i.seq""",
        (spu_id,)).fetchall()
    skus = [
        {"id": sku["id"], "qty": sku["qty"],
         "ref": format_item_ref(spu_id, sku["seq"])}
        for sku in sku_rows
    ]
    total_qty = sum(sku["qty"] for sku in skus)
    context = {"id": spu_id, "total_qty": total_qty, "sku_count": len(skus), "skus": skus}
    values, calculations = fields.load_spu_values(conn, [context])
    out = {
        **dict(row),
        "sku_count": len(skus),
        "values": values[spu_id],
        "calculations": calculations[spu_id],
    }
    if include_total:
        out["total_qty"] = total_qty
    return out


def _load_spu_ref(conn, spu_id: int):
    return conn.execute("SELECT * FROM spus WHERE id = ?", (spu_id,)).fetchone()


def create_spu(shop_id: int, name: str, values: dict | None = None) -> dict:
    name = name.strip()
    if not name:
        raise ApiError("SPU_NAME_INVALID", "SPU 名称不能为空白", status=400)
    values = values or {}
    _validate_create_values(values)
    d = db.shop_db(shop_id)
    ts = now_iso()
    with d.lock, d.conn:
        _ensure_name_free(d.conn, name)
        by_name = _resolve_writable_values(d.conn, values)
        cur = d.conn.execute(
            "INSERT INTO spus (sync_uid, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (db.new_uid(), name, ts, ts))
        spu_id = cur.lastrowid
        applied = []
        for field_name, value in values.items():
            detail = fields.set_value(d.conn, spu_id, by_name[field_name], value)
            applied.append(detail)
        db.write_audit(d.conn, "spu_create", "spu", spu_id,
                       {"name": name, "values": applied})
        spu = _load_spu(d.conn, spu_id)
    return spu


def list_spus(shop_id: int) -> dict:
    """一次批量读取 SPU、所属 SKU 库存和两类属性定义，再统一求值。"""
    d = db.shop_db(shop_id)
    with d.lock:
        rows = d.conn.execute(
            """SELECT s.id, s.name, s.created_at, s.updated_at,
                      i.id AS item_id, i.seq AS item_seq,
                      COALESCE(q.total, 0) AS item_qty
               FROM spus s
               LEFT JOIN items i ON i.spu_id = s.id
               LEFT JOIN (SELECT item_id, SUM(qty) AS total FROM movements GROUP BY item_id) q
                      ON q.item_id = i.id
               ORDER BY s.id, i.seq""").fetchall()
        spu_defs = fields.load_definitions(d.conn, "spu")
        sku_defs = fields.load_definitions(d.conn, "sku")
        summaries = {}
        for row in rows:
            spu_id = row["id"]
            summary = summaries.setdefault(spu_id, {
                "id": spu_id, "name": row["name"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "skus": [],
            })
            if row["item_id"] is not None:
                summary["skus"].append({
                    "id": row["item_id"], "qty": row["item_qty"],
                    "ref": format_item_ref(spu_id, row["item_seq"]),
                })
        contexts = [
            {
                "id": spu_id,
                "total_qty": sum(sku["qty"] for sku in summary["skus"]),
                "sku_count": len(summary["skus"]),
                "skus": summary["skus"],
            }
            for spu_id, summary in summaries.items()
        ]
        values, calculations = fields.load_spu_values(
            d.conn, contexts, defs=spu_defs, sku_defs=sku_defs)

    out = []
    for context in contexts:
        summary = summaries[context["id"]]
        out.append({
            "id": summary["id"], "name": summary["name"],
            "sku_count": context["sku_count"], "total_qty": context["total_qty"],
            "values": values[context["id"]],
            "calculations": calculations[context["id"]],
            "created_at": summary["created_at"], "updated_at": summary["updated_at"],
        })
    return {"spus": out}


def get_spu(shop_id: int, spu_id: int) -> dict:
    d = db.shop_db(shop_id)
    with d.lock:
        spu = _load_spu(d.conn, spu_id)
    if spu is None:
        raise _not_found(spu_id)
    return spu


def update_spu(shop_id: int, spu_id: int, payload) -> dict:
    body = payload.model_dump(exclude_unset=True)
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = _load_spu_ref(d.conn, spu_id)
        if row is None:
            raise _not_found(spu_id)
        changed = False

        new_name = body.get("name")
        if new_name is not None:
            new_name = new_name.strip()
            if not new_name:
                raise ApiError("SPU_NAME_INVALID", "SPU 名称不能为空白", status=400)
            if new_name != row["name"]:
                _ensure_name_free(d.conn, new_name, exclude_id=spu_id)
                d.conn.execute(
                    "UPDATE spus SET name = ?, updated_at = ? WHERE id = ?",
                    (new_name, now_iso(), spu_id))
                db.write_audit(d.conn, "spu_rename", "spu", spu_id,
                               {"old": row["name"], "new": new_name})
                changed = True

        values = body.get("values")
        if values:
            by_name = _resolve_writable_values(d.conn, values)
            for field_name, value in values.items():
                detail = fields.set_value(d.conn, spu_id, by_name[field_name], value)
                action = "field_clear" if detail.get("action") == "clear" else "field_value_change"
                db.write_audit(d.conn, action, "spu", spu_id, detail)
                changed = True

        if changed:
            d.conn.execute("UPDATE spus SET updated_at = ? WHERE id = ?",
                           (now_iso(), spu_id))
        spu = _load_spu(d.conn, spu_id)
    return spu


def rename_spu(shop_id: int, spu_id: int, name: str) -> dict:
    """保留旧服务入口；实际更新与属性修改共用同一事务路径。"""
    return update_spu(shop_id, spu_id, _RenamePayload(name))


class _RenamePayload:
    def __init__(self, name: str):
        self.name = name

    def model_dump(self, **kwargs):
        return {"name": self.name}


def resolve_spu(conn, spu_id: int | None, spu_name: str | None) -> tuple[int, bool]:
    """解析单品应挂载的 SPU：spu_id 优先；spu_name 精确匹配则复用，无则自动新建。"""
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
    """删除其下已无任何 SKU 的 SPU，并清理该 SPU 的普通属性值。"""
    if not confirm:
        raise ApiError("CONFIRM_REQUIRED", "删除 SPU 需要显式确认",
                       hint="删除后不可恢复；确认请附带 ?confirm=true", status=400,
                       user_message="删除 SPU 需要再次确认",
                       user_hint="删除后不可恢复，请在确认框中删除")
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = _load_spu_ref(d.conn, spu_id)
        if row is None:
            raise _not_found(spu_id)
        sku_count = d.conn.execute(
            "SELECT COUNT(*) FROM items WHERE spu_id = ?", (spu_id,)).fetchone()[0]
        if sku_count:
            raise ApiError("SPU_HAS_ITEMS",
                           f"SPU「{row['name']}」下还有 {sku_count} 个单品，不能删除",
                           hint="请先删除该 SPU 下的全部单品（仅从未产生过流水的单品可删除）",
                           status=409,
                           user_message=f"SPU「{row['name']}」下还有 {sku_count} 个单品，不能删除",
                           user_hint="请先删除该 SPU 下的全部单品（仅从未产生过流水的单品可删除）")
        values_deleted = d.conn.execute(
            "SELECT COUNT(*) FROM spu_custom_values WHERE spu_id = ?", (spu_id,)).fetchone()[0]
        d.conn.execute("DELETE FROM spu_custom_values WHERE spu_id = ?", (spu_id,))
        db.write_audit(d.conn, "spu_delete", "spu", spu_id,
                       {"name": row["name"], "deleted_values": values_deleted})
        d.conn.execute("DELETE FROM spus WHERE id = ?", (spu_id,))
    return {"deleted": True, "spu_id": spu_id, "name": row["name"],
            "deleted_values": values_deleted}


def _not_found(spu_id: int) -> ApiError:
    return ApiError("SPU_NOT_FOUND", f"SPU {spu_id} 不存在",
                    hint="GET /api/v1/spus 查看现有 SPU", status=404,
                    user_message=f"SPU {spu_id} 不存在",
                    user_hint="该 SPU 可能已被删除，请刷新页面后重试")
