"""自定义字段管理：创建/改名/删除/结构转换/计算公式更新

系统字段（每店默认存在、不可删除）在代码中固定呈现，不入库。
结构转换规则：
- string/bool 与其他类型不可互转 → 需 confirm_clear=true，确认后清空全部值
- 数值类互转保持逻辑值不变（存储值 ×10^(k2-k1)），除法余数 ≠ 0 即有损，
  有损必须指定 rounding（ceil/floor/round=四舍五入）
- 关闭 allow_negative 前若存在负值则阻断

数学计算字段：
- 由 formula_json 承载公式定义，type 为结果类型（int/decimal/percent），scale 为显示位数
- 结果类型为整数时结果按四舍五入取整，显示位数固定为 0
- 值由后端按公式实时算出，不落 custom_values，不可手填/清除，不可与普通字段互转
- 被公式引用的字段不能删除，也不能转成非数值类型（否则公式失去可计算性）
"""
from .. import db, fieldtypes, formulas
from ..config import now_iso
from ..errors import ApiError
from . import items

# 系统字段（API 统一呈现；readonly 表示不可通过「字段值编辑」路径修改）
# SKU 的对外标识是「SPU 号-组内序号」（如 1-2），故 ID 字段按文本呈现
SYSTEM_FIELDS = [
    {"system": True, "key": "id", "name": "ID", "type": "string", "readonly": True,
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
# 计算字段允许的结果类型：整数（结果四舍五入取整）／小数／百分比
COMPUTED_TYPES = formulas.RESULT_TYPES


def _computed_scale(t: str, scale: int | None, fallback: int) -> int:
    """计算字段的显示位数：整数只能是 0（省略即 0）；小数与百分比省略则沿用现值（默认 2）"""
    if t == "int":
        if scale not in (None, 0):
            raise ApiError("SCALE_NOT_APPLICABLE",
                           f"结果类型为整数时不能设置小数位（当前收到 {scale}）",
                           hint="整数计算字段的显示位数固定为 0；如需保留小数请把结果类型改为小数或百分比",
                           status=400,
                           user_message="整数结果不能设置小数位",
                           user_hint="整数结果按四舍五入取整，没有小数位；"
                                     "如需保留小数，请把「计算结果类型」改为小数或百分比")
        return 0
    return fallback if scale is None else scale


def _type_hint_error(t: str) -> ApiError:
    return ApiError("FORMULA_TYPE_INVALID",
                    f"计算字段的结果类型只能是整数、小数或百分比（当前收到 {t}）",
                    hint="可选结果类型：int（整数）、decimal（小数）、percent（百分比）", status=400,
                    user_message="计算字段的结果类型只能是整数、小数或百分比",
                    user_hint="请在「计算结果类型」中选择「整数」「小数」或「百分比」")


def _formula_out(tokens) -> dict | None:
    return None if not tokens else {"version": formulas.VERSION, "tokens": tokens}


def _out(defn: dict, dry_run: bool = False, field_id=None, uid=None) -> dict:
    """字段定义 → API 呈现（普通字段与计算字段共用）"""
    computed = bool(defn.get("formula"))
    out = {"system": False, "id": defn.get("id", field_id), "uid": defn.get("uid", uid),
           "name": defn["name"], "type": defn["type"], "readonly": computed,
           "allow_negative": bool(defn["allow_negative"]), "scale": defn["scale"],
           "formula": _formula_out(defn.get("formula")), "computed": computed,
           "dry_run": dry_run}
    return out


def _field_out(row) -> dict:
    return _out(items.field_def(None, row=row))


def _target_def(field_id: int, name: str, t: str, s: int, neg: bool) -> dict:
    return {"system": False, "id": field_id, "uid": None, "name": name, "type": t,
            "readonly": False, "allow_negative": neg, "scale": s, "formula": None,
            "computed": False}


def _def_str(t: str, s: int, neg: bool) -> str:
    out = f"{t}" + (f"({s}位小数)" if t == "decimal" else "")
    return out + ("，允许负数" if neg else "")


def _ensure_name_free(conn, name: str) -> None:
    if name in RESERVED_NAMES:
        raise ApiError("FIELD_NAME_EXISTS", f"字段名「{name}」与系统字段重名",
                       hint=f"系统字段（保留名）：{'、'.join(sorted(RESERVED_NAMES))}", status=409,
                       user_message=f"字段名「{name}」与系统字段重名",
                       user_hint=f"系统字段（保留名）：{'、'.join(sorted(RESERVED_NAMES))}")
    if conn.execute("SELECT id FROM custom_fields WHERE name = ?", (name,)).fetchone():
        raise ApiError("FIELD_NAME_EXISTS", f"字段「{name}」已存在", status=409,
                       user_message=f"字段「{name}」已存在",
                       user_hint="请换一个字段名称")


def _not_found(field_id: int) -> ApiError:
    return ApiError("FIELD_NOT_FOUND", f"字段 {field_id} 不存在",
                    hint="GET /api/v1/fields 查看字段列表", status=404,
                    user_message=f"字段 {field_id} 不存在",
                    user_hint="该字段可能已被删除，请刷新页面后重试")


def _tokens(formula_in) -> list:
    """请求里的公式对象 → 规范化 token 列表（语法校验在 parse 中完成）"""
    result = formulas.parse([t.model_dump() for t in formula_in.tokens])
    return result.tokens


def _referenced_defs(conn, uid: str) -> list:
    """返回引用了该字段的计算字段名（删除与类型转换的依赖保护）"""
    defs = items.field_defs(conn)
    parsed = {d["uid"]: formulas.parse(d["formula"]) for d in defs if d["formula"]}
    names = {d["uid"]: d["name"] for d in defs}
    return sorted(names[other] for other in formulas.dependents(parsed, uid))


def _guard_referenced(conn, row) -> None:
    users = _referenced_defs(conn, row["sync_uid"])
    if users:
        raise ApiError("FIELD_REFERENCED",
                       f"字段「{row['name']}」被计算字段引用：{'、'.join(users)}",
                       hint="请先修改这些计算字段的公式，去掉对该字段的引用", status=409,
                       user_message=f"字段「{row['name']}」被计算字段引用：{'、'.join(users)}",
                       user_hint="请先修改这些计算字段的公式，去掉对该字段的引用")


def list_fields(shop_id: int) -> dict:
    d = db.shop_db(shop_id)
    with d.lock:
        rows = d.conn.execute("SELECT * FROM custom_fields ORDER BY id").fetchall()
    return {"fields": [dict(f) for f in SYSTEM_FIELDS] + [_field_out(r) for r in rows]}


def create_field(shop_id: int, body) -> dict:
    """创建普通字段或数学计算字段（带 dry_run 时只校验，不落库）"""
    name = body.name.strip()
    if not name:
        raise ApiError("FIELD_NAME_INVALID", "字段名不能为空白", status=400)
    if body.formula is not None:
        return _create_computed(shop_id, name, body)

    type_ = body.type
    if type_ not in fieldtypes.TYPES:
        raise ApiError("FIELD_TYPE_INVALID", f"无效的字段类型：{type_}",
                       hint=f"可选类型：{'、'.join(fieldtypes.TYPES)}", status=400,
                       user_message=f"无效的字段类型：{type_}",
                       user_hint="可选类型：" + "、".join(fieldtypes.TYPE_LABELS[t]
                                                          for t in fieldtypes.TYPES))
    if type_ not in fieldtypes.NUMERIC_TYPES and body.allow_negative:
        raise ApiError("NEGATIVE_NOT_APPLICABLE",
                       "仅数值类字段（int/decimal/percent）支持 allow_negative", status=400,
                       user_message="只有数值类字段才允许负值",
                       user_hint="整数、小数、百分比字段可以勾选「允许负数」，"
                                 "文本与开关字段不能")
    if body.scale is not None:
        if type_ != "decimal":
            raise ApiError("SCALE_NOT_APPLICABLE", "scale 仅 decimal 类型支持", status=400,
                           user_message="只有小数类型才能设置小数位",
                           user_hint="如需设置小数位，请先把类型选为「小数」")
        scale = body.scale
    else:
        scale = 2 if type_ == "decimal" else 0

    d = db.shop_db(shop_id)
    ts = now_iso()
    with d.lock, d.conn:
        _ensure_name_free(d.conn, name)
        cur = d.conn.execute(
            "INSERT INTO custom_fields (sync_uid, name, type, allow_negative, scale, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (db.new_uid(), name, type_, int(body.allow_negative), scale, ts, ts))
        field_id = cur.lastrowid
        db.write_audit(d.conn, "field_create", "field", field_id,
                       {"name": name, "type": type_, "allow_negative": body.allow_negative,
                        "scale": scale})
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
    return _field_out(row)


def _create_computed(shop_id: int, name: str, body) -> dict:
    """创建数学计算字段：先做语法、引用与完整依赖图校验，再落库"""
    tokens = _tokens(body.formula)
    t = body.type
    if t not in COMPUTED_TYPES:
        raise _type_hint_error(t)
    scale = _computed_scale(t, body.scale, 2)
    defn = {"id": None, "uid": "", "name": name, "type": t, "scale": scale,
            "allow_negative": bool(body.allow_negative), "formula": tokens}

    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        _ensure_name_free(d.conn, name)
        defs = items.field_defs(d.conn)
        formulas.check(defs, defn)
        if body.dry_run:
            return _out(defn, dry_run=True)
        formula_json = formulas.dump(tokens)
        ts = now_iso()
        cur = d.conn.execute(
            "INSERT INTO custom_fields (sync_uid, name, type, allow_negative, scale, "
            "created_at, updated_at, formula_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (db.new_uid(), name, t, int(defn["allow_negative"]), scale, ts, ts, formula_json))
        field_id = cur.lastrowid
        db.write_audit(d.conn, "field_create", "field", field_id,
                       {"name": name, "type": t, "allow_negative": defn["allow_negative"],
                        "scale": scale, "formula": tokens})
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
    return _field_out(row)


def update_formula(shop_id: int, field_id: int, body) -> dict:
    """整体更新计算字段的公式与结果格式；仅计算字段可用，重复提交相同定义不重复写审计"""
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
        if row is None:
            raise _not_found(field_id)
        if not row["formula_json"]:
            raise ApiError("FORMULA_NOT_COMPUTED",
                           f"字段「{row['name']}」不是计算字段，不能设置公式",
                           hint="普通字段与计算字段不能互转；如需调整普通字段结构，请使用转换端点",
                           status=409,
                           user_message=f"字段「{row['name']}」不是计算字段，不能设置公式",
                           user_hint="普通字段与计算字段不能互转；如需调整普通字段结构，"
                                     "请使用列表里的「转换」")
        tokens = _tokens(body.formula)
        t2 = body.type or row["type"]
        if t2 not in COMPUTED_TYPES:
            raise _type_hint_error(t2)
        # 小数与百分比缺省沿用当前精度（含从整数切换时的 0），整数恒为 0
        s2 = _computed_scale(t2, body.scale, row["scale"])
        neg2 = bool(row["allow_negative"]) if body.allow_negative is None else body.allow_negative
        defn = {"id": field_id, "uid": row["sync_uid"], "name": row["name"], "type": t2,
                "scale": s2, "allow_negative": neg2, "formula": tokens}

        defs = [x for x in items.field_defs(d.conn) if x["uid"] != row["sync_uid"]]
        formulas.check(defs, defn)
        formula_json = formulas.dump(tokens)
        unchanged = (formula_json == row["formula_json"] and t2 == row["type"]
                     and s2 == row["scale"] and int(neg2) == row["allow_negative"])
        if body.dry_run:
            return _out(defn, dry_run=True)
        if unchanged:
            return _out(defn)
        d.conn.execute("UPDATE custom_fields SET type = ?, scale = ?, allow_negative = ?, "
                       "formula_json = ?, updated_at = ? WHERE id = ?",
                       (t2, s2, int(neg2), formula_json, now_iso(), field_id))
        db.write_audit(d.conn, "field_formula", "field", field_id,
                       {"name": row["name"],
                        "from": {"type": row["type"], "scale": row["scale"],
                                 "allow_negative": bool(row["allow_negative"]),
                                 "formula": formulas.tokens_of(row["formula_json"])},
                        "to": {"type": t2, "scale": s2, "allow_negative": neg2, "formula": tokens}})
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
                           hint="GET /api/v1/fields 查看字段列表（系统字段不可修改）", status=404,
                           user_message=f"字段 {field_id} 不存在",
                           user_hint="该字段可能已被删除，请刷新页面后重试")
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
                       status=400,
                       user_message="删除字段需要再次确认",
                       user_hint="删除会同时清空所有单品的该字段值且不可恢复，"
                                 "请在确认框中确认删除")
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
        if row is None:
            raise _not_found(field_id)
        # 公式按稳定标识引用字段，删除被引用的字段会让公式失去可计算性
        _guard_referenced(d.conn, row)
        deleted_values = d.conn.execute(
            "SELECT COUNT(*) FROM custom_values WHERE field_id = ?", (field_id,)).fetchone()[0]
        # custom_values 经外键 ON DELETE CASCADE 一并删除
        d.conn.execute("DELETE FROM custom_fields WHERE id = ?", (field_id,))
        db.write_audit(d.conn, "field_delete", "field", field_id,
                       {"name": row["name"], "type": row["type"], "deleted_values": deleted_values}, entity_uid=row["sync_uid"])
    return {"deleted": True, "field": _field_out(row), "deleted_values": deleted_values}


def convert_field(shop_id: int, field_id: int, new_type: str | None, new_scale: int | None,
                  new_allow_negative: bool | None, rounding: str | None,
                  confirm_clear: bool, dry_run: bool) -> dict:
    if rounding is not None and rounding not in fieldtypes.ROUNDINGS:
        raise ApiError("ROUNDING_INVALID", f"无效的取整方式：{rounding}",
                       hint="可选：ceil（向上）/ floor（向下）/ round（四舍五入）", status=400,
                       user_message=f"无效的取整方式：{rounding}",
                       user_hint="可选：" + "／".join(fieldtypes.ROUNDING_LABELS[r]
                                                     for r in fieldtypes.ROUNDINGS))
    d = db.shop_db(shop_id)
    with d.lock, d.conn:
        row = d.conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
        if row is None:
            raise _not_found(field_id)
        if row["formula_json"]:
            raise ApiError("FORMULA_NOT_COMPUTED",
                           f"字段「{row['name']}」是计算字段，不能做结构转换",
                           hint="计算字段的公式、结果格式与精度请使用"
                                "PUT /api/v1/fields/{id}/formula；普通字段与计算字段不能互转",
                           status=409,
                           user_message=f"字段「{row['name']}」是计算字段，不能做结构转换",
                           user_hint="计算字段的公式、结果类型与显示位数请用「编辑公式」修改；"
                                     "普通字段与计算字段不能互转")
        t1, s1, neg1 = row["type"], row["scale"], bool(row["allow_negative"])

        t2 = new_type or t1
        if t2 not in fieldtypes.TYPES:
            raise ApiError("FIELD_TYPE_INVALID", f"无效的目标类型：{t2}",
                           hint=f"可选类型：{'、'.join(fieldtypes.TYPES)}", status=400,
                           user_message=f"无效的目标类型：{t2}",
                           user_hint="可选类型：" + "、".join(fieldtypes.TYPE_LABELS[t]
                                                              for t in fieldtypes.TYPES))
        if new_scale is not None:
            if t2 != "decimal":
                raise ApiError("SCALE_NOT_APPLICABLE", "new_scale 仅目标类型为 decimal 时有效",
                               status=400,
                               user_message="只有目标类型为小数时才能设置小数位",
                               user_hint="如需设置小数位，请把「目标类型」选为「小数」")
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
                           hint="无需转换，若需要转换，请选择将要转换成的目标数据类型",
                           status=400,
                           user_message="转换目标与当前结构相同",
                           user_hint="如需转换，请选择不同的目标类型、小数位或负数设置")

        # 取全部现存值（含单品标识，供报告与审计）；对外一律用「SPU 号-组内序号」
        value_rows = d.conn.execute(
            "SELECT v.item_id, v.value_int, v.value_text, i.name AS item_name, i.spu_id, i.seq "
            "FROM custom_values v JOIN items i ON i.id = v.item_id "
            "WHERE v.field_id = ? ORDER BY i.spu_id, i.seq", (field_id,)).fetchall()

        def sample(row, old, new) -> dict:
            return {"item_id": items.format_item_ref(row["spu_id"], row["seq"]),
                    "item_name": row["item_name"], "old": old, "new": new}

        def stored_value(row):
            return row["value_int"] if row["value_int"] is not None else row["value_text"]

        # 关闭 allow_negative 前置检查：存在负值则阻断（取整不改变符号）
        if not neg2:
            neg_rows = [r for r in value_rows if (r["value_int"] or 0) < 0]
            if neg_rows:
                neg_items = "、".join(r["item_name"] for r in neg_rows[:3])
                raise ApiError("NEGATIVE_VALUES_EXIST",
                               f"字段「{row['name']}」存在 {len(neg_rows)} 个负值，无法关闭允许负数",
                               hint=f"负值所在单品：{neg_items}；请先修正这些值再转换", status=409,
                               user_message=f"字段「{row['name']}」存在 {len(neg_rows)} 个负值，"
                                            "无法关闭允许负数",
                               user_hint=f"负值所在单品：{neg_items}；请先修正这些值再转换")

        kind = fieldtypes.conversion_kind(t1, s1, t2, s2)

        # 数值字段被公式引用时不能转成非数值类型（confirm_clear 也不能绕过），
        # 否则公式将失去可计算性
        if kind == "clear" and t1 in fieldtypes.NUMERIC_TYPES:
            _guard_referenced(d.conn, row)

        if kind == "no_change":
            # 仅切换 allow_negative，无值换算
            samples = []
            for r in value_rows[:5]:
                old = fieldtypes.decode(t1, s1, stored_value(r))
                samples.append(sample(r, old, old))
            result = {"field": _target_def(field_id, row["name"], t2, s2, neg2),
                      "converted": False, "affected_items": len(value_rows),
                      "samples": samples,
                      "note": "仅更新了允许负数，字段值不变"}
            if dry_run:
                return {"dry_run": True, **result}
            _update_def(d.conn, field_id, t2, s2, neg2)
            db.write_audit(d.conn, "field_convert", "field", field_id,
                           {"name": row["name"], "only": "allow_negative",
                            "from": _def_str(t1, s1, neg1), "to": _def_str(t2, s2, neg2)})
            return result

        if kind == "clear":
            # string/bool 与其他类型互转：确认后清空全部值
            # 预览优先于清空确认：dry_run 只返回报告（含清空规模与样例），不要求 confirm_clear
            samples = [sample(r, fieldtypes.decode(t1, s1, stored_value(r)), None)
                       for r in value_rows[:5]]
            if dry_run:
                return {"dry_run": True, "requires_clear": True,
                        "affected_items": len(value_rows), "samples": samples,
                        "field": _target_def(field_id, row["name"], t2, s2, neg2)}
            if not confirm_clear:
                raise ApiError("CONVERT_REQUIRES_CLEAR",
                               f"「{t1}」与「{t2}」不可互转，转换将清空字段「{row['name']}」"
                               f"的全部 {len(value_rows)} 个值",
                               hint="确认清空并转换请附带 confirm_clear=true", status=409,
                               user_message=f"「{fieldtypes.TYPE_LABELS.get(t1, t1)}」与"
                                            f"「{fieldtypes.TYPE_LABELS.get(t2, t2)}」不可互转，"
                                            f"转换将清空字段「{row['name']}」的全部 "
                                            f"{len(value_rows)} 个值",
                               user_hint="该操作会清空字段的全部值且不可恢复；"
                                         "确认无误请再次点击「执行转换」")
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
                        "description_text": _describe_text(k1, k2),
                        "affected_items": len(converted), "samples": samples,
                        "field": _target_def(field_id, row["name"], t2, s2, neg2)}
            raise ApiError("ROUNDING_REQUIRED", "本次转换存在精度损失，必须指定取整方式",
                           hint="rounding 可选：ceil（向上）/ floor（向下）/ round（四舍五入）",
                           status=400,
                           user_message="本次转换存在精度损失，请先选择取整方式",
                           user_hint="请在「取整方式」中选择向上取整、向下取整或四舍五入")

        samples = [sample(r, fieldtypes.decode(t1, s1, n),
                          None if new_n is None else fieldtypes.decode(t2, s2, new_n))
                   for r, n, new_n in converted[:5]]
        result = {"description": _describe(k1, k2), "description_text": _describe_text(k1, k2),
                  "lossy": lossy,
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
    """生成换算说明（存储值口径，面向 Agent）"""
    if k2 >= k1:
        return f"存储值 ×{10 ** (k2 - k1)}（逻辑值不变）"
    return f"存储值 ÷{10 ** (k1 - k2)}（逻辑值不变）"


def _describe_text(k1: int, k2: int) -> str:
    """生成换算说明（面向界面）：不提存储值口径，只说明小数位变化后数值含义不变"""
    if k2 >= k1:
        return f"小数位由 {k1} 位增加到 {k2} 位，数值含义不变"
    return f"小数位由 {k1} 位减少到 {k2} 位，数值含义不变"
