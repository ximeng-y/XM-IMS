"""自定义字段的领域定义：系统字段、字段定义读取、值写入与批量还原

本模块只做「字段行 ↔ 领域值」的转换与读写，不开库、不提交事务、不写审计：
连接由调用方传入，事务与审计由上层服务统一编排。

数学计算字段：值不落库，由公式按定义实时算出，读接口统一经 load_values 出口
给出（values 放显示数值，calculations 放格式化文本与单元格错误）；其值不可
手填或清除，set_value 一律拒绝。
"""
from . import fieldtypes, formulas
from ..errors import ApiError

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


def definition_from_row(row) -> dict:
    """字段行 → 领域定义：uid 稳定标识 + 公式 token 列表

    普通字段用 type/scale 解释编码；计算字段的 formula 决定取值方式。
    """
    return {"id": row["id"], "uid": row["sync_uid"], "name": row["name"], "type": row["type"],
            "scale": row["scale"], "allow_negative": bool(row["allow_negative"]),
            "formula": formulas.tokens_of(row["formula_json"])}


def load_definition(conn, field_id: int) -> dict:
    row = conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
    return definition_from_row(row)


def load_definitions(conn) -> list:
    """本店全部字段定义（按 id 顺序），供公式校验与求值使用"""
    return [definition_from_row(row)
            for row in conn.execute("SELECT * FROM custom_fields ORDER BY id").fetchall()]


def resolve_value_fields(conn, values: dict) -> dict:
    """校验 values 的字段名均为已有自定义字段，返回 名称→字段行"""
    rows = conn.execute("SELECT * FROM custom_fields").fetchall()
    by_name = {r["name"]: r for r in rows}
    unknown = [k for k in values if k not in by_name]
    if unknown:
        valid = "、".join(sorted(by_name)) or "（该店铺还没有自定义字段，先 POST /api/v1/fields 创建）"
        unknown_text = "、".join(unknown)
        raise ApiError("UNKNOWN_FIELD", f"未知字段：{unknown_text}",
                       hint=f"有效自定义字段：{valid}；名称/SPU 等系统属性通过请求体顶层字段修改",
                       status=400,
                       user_message=f"未知字段：{unknown_text}",
                       user_hint=("有效自定义字段：" + "、".join(sorted(by_name)))
                                 if by_name else "该店铺还没有自定义字段，请先到「字段」页创建")
    return by_name


def set_value(conn, item_id: int, field_row, value) -> dict:
    """写入/清除单个字段值。value 为 None 表示清除。返回审计 detail

    计算字段的值由公式算出，不落 custom_values，因此任何直接写入（含 null 清除）
    都在这里被拒绝，包括绕过前端的 API 调用。
    """
    if field_row["formula_json"]:
        raise ApiError("FIELD_READONLY",
                       f"字段「{field_row['name']}」是计算字段，值由公式自动算出，不能直接写入或清除",
                       hint="请修改公式引用的源字段值；如需调整该字段本身，"
                            "请使用字段页的「编辑公式」", status=409,
                       user_message=f"字段「{field_row['name']}」是计算字段，"
                                    "值由公式自动算出，不能直接填写或清除",
                       user_hint="请修改公式引用的源字段值；如需调整该字段本身，"
                                 "请使用「字段」页的「编辑公式」")
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
                       hint=f"该字段类型为 {type_desc}", status=400,
                       user_message=f"字段「{field_row['name']}」的值无效：{e}",
                       user_hint=f"该字段类型为 "
                                 f"{fieldtypes.TYPE_LABELS.get(field_row['type'], field_row['type'])}"
                                 + (f"（{field_row['scale']} 位小数）"
                                    if field_row["type"] == "decimal" else ""))
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


def load_values(conn, contexts: list, defs: list | None = None, engine=None):
    """批量取自定义字段值，返回 (item_id → 显示值字典, item_id → calculations)

    contexts 为内部行上下文 [{"id", "qty", "spu_id"}]（内部整数 id，不是对外编号）。
    普通字段沿用原有编码还原；计算字段按公式求值，中间结果保留内部精度，整数字段
    用取整后的结果供下游引用。
    """
    ids = [c["id"] for c in contexts]
    values = {iid: {} for iid in ids}
    calculations = {iid: {} for iid in ids}
    if not ids:
        return values, calculations
    defs = load_definitions(conn) if defs is None else defs
    if engine is None:
        engine = formulas.Engine(defs)
    has_computed = bool(engine.computed)
    # 读取集合是「全部普通字段」，不是「可参与公式的数值字段」：文本与开关字段
    # 照常还原（Engine.plain 只数值型，仅用于公式求值的取值来源）
    wanted = {f["id"]: f for f in defs if not f["formula"]}
    marks = ",".join("?" * len(ids))
    stored = {iid: {} for iid in ids}
    if wanted:
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
    # 用原始编码还原数学值（百分比 25% → 0.25），避免经 float 往返；
    # 商品自身的 spu_id 不进公式上下文：SPU ID 不参与数学计算
    for c in contexts:
        computed, cells = engine.compute(
            {"qty": c["qty"], "stored": stored[c["id"]]})
        values[c["id"]].update(computed)
        calculations[c["id"]] = cells
    return values, calculations
