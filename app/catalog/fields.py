"""自定义字段领域定义：系统字段、定义读取、值写入与批量求值

本模块只做「字段行 ↔ 领域值」的转换与读写，不开库、不提交事务、不写审计。
SKU 与 SPU 使用各自的值表和实体列；默认参数保持 SKU 旧行为，明确传 target
时才读写 SPU 定义和值。
"""
from . import fieldtypes, formulas
from ..errors import ApiError

TARGETS = ("sku", "spu")
TARGET_LABELS = {"sku": "单品", "spu": "SPU"}

# 系统字段（API 统一呈现；readonly 表示不可通过字段值编辑路径修改）
SYSTEM_FIELDS_BY_TARGET = {
    "sku": [
        {"system": True, "target": "sku", "key": "id", "name": "ID", "type": "string",
         "readonly": True, "allow_negative": False, "scale": 0},
        {"system": True, "target": "sku", "key": "name", "name": "名称", "type": "string",
         "readonly": False, "allow_negative": False, "scale": 0},
        {"system": True, "target": "sku", "key": "qty", "name": "库存数量", "type": "int",
         "readonly": True, "allow_negative": False, "scale": 0},
        {"system": True, "target": "sku", "key": "spu_id", "name": "SPU ID", "type": "int",
         "readonly": True, "allow_negative": False, "scale": 0},
        {"system": True, "target": "sku", "key": "spu_name", "name": "SPU 名称", "type": "string",
         "readonly": True, "allow_negative": False, "scale": 0},
    ],
    "spu": [
        {"system": True, "target": "spu", "key": "id", "name": "ID", "type": "int",
         "readonly": True, "allow_negative": False, "scale": 0},
        {"system": True, "target": "spu", "key": "name", "name": "名称", "type": "string",
         "readonly": False, "allow_negative": False, "scale": 0},
        {"system": True, "target": "spu", "key": "sku_count", "name": "SKU 数", "type": "int",
         "readonly": True, "allow_negative": False, "scale": 0},
        {"system": True, "target": "spu", "key": "total_qty", "name": "合计库存", "type": "int",
         "readonly": True, "allow_negative": False, "scale": 0},
    ],
}
SYSTEM_FIELDS = SYSTEM_FIELDS_BY_TARGET["sku"]
RESERVED_NAMES = {
    target: {field["name"] for field in fields}
    for target, fields in SYSTEM_FIELDS_BY_TARGET.items()
}

VALUE_TABLES = {
    "sku": {"table": "custom_values", "entity_column": "item_id", "entity": "单品"},
    "spu": {"table": "spu_custom_values", "entity_column": "spu_id", "entity": "SPU"},
}


def validate_target(target: str) -> str:
    if target not in TARGETS:
        raise ApiError("FIELD_TARGET_INVALID", f"无效的属性归属：{target}",
                       hint="可选归属：sku（单品）、spu", status=400,
                       user_message="属性归属无效",
                       user_hint="请选择单品或 SPU")
    return target


def definition_from_row(row) -> dict:
    """字段行 → 领域定义；v3/v4 旧行由迁移统一补成 sku。"""
    return {
        "id": row["id"],
        "uid": row["sync_uid"],
        "target": row["target"] if "target" in row.keys() else "sku",
        "name": row["name"],
        "type": row["type"],
        "scale": row["scale"],
        "allow_negative": bool(row["allow_negative"]),
        "formula": formulas.tokens_of(row["formula_json"]),
    }


def load_definition(conn, field_id: int) -> dict:
    row = conn.execute("SELECT * FROM custom_fields WHERE id = ?", (field_id,)).fetchone()
    return definition_from_row(row)


def load_definitions(conn, target: str | None = "sku") -> list:
    """读取字段定义；target=None 仅供完整公式校验，默认仍只返回 SKU。"""
    if target is None:
        rows = conn.execute("SELECT * FROM custom_fields ORDER BY id").fetchall()
    else:
        target = validate_target(target)
        rows = conn.execute(
            "SELECT * FROM custom_fields WHERE target = ? ORDER BY id", (target,)).fetchall()
    return [definition_from_row(row) for row in rows]


