"""单品（SKU）管理：创建/查询/改名/换挂 SPU/自定义字段值修改

改价 = 修改价格类自定义字段的值（记审计 old/new），与改其他字段值同一路径。

对外标识：SKU 的 id 呈现为「SPU 号-组内序号」（如 1-2），由 items.spu_id 与
items.seq 实时拼出；内部仍以 items.id（整数）作为流水与字段值的外键。因此换挂
SPU 时 SKU 的显示编号会随之变化，历史流水显示的是当前归属，而非当时归属。

数学计算字段：值不落库，由 formulas 按公式实时算出，读接口统一经 _load_values
出口给出（values 放显示数值，calculations 放格式化文本与单元格错误）；
其值不可手填或清除，_set_value 一律拒绝。
"""
import re

from .. import db, fieldtypes, formulas
from ..config import now_iso
from ..errors import ApiError
from ..schemas import ItemCreate, ItemPatch
from . import spus

# 对外标识格式：正整数-正整数
ITEM_REF_RE = re.compile(r"^([1-9]\d*)-([1-9]\d*)$")

# 两侧数字各自的上界（SQLite INTEGER 为 64 位有符号）。超出后绑定参数会抛
# OverflowError，须在解析阶段拦成可读错误，不能让客户端自由文本打到 500
REF_PART_MAX = 2 ** 63 - 1


def format_item_ref(spu_id: int, seq: int) -> str:
    """拼出对外标识「SPU 号-组内序号」"""
    return f"{spu_id}-{seq}"


def resolve_item_ref(conn, ref) -> int:
    """把对外标识「SPU 号-组内序号」解析为内部 items.id

    只接受 "n-m" 形式：纯数字（旧的内部 ID）一律拒绝，避免同一套 API 出现
    两种编号语义；调用方需要给出可修复提示。
    """
    m = ITEM_REF_RE.match(str(ref).strip())
    if not m:
        raise ApiError("ITEM_REF_INVALID", f"无效的单品标识：{ref}",
                       hint="单品标识形如 1-2（SPU 号-组内序号）；"
                            "GET /api/v1/items 查看全部单品及其标识", status=400)
    spu_id, seq = int(m.group(1)), int(m.group(2))
    if spu_id > REF_PART_MAX or seq > REF_PART_MAX:
        raise ApiError("ITEM_REF_INVALID", f"单品标识超出取值范围：{ref}",
                       hint=f"SPU 号与组内序号都不得超过 {REF_PART_MAX}；"
                            "GET /api/v1/items 查看全部单品及其标识", status=400)
    row = conn.execute("SELECT id FROM items WHERE spu_id = ? AND seq = ?",
                       (spu_id, seq)).fetchone()
    if row is None:
        raise ApiError("ITEM_NOT_FOUND", f"单品 {spu_id}-{seq} 不存在",
                       hint=_existing_hint(conn), status=404)
    return row["id"]


def _existing_hint(conn) -> str:
    rows = conn.execute(
        "SELECT spu_id, seq, name FROM items ORDER BY spu_id, seq LIMIT 3").fetchall()
    if not rows:
        return "该店铺还没有单品，POST /api/v1/items 创建"
    return "现有单品：" + "、".join(
        f"{format_item_ref(r['spu_id'], r['seq'])}（{r['name']}）" for r in rows)


def _next_seq(conn, spu_id: int) -> int:
    """该 SPU 内的下一个序号。只取 max+1，不复用空位（流水里的旧编号不应指向新 SKU）"""
    row = conn.execute("SELECT MAX(seq) AS mx FROM items WHERE spu_id = ?",
                       (spu_id,)).fetchone()
    return (row["mx"] or 0) + 1


def field_def(conn, field_id: int | None = None, row=None) -> dict:
    """字段行 → 领域定义：uid 稳定标识 + 公式 token 列表

    普通字段用 type/scale 解释编码；计算字段的 formula 决定取值方式。
    """
    if row is None:
        row = conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
    return {"id": row["id"], "uid": row["sync_uid"], "name": row["name"], "type": row["type"],
            "scale": row["scale"], "allow_negative": bool(row["allow_negative"]),
            "formula": formulas.tokens_of(row["formula_json"])}


def field_defs(conn) -> list:
    """本店全部字段定义（按 id 顺序），供公式校验与求值使用"""
    return [field_def(conn, row=r)
            for r in conn.execute("SELECT * FROM custom_fields ORDER BY id").fetchall()]


def _resolve_fields(conn, values: dict) -> dict:
    """校验 values 的字段名均为已有自定义字段，返回 名称→字段行"""
    rows = conn.execute("SELECT * FROM custom_fields").fetchall()
    by_name = {r["name"]: r for r in rows}
    unknown = [k for k in values if k not in by_name]
    if unknown:
        valid = "、".join(sorted(by_name)) or "（该店铺还没有自定义字段，先 POST /api/v1/fields 创建）"
        raise ApiError("UNKNOWN_FIELD", f"未知字段：{'、'.join(unknown)}",
                       hint=f"有效自定义字段：{valid}；名称/SPU 等系统属性通过请求体顶层字段修改",
                       status=400)
    return by_name


