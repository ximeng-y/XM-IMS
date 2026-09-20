"""自定义字段管理：创建/改名/删除/结构转换

系统字段（每店默认存在、不可删除）在代码中固定呈现，不入库。
结构转换规则：
- string/bool 与其他类型不可互转 → 需 confirm_clear=true，确认后清空全部值
- 数值类互转保持逻辑值不变（存储值 ×10^(k2-k1)），除法余数 ≠ 0 即有损，
  有损必须指定 rounding（ceil/floor/round=四舍五入）
- 关闭 allow_negative 前若存在负值则阻断
"""
from .. import db, fieldtypes
from ..config import now_iso
from ..errors import ApiError
from . import items

# 系统字段（API 统一呈现；readonly 表示不可通过「字段值编辑」路径修改）
SYSTEM_FIELDS = [
    {"system": True, "key": "id", "name": "ID", "type": "int", "readonly": True,
     "allow_negative": False, "scale": 0},
    {"system": True, "key": "name", "name": "名称", "type": "string", "readonly": False,
     "allow_negative": False, "scale": 0},
    {"system": True, "key": "qty", "name": "库存数量", "type": "int", "readonly": True,
     "allow_negative": False, "scale": 0},
    {"system": True, "key": "spu_id", "name": "SPU ID", "type": "int", "readonly": True,
     "allow_negative": False, "scale": 0},
    {"system": True, "key": "spu_name", "name": "SPU 名称", "type": "string", "readonly": True,
     "allow_negative": False, "scale": 0},
]
RESERVED_NAMES = {f["name"] for f in SYSTEM_FIELDS}


def _field_out(row) -> dict:
    return {"system": False, "id": row["id"], "name": row["name"], "type": row["type"],
            "readonly": False, "allow_negative": bool(row["allow_negative"]),
            "scale": row["scale"]}


def _target_def(field_id: int, name: str, t: str, s: int, neg: bool) -> dict:
    return {"system": False, "id": field_id, "name": name, "type": t, "readonly": False,
            "allow_negative": neg, "scale": s}


def _def_str(t: str, s: int, neg: bool) -> str:
    out = f"{t}" + (f"({s}位小数)" if t == "decimal" else "")
    return out + ("，允许负数" if neg else "")


def _ensure_name_free(conn, name: str) -> None:
    if name in RESERVED_NAMES:
        raise ApiError("FIELD_NAME_EXISTS", f"字段名「{name}」与系统字段重名",
                       hint=f"系统字段（保留名）：{'、'.join(sorted(RESERVED_NAMES))}", status=409)
    if conn.execute("SELECT id FROM custom_fields WHERE name = ?", (name,)).fetchone():
        raise ApiError("FIELD_NAME_EXISTS", f"字段「{name}」已存在", status=409)


def list_fields(shop_id: int) -> dict:
    d = db.shop_db(shop_id)
    with d.lock:
        rows = d.conn.execute("SELECT * FROM custom_fields ORDER BY id").fetchall()
    return {"fields": [dict(f) for f in SYSTEM_FIELDS] + [_field_out(r) for r in rows]}