def resolve_value_fields(conn, values: dict, target: str = "sku") -> dict:
    """校验 values 的字段名均为指定归属的已有自定义字段，返回名称→字段行。"""
    target = validate_target(target)
    rows = conn.execute(
        "SELECT * FROM custom_fields WHERE target = ?", (target,)).fetchall()
    by_name = {r["name"]: r for r in rows}
    unknown = [key for key in values if key not in by_name]
    if unknown:
        valid = "、".join(sorted(by_name))
        unknown_text = "、".join(unknown)
        label = TARGET_LABELS[target]
        raise ApiError(
            "UNKNOWN_FIELD", f"未知{label}字段：{unknown_text}",
            hint=f"有效{label}自定义字段：{valid or '（无）'}；系统属性通过请求体顶层字段修改",
            status=400,
            user_message=f"未知字段：{unknown_text}",
            user_hint=(f"有效{label}自定义字段：" + valid) if by_name else
                      f"该店铺还没有{label}自定义字段，请先到「属性」页创建")
    return by_name


def set_value(conn, entity_id: int, field_row, value) -> dict:
    """写入/清除单个字段值。value=None 表示清除；计算字段一律拒绝。"""
    target = validate_target(field_row["target"])
    spec = VALUE_TABLES[target]
    if field_row["formula_json"]:
        raise ApiError(
            "FIELD_READONLY",
            f"字段「{field_row['name']}」是计算字段，值由公式自动算出，不能直接写入或清除",
            hint="请修改公式引用的源字段值；如需调整该字段本身，请使用字段页的「编辑公式」",
            status=409,
            user_message=f"字段「{field_row['name']}」是计算字段，值由公式自动算出，不能直接填写或清除",
            user_hint="请修改公式引用的源字段值；如需调整该字段本身，请使用「属性」页的「编辑公式」")

    ename, table = spec["entity_column"], spec["table"]
    if value is None:
        conn.execute(
            f"DELETE FROM {table} WHERE {ename} = ? AND field_id = ?",
            (entity_id, field_row["id"]))
        return {"field": field_row["name"], "action": "clear", "target": target}
    try:
        encoded, display = fieldtypes.validate_and_encode(
            field_row["type"], field_row["scale"], bool(field_row["allow_negative"]), value)
    except ValueError as exc:
        type_desc = field_row["type"] + (
            f"（{field_row['scale']} 位小数）" if field_row["type"] == "decimal" else "")
        raise ApiError(
            "FIELD_VALUE_INVALID", f"字段「{field_row['name']}」的值无效：{exc}",
            hint=f"该字段类型为 {type_desc}", status=400,
            user_message=f"字段「{field_row['name']}」的值无效：{exc}",
            user_hint="该字段类型为 " +
                      fieldtypes.TYPE_LABELS.get(field_row["type"], field_row["type"]) +
                      (f"（{field_row['scale']} 位小数）"
                       if field_row["type"] == "decimal" else ""))
    old = conn.execute(
        f"SELECT value_int, value_text FROM {table} "
        f"WHERE {ename} = ? AND field_id = ?",
        (entity_id, field_row["id"])).fetchone()
    v_int, v_text = (None, display) if field_row["type"] == "string" else (encoded, None)
    conn.execute(
        f"INSERT INTO {table} ({ename}, field_id, value_int, value_text) "
        "VALUES (?, ?, ?, ?) "
        f"ON CONFLICT ({ename}, field_id) DO UPDATE "
        "SET value_int = excluded.value_int, value_text = excluded.value_text",
        (entity_id, field_row["id"], v_int, v_text))
    old_display = None
    if old is not None:
        old_stored = old["value_int"] if old["value_int"] is not None else old["value_text"]
        old_display = fieldtypes.decode(field_row["type"], field_row["scale"], old_stored)
    return {"field": field_row["name"], "old": old_display, "new": display, "target": target}