def _set_value(conn, item_id: int, field_row, value) -> dict:
    """写入/清除单个字段值。value 为 None 表示清除。返回审计 detail

    计算字段的值由公式算出，不落 custom_values，因此任何直接写入（含 null 清除）
    都在这里被拒绝，包括绕过前端的 API 调用。
    """
    if field_row["formula_json"]:
        raise ApiError("FIELD_READONLY",
                       f"字段「{field_row['name']}」是计算字段，值由公式自动算出，不能直接写入或清除",
                       hint="请修改公式引用的源字段值；如需调整该字段本身，"
                            "请使用字段页的「编辑公式」", status=409)
    if value is None:
        conn.execute("DELETE FROM custom_values WHERE item_id = ? AND field_id = ?",
                     (item_id, field_row["id"]))
        return {"field": field_row["name"], "action": "clear"}
    try:
        encoded, display = fieldtypes.validate_and_encode(
            field_row["type"], field_row["scale"], bool(field_row["allow_negative"]), value)
    except ValueError as e:
        type_desc = field_row["type"] + (f"（{field_row['scale']} 位小数）"
                                         if field_row["type"] == "decimal" else "")
        raise ApiError("FIELD_VALUE_INVALID", f"字段「{field_row['name']}」的值无效：{e}",
                       hint=f"该字段类型为 {type_desc}", status=400)
    old = conn.execute("SELECT value_int, value_text FROM custom_values "
                       "WHERE item_id = ? AND field_id = ?",
                       (item_id, field_row["id"])).fetchone()
    if field_row["type"] == "string":
        v_int, v_text = None, display
    else:
        v_int, v_text = encoded, None
    conn.execute(
        "INSERT INTO custom_values (item_id, field_id, value_int, value_text) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (item_id, field_id) DO UPDATE "
        "SET value_int = excluded.value_int, value_text = excluded.value_text",
        (item_id, field_row["id"], v_int, v_text))
    old_display = None
    if old is not None:
        old_stored = old["value_int"] if old["value_int"] is not None else old["value_text"]
        old_display = fieldtypes.decode(field_row["type"], field_row["scale"], old_stored)
    return {"field": field_row["name"], "old": old_display, "new": display}


def _load_values(conn, contexts: list, defs: list | None = None, engine=None):
    """批量取自定义字段值，返回 (item_id → 显示值字典, item_id → calculations)

    contexts 为内部行上下文 [{"id", "qty", "spu_id"}]（内部整数 id，不是对外编号）。
    普通字段沿用原有编码还原；计算字段按公式求值，中间结果保留内部精度。
    """
    ids = [c["id"] for c in contexts]
    values = {iid: {} for iid in ids}
    calculations = {iid: {} for iid in ids}
    if not ids:
        return values, calculations
    defs = field_defs(conn) if defs is None else defs
    if engine is None:
        engine = formulas.Engine(defs)
    plain = engine.plain
    by_uid = engine.by_uid
    has_computed = bool(engine.computed)
    marks = ",".join("?" * len(ids))
    stored = {iid: {} for iid in ids}
    if plain:
        wanted = {f["id"]: f for f in plain}
        rows = conn.execute(
            f"SELECT v.item_id, v.field_id, v.value_int, v.value_text "
            f"FROM custom_values v WHERE v.item_id IN ({marks})", ids).fetchall()
        for r in rows:
            field = wanted.get(r["field_id"])
            if field is None:
                continue        # 计算字段不读值行；已删除字段的残留值一并忽略
            raw = r["value_int"] if r["value_int"] is not None else r["value_text"]
            stored[r["item_id"]][field["uid"]] = raw
            values[r["item_id"]][field["name"]] = fieldtypes.decode(
                field["type"], field["scale"], raw)
    if not has_computed:
        return values, calculations
    # 用原始编码还原数学值（百分比 25% → 0.25），避免经 float 往返
    for c in contexts:
        computed, cells = engine.compute(
            {"qty": c["qty"], "spu_id": c["spu_id"], "stored": stored[c["id"]]})
        values[c["id"]].update(computed)
        calculations[c["id"]] = cells
    return values, calculations


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
    values, calculations = _load_values(conn, [context])
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
                       hint="null（清除值）仅用于 PATCH 修改", status=400)
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        spu_id, spu_created = spus.resolve_spu(d.conn, payload.spu_id, payload.spu_name)
        by_name = _resolve_fields(d.conn, payload.values)
        blocked = [k for k in payload.values if by_name[k]["formula_json"]]
        if blocked:
            raise ApiError("FIELD_READONLY",
                           f"计算字段不能直接赋值：{'、'.join(blocked)}",
                           hint="计算字段的值由公式自动算出；请填写公式引用的源字段", status=409)
        ts = now_iso()
        cur = d.conn.execute(
            "INSERT INTO items (sync_uid, spu_id, seq, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_uid(), spu_id, _next_seq(d.conn, spu_id), name, ts, ts))
        item_id = cur.lastrowid
        for fname, value in payload.values.items():
            _set_value(d.conn, item_id, by_name[fname], value)
        db.write_audit(d.conn, "item_create", "item", item_id,
                       {"name": name, "spu_id": spu_id, "spu_auto_created": spu_created})
        item = _load_item(d.conn, item_id)
    item["spu_auto_created"] = spu_created
    return item


def list_items(shop_id: int, offset: int = 0, limit: int = 50,
               q: str | None = None, spu_id: int | None = None) -> dict:
    where, params = [], []
    if q:
        where.append("i.name LIKE ?")
        params.append(f"%{q}%")
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
        values, calculations = _load_values(d.conn, [dict(r) for r in rows])
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
            by_name = _resolve_fields(d.conn, values)
            for fname, v in values.items():
                detail = _set_value(d.conn, item_id, by_name[fname], v)
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