def create_field(shop_id: int, name: str, type_: str,
                 allow_negative: bool, scale: int | None) -> dict:
    name = name.strip()
    if not name:
        raise ApiError("FIELD_NAME_INVALID", "字段名不能为空白", status=400)
    if type_ not in fieldtypes.TYPES:
        raise ApiError("FIELD_TYPE_INVALID", f"无效的字段类型：{type_}",
                       hint=f"可选类型：{'、'.join(fieldtypes.TYPES)}", status=400)
    if type_ not in fieldtypes.NUMERIC_TYPES and allow_negative:
        raise ApiError("NEGATIVE_NOT_APPLICABLE",
                       "仅数值类字段（int/decimal/percent）支持 allow_negative", status=400)
    if scale is not None:
        if type_ != "decimal":
            raise ApiError("SCALE_NOT_APPLICABLE", "scale 仅 decimal 类型支持", status=400)
    else:
        scale = 2 if type_ == "decimal" else 0

    d = db.shop_db(shop_id)
    ts = now_iso()
    with d.lock, d.conn:
        _ensure_name_free(d.conn, name)
        cur = d.conn.execute(
            "INSERT INTO custom_fields (name, type, allow_negative, scale, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, type_, int(allow_negative), scale, ts, ts))
        field_id = cur.lastrowid
        db.write_audit(d.conn, "field_create", "field", field_id,
                       {"name": name, "type": type_, "allow_negative": allow_negative,
                        "scale": scale})
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
    return _field_out(row)


def rename_field(shop_id: int, field_id: int, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ApiError("FIELD_NAME_INVALID", "字段名不能为空白", status=400)
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
        if row is None:
            raise ApiError("FIELD_NOT_FOUND", f"字段 {field_id} 不存在",
                           hint="GET /api/v1/fields 查看字段列表（系统字段不可修改）", status=404)
        if row["name"] != name:
            _ensure_name_free(d.conn, name)
            d.conn.execute("UPDATE custom_fields SET name = ?, updated_at = ? WHERE id = ?",
                           (name, now_iso(), field_id))
            db.write_audit(d.conn, "field_rename", "field", field_id,
                           {"old": row["name"], "new": name})
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
    return _field_out(row)


def delete_field(shop_id: int, field_id: int, confirm: bool) -> dict:
    if not confirm:
        raise ApiError("CONFIRM_REQUIRED", "删除字段需要显式确认",
                       hint="删除将同时清空所有单品的该字段值且不可恢复；确认请附带 ?confirm=true",
                       status=400)
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
        if row is None:
            raise ApiError("FIELD_NOT_FOUND", f"字段 {field_id} 不存在",
                           hint="GET /api/v1/fields 查看字段列表（系统字段不可删除）", status=404)
        deleted_values = d.conn.execute(
            "SELECT COUNT(*) FROM custom_values WHERE field_id = ?", (field_id,)).fetchone()[0]
        # custom_values 经外键 ON DELETE CASCADE 一并删除
        d.conn.execute("DELETE FROM custom_fields WHERE id = ?", (field_id,))
        db.write_audit(d.conn, "field_delete", "field", field_id,
                       {"name": row["name"], "type": row["type"], "deleted_values": deleted_values})
    return {"deleted": True, "field": _field_out(row), "deleted_values": deleted_values}


def convert_field(shop_id: int, field_id: int, new_type: str | None, new_scale: int | None,
                  new_allow_negative: bool | None, rounding: str | None,
                  confirm_clear: bool, dry_run: bool) -> dict:
    if rounding is not None and rounding not in fieldtypes.ROUNDINGS:
        raise ApiError("ROUNDING_INVALID", f"无效的取整方式：{rounding}",
                       hint="可选：ceil（向上）/ floor（向下）/ round（四舍五入）", status=400)
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
        if row is None:
            raise ApiError("FIELD_NOT_FOUND", f"字段 {field_id} 不存在",
                           hint="GET /api/v1/fields 查看字段列表", status=404)
        t1, s1, neg1 = row["type"], row["scale"], bool(row["allow_negative"])

        t2 = new_type or t1
        if t2 not in fieldtypes.TYPES:
            raise ApiError("FIELD_TYPE_INVALID", f"无效的目标类型：{t2}",
                           hint=f"可选类型：{'、'.join(fieldtypes.TYPES)}", status=400)
        if new_scale is not None:
            if t2 != "decimal":
                raise ApiError("SCALE_NOT_APPLICABLE", "new_scale 仅目标类型为 decimal 时有效",
                               status=400)
            s2 = new_scale
        else:
            # 未显式给 scale：decimal→decimal 沿用旧精度；其他类型转 decimal 默认 2 位
            if t1 == "decimal" and t2 == "decimal":
                s2 = s1
            else:
                s2 = 2 if t2 == "decimal" else 0
        neg2 = new_allow_negative if new_allow_negative is not None else neg1

        if (t1, s1, neg1) == (t2, s2, neg2):
            raise ApiError("FIELD_NO_CHANGE", "转换目标与当前结构相同",
                           hint="无需转换；如需改名请使用 PATCH /api/v1/fields/{id}", status=400)

        # 取全部现存值（含单品标识，供报告与审计）；对外一律用「SPU 号-组内序号」
        value_rows = d.conn.execute(
            "SELECT v.item_id, v.value_int, i.name AS item_name, i.spu_id, i.seq "
            "FROM custom_values v JOIN items i ON i.id = v.item_id "
            "WHERE v.field_id = ? ORDER BY i.spu_id, i.seq", (field_id,)).fetchall()

        def sample(row, old, new) -> dict:
            return {"item_id": items.format_item_ref(row["spu_id"], row["seq"]),
                    "item_name": row["item_name"], "old": old, "new": new}

        # 关闭 allow_negative 前置检查：存在负值则阻断（取整不改变符号）
        if not neg2:
            neg_rows = [r for r in value_rows if (r["value_int"] or 0) < 0]
            if neg_rows:
                sample = "、".join(r["item_name"] for r in neg_rows[:3])
                raise ApiError("NEGATIVE_VALUES_EXIST",
                               f"字段「{row['name']}」存在 {len(neg_rows)} 个负值，无法关闭允许负数",
                               hint=f"负值所在单品：{sample}；请先修正这些值再转换", status=409)

        kind = fieldtypes.conversion_kind(t1, s1, t2, s2)

        if kind == "no_change":
            # 仅 allow_negative 放宽，无值换算
            _update_def(d.conn, field_id, t2, s2, neg2)
            db.write_audit(d.conn, "field_convert", "field", field_id,
                           {"name": row["name"], "only": "allow_negative",
                            "from": _def_str(t1, s1, neg1), "to": _def_str(t2, s2, neg2)})
            return {"field": _target_def(field_id, row["name"], t2, s2, neg2),
                    "converted": False, "note": "仅更新了 allow_negative，字段值不变"}

        if kind == "clear":
            # string/bool 与其他类型互转：确认后清空全部值
            samples = [sample(r, fieldtypes.decode(t1, s1, r["value_int"]), None)
                       for r in value_rows[:5]]
            if not confirm_clear:
                raise ApiError("CONVERT_REQUIRES_CLEAR",
                               f"「{t1}」与「{t2}」不可互转，转换将清空字段「{row['name']}」"
                               f"的全部 {len(value_rows)} 个值",
                               hint="确认清空并转换请附带 confirm_clear=true", status=409)
            if dry_run:
                return {"dry_run": True, "requires_clear": True,
                        "affected_items": len(value_rows), "samples": samples,
                        "field": _target_def(field_id, row["name"], t2, s2, neg2)}
            d.conn.execute("DELETE FROM custom_values WHERE field_id = ?", (field_id,))
            _update_def(d.conn, field_id, t2, s2, neg2)
            db.write_audit(d.conn, "field_convert", "field", field_id,
                           {"name": row["name"], "from": _def_str(t1, s1, neg1),
                            "to": _def_str(t2, s2, neg2), "cleared_values": len(value_rows)})
            return {"field": _target_def(field_id, row["name"], t2, s2, neg2),
                    "cleared": True, "affected_items": len(value_rows), "samples": samples}

        # 数值类转换：逻辑值不变，存储值 ×10^(k2-k1)
        k1, k2 = kind[1], kind[2]
        converted = []
        lossy = False
        for r in value_rows:
            n = r["value_int"] or 0
            new_n, ok = fieldtypes.convert_encoded(n, k1, k2, rounding)
            lossy = lossy or not ok
            converted.append((r, n, new_n))

        if lossy and rounding is None:
            if dry_run:
                samples = [sample(r, fieldtypes.decode(t1, s1, n), None)
                           for r, n, _ in converted[:5]]
                return {"dry_run": True, "lossy": True, "rounding_required": True,
                        "description": _describe(k1, k2),
                        "affected_items": len(converted), "samples": samples,
                        "field": _target_def(field_id, row["name"], t2, s2, neg2)}
            raise ApiError("ROUNDING_REQUIRED", "本次转换存在精度损失，必须指定取整方式",
                           hint="rounding 可选：ceil（向上）/ floor（向下）/ round（四舍五入）",
                           status=400)

        samples = [sample(r, fieldtypes.decode(t1, s1, n),
                          None if new_n is None else fieldtypes.decode(t2, s2, new_n))
                   for r, n, new_n in converted[:5]]
        result = {"description": _describe(k1, k2), "lossy": lossy,
                  "affected_items": len(converted), "samples": samples,
                  "field": _target_def(field_id, row["name"], t2, s2, neg2)}
        if rounding is not None:
            result["rounding"] = rounding
        if dry_run:
            return {"dry_run": True, **result}
        for r, n, new_n in converted:
            d.conn.execute("UPDATE custom_values SET value_int = ? "
                           "WHERE item_id = ? AND field_id = ?", (new_n, r["item_id"], field_id))
        _update_def(d.conn, field_id, t2, s2, neg2)
        db.write_audit(d.conn, "field_convert", "field", field_id,
                       {"name": row["name"], "from": _def_str(t1, s1, neg1),
                        "to": _def_str(t2, s2, neg2),
                        "rounding": rounding if lossy else None,
                        "lossy": lossy, "affected_items": len(converted)})
        return {"converted": True, **result}


def _update_def(conn, field_id: int, t: str, s: int, neg: bool) -> None:
    conn.execute("UPDATE custom_fields SET type = ?, scale = ?, allow_negative = ?, "
                 "updated_at = ? WHERE id = ?",
                 (t, s, int(neg), now_iso(), field_id))


def _describe(k1: int, k2: int) -> str:
    """生成人类可读的换算说明（存储值口径）"""
    if k2 >= k1:
        return f"存储值 ×{10 ** (k2 - k1)}（逻辑值不变）"
    return f"存储值 ÷{10 ** (k1 - k2)}（逻辑值不变）"