def _stored_by_entity(conn, target: str, entity_ids: list, defs: list) -> dict:
    """批量读取指定归属的普通字段编码值，返回 entity_id → {uid: raw}。"""
    ids = list(entity_ids)
    out = {entity_id: {} for entity_id in ids}
    if not ids:
        return out
    wanted = {field["id"]: field for field in defs if not field["formula"]}
    if not wanted:
        return out
    spec = VALUE_TABLES[target]
    ename, table = spec["entity_column"], spec["table"]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT {ename} AS entity_id, field_id, value_int, value_text "
        f"FROM {table} WHERE {ename} IN ({marks})", ids).fetchall()
    for row in rows:
        field = wanted.get(row["field_id"])
        if field is None:
            continue
        out[row["entity_id"]][field["uid"]] = (
            row["value_int"] if row["value_int"] is not None else row["value_text"])
    return out


def _decode_plain(out: dict, stored: dict, defs: list) -> None:
    for field in defs:
        if field["formula"]:
            continue
        raw = stored.get(field["uid"])
        if raw is not None:
            out[field["name"]] = fieldtypes.decode(field["type"], field["scale"], raw)


def load_values(conn, contexts: list, defs: list | None = None, engine=None):
    """读取 SKU 普通值并计算 SKU 公式，保持原有返回契约。"""
    ids = [context["id"] for context in contexts]
    values = {entity_id: {} for entity_id in ids}
    calculations = {entity_id: {} for entity_id in ids}
    if not ids:
        return values, calculations
    defs = load_definitions(conn, "sku") if defs is None else defs
    stored = _stored_by_entity(conn, "sku", ids, defs)
    if engine is None:
        engine = formulas.Engine(defs, target="sku")
    for context in contexts:
        entity_id = context["id"]
        _decode_plain(values[entity_id], stored[entity_id], defs)
        if not engine.computed:
            continue
        computed, cells = engine.compute({**context, "stored": stored[entity_id]})
        values[entity_id].update(computed)
        calculations[entity_id] = cells
    return values, calculations


def load_spu_values(conn, contexts: list, defs: list | None = None, sku_defs: list | None = None):
    """批量读取 SPU 普通值并计算 SPU 公式；SUM 使用子 SKU 的内部 Decimal 值。"""
    ids = [context["id"] for context in contexts]
    values = {entity_id: {} for entity_id in ids}
    calculations = {entity_id: {} for entity_id in ids}
    if not ids:
        return values, calculations
    defs = load_definitions(conn, "spu") if defs is None else defs
    sku_defs = load_definitions(conn, "sku") if sku_defs is None else sku_defs
    spu_stored = _stored_by_entity(conn, "spu", ids, defs)

    parsed = {field["uid"]: formulas.parse(field["formula"])
              for field in defs if field["formula"]}
    has_aggregate = any(item.has_aggregate for item in parsed.values())
    sku_engine = formulas.Engine(sku_defs, target="sku") if has_aggregate else None
    spu_engine = formulas.Engine(defs, target="spu", sku_engine=sku_engine)

    item_stored = {}
    if has_aggregate:
        item_ids = [sku["id"] for context in contexts for sku in context.get("skus", [])]
        item_stored = _stored_by_entity(conn, "sku", item_ids, sku_defs)

    for context in contexts:
        entity_id = context["id"]
        _decode_plain(values[entity_id], spu_stored[entity_id], defs)
        if not spu_engine.computed:
            continue
        skus = [
            {**sku, "stored": item_stored.get(sku["id"], {})}
            for sku in context.get("skus", [])
        ]
        computed, cells = spu_engine.compute({
            "total_qty": context.get("total_qty", 0),
            "sku_count": context.get("sku_count", 0),
            "stored": spu_stored[entity_id],
            "skus": skus,
        })
        values[entity_id].update(computed)
        calculations[entity_id] = cells
    return values, calculations
