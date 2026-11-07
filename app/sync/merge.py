"""按稳定身份三方合并，所有选择只影响候选模型。"""
import copy
import sqlite3
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from .. import schema
from ..catalog import formulas
from ..errors import ApiError
from . import snapshot
from .common import fail
from ..storage.files import read_json, dump

REFERENCES = {"items": {"spu_id": "spus"}, "custom_values": {"item_id": "items", "field_id": "custom_fields"},
              "movements": {"item_id": "items"}}
TECHNICAL = {"_id", "sync_uid", "created_at", "updated_at"}
# 计算字段的完整定义单元：公式与结果格式必须整体取用，不能跨版本拼装
DEFINITION_FIELDS = ("type", "scale", "allow_negative", "formula_json")
SYSTEM_LABEL = {"qty": "库存数量", "spu_id": "SPU ID"}


def load(root):
    manifest = snapshot.validate_files(root)
    rows, metas = {}, {}
    for database in manifest["databases"]:
        key = database["key"]
        meta = read_json(Path(root) / key / "schema.json")
        metas[key] = meta
        tables = {t: list(snapshot.iter_rows(root, key, t)) for t in meta["tables"]}
        mappings = {t: {r["id"]: r["sync_uid"] for r in data} for t, data in tables.items() if t != "custom_values"}
        for table, data in tables.items():
            for original in data:
                row = dict(original)
                # v3 快照没有 formula_json 列：只补成 NULL，避免「缺键」与「NULL」被当成业务修改
                if table == "custom_fields" and "formula_json" not in row:
                    row["formula_json"] = None
                if "id" in row:
                    row["_id"] = row.pop("id")
                for column, target in REFERENCES.get(table, {}).items():
                    row[column] = mappings[target][row[column]]
                uid = row.get("sync_uid") or row["item_id"] + ":" + row["field_id"]
                rows[key + "/" + table + "/" + uid] = row
    return {"manifest": manifest, "rows": rows, "metas": metas}


def logical(row):
    return None if row is None else {k: v for k, v in row.items() if k not in TECHNICAL}


def three(base, local, remote):
    if local == remote:
        return local, False
    if local == base:
        return remote, False
    if remote == base:
        return local, False
    return local, True


def display(value):
    if isinstance(value, dict):
        return {k: display(v) for k, v in value.items() if k not in ("_id", "sync_uid")}
    if isinstance(value, list):
        return [display(v) for v in value]
    if type(value) is int:
        return str(value)
    if isinstance(value, bytes):
        return snapshot.encode(value)
    return value


class Merger:
    def __init__(self, base, local, remote, session, timestamp):
        self.models = [load(p) for p in (base, local, remote)]
        if len({m["manifest"]["dataset"] for m in self.models}) != 1:
            fail("DATASET_CONFLICT", "不同数据集不能逐项合并，请先选择初始化来源",
                 user_message="本地与远端不是同一套数据，无法逐项合并",
                 user_hint="请先在同步页面明确选择以本地数据还是远端数据为准")
        self.session, self.timestamp = session, timestamp
        self.choices = {}
        self.conflicts, self.changes, self.renumber = [], [], []
        self.detail_sets = {}

    def conflict(self, key, field, values, kind="field", editable=False, extra=None):
        identity = key + "#" + field
        choice = self.choices.get(identity)
        resolved = choice is not None
        disabled = {source: "该版本没有此对象，请选择已有名称或输入新名称"
                    for source, value in zip(("base", "local", "remote"), values)
                    if kind == "name" and value is None}
        value = next((v for v in values[1:] + values[:1] if v is not None), None) if kind == "name" else values[1]
        if choice:
            source = choice.get("source")
            if source in disabled:
                fail("RESOLUTION_INVALID", disabled[source],
                     user_message="这个版本没有该对象，无法采用",
                     user_hint="请选择已有的名称，或直接输入一个新名称")
            if source in ("base", "local", "remote"):
                value = values[("base", "local", "remote").index(source)]
            elif source == "custom" and editable:
                value = choice.get("value")
            else:
                fail("RESOLUTION_INVALID", "该冲突不支持此处理方式",
                     user_message="这项冲突不支持该处理方式",
                     user_hint="请改用页面上提供的其他选项")
        if kind == "field_group":
            self.detail_sets[identity] = [v["values"] for v in values]
            shown = [{"definition": self.field_display(v["definition"]), "关联值数量": sum(x is not None for x in v["values"].values())} for v in values]
            shown_result = {"definition": self.field_display(value["definition"]), "关联值数量": sum(x is not None for x in value["values"].values())}
        else:
            shown, shown_result = [display(v) for v in values], display(value)
        self.conflicts.append({"label": self.describe(key), "id": identity, "key": key, "field": field, "kind": kind,
                               "base": shown[0], "local": shown[1], "remote": shown[2],
                               "result": shown_result, "resolved": resolved, "editable": editable, "disabled_sources": disabled, **(extra or {})})
        if kind == "value" and extra and "display_values" in extra:
            source = choice.get("source") if choice else "local"
            self.conflicts[-1]["display_result"] = str(choice.get("value")) if source == "custom" else extra["display_values"][("base", "local", "remote").index(source)]
        return value

    def build(self, choices=None):
        self.choices = choices or {}
        self.conflicts, self.changes, self.renumber = [], [], []
        sides = [m["rows"] for m in self.models]
        keys = sorted(set().union(*(set(s) for s in sides)))
        result, grouped = {}, set()
        # 整店去留先于逐行检查：共同版本里的店铺被某一侧删除时，所属业务记录会因
        # 「共同版本有、该侧没有」被当成篡改不可变记录。这里以店铺 sync_uid 为界，
        # 把索引行与该店铺库下的全部记录当成一个单元处理。
        self.shop_units(sides, result, grouped)
        # 字段定义变化/删除与另一侧值修改必须共同处理，避免用错误类型解释编码。
        for key in keys:
            if key in grouped:
                continue
            if "/custom_fields/" not in key:
                continue
            b, l, r = [s.get(key) for s in sides]
            uid = key.rsplit("/", 1)[1]
            dbkey = key.split("/")[0]
            related = [k for k in keys if k.startswith(dbkey + "/custom_values/") and k.endswith(":" + uid)]
            definition = lambda row: None if row is None else tuple(row[f] for f in DEFINITION_FIELDS)
            defs = [definition(x) for x in (b, l, r)]
            values = [{k: s.get(k) for k in related} for s in sides]
            # 任一侧是计算字段（或将成为计算字段）时，type/scale/allow_negative/formula_json
            # 必须作为完整定义单元整体取用；同一公式的 token 数组也不做逐项自动合并
            computed = any(x is not None and x["formula_json"] for x in (b, l, r))
            if computed:
                tied = defs[1] != defs[2] and defs[1] != defs[0] and defs[2] != defs[0]
            else:
                tied = ((defs[1] != defs[0] and values[2] != values[0]) or
                        (defs[2] != defs[0] and values[1] != values[0])) and (defs[1], values[1]) != (defs[2], values[2])
            if b is not None and tied:
                groups = [{"definition": row, "values": vals} for row, vals in zip((b, l, r), values)]
                chosen = self.conflict(key, "$definition_and_values", groups, "field_group",
                                       extra={"affected": len(related)})
                if chosen["definition"] is not None:
                    result[key] = copy.deepcopy(chosen["definition"])
                    result.update({k: copy.deepcopy(v) for k, v in chosen["values"].items() if v is not None})
                grouped.update([key, *related])
        for key in keys:
            if key in grouped:
                continue
            b, l, r = [s.get(key) for s in sides]
            table = key.split("/")[1]
            if table in ("movements", "audit_log") and b is not None:
                if l != b or r != b:
                    # 已存在的流水和审计不可编辑；异常修改不能通过合并改变历史。
                    self.conflict(key, "$immutable", [b, l, r], "immutable")
                    if self.choices.get(key + "#$immutable", {}).get("source") not in (None, "base"):
                        fail("IMMUTABLE_RECORD", "共同版本中的流水和审计只能保留原记录",
                             user_message="已有的流水记录不能改动",
                             user_hint="请选择「保留共同版本」；库存差异可通过追加调整流水修正")
                result[key] = copy.deepcopy(b)
                continue
            if b is None or l is None or r is None or table in ("movements", "audit_log"):
                proposed, conflict = three(logical(b), logical(l), logical(r))
                selected = l if proposed == logical(l) else r if proposed == logical(r) else b
                if conflict:
                    selected = self.conflict(key, "$record", [b, l, r], "record")
                if selected is not None:
                    result[key] = copy.deepcopy(selected)
            else:
                merged = copy.deepcopy(r)
                fields = set(b) | set(l) | set(r)
                if table == "items":
                    placements = [{f: row[f] for f in ("spu_id", "seq")} for row in (b, l, r)]
                    placement, conflict = three(*placements)
                    if conflict:
                        placement = self.conflict(key, "$placement", placements, "placement")
                    merged.update(placement)
                    fields -= {"spu_id", "seq"}
                if table == "custom_values":
                    vals = [{f: row[f] for f in ("value_int", "value_text")} for row in (b, l, r)]
                    value, conflict = three(*vals)
                    if conflict:
                        field_key = key.split("/")[0] + "/custom_fields/" + l["field_id"]
                        definitions = [s.get(field_key) for s in sides]
                        shown = [self.value_display(v, definition) for v, definition in zip(vals, definitions)]
                        value = self.conflict(key, "$value", vals, "value", True, {"display_values": shown})
                        if not isinstance(value, dict):
                            value = self.encode_value(value, result.get(field_key) or r and sides[2].get(field_key))
                    merged.update(value)
                    fields -= {"value_int", "value_text"}
                for field in fields - TECHNICAL:
                    values = [row.get(field) for row in (b, l, r)]
                    value, conflict = three(*values)
                    if conflict:
                        value = self.conflict(key, field, values, editable=field == "name")
                    merged[field] = value
                if "updated_at" in merged:
                    merged["updated_at"] = max(l["updated_at"], r["updated_at"])
                result[key] = merged
            if logical(b) != logical(result.get(key)):
                self.changes.append({"key": key, "base": display(b), "result": display(result.get(key))})
        self.fix_dependencies(result, sides)
        self.unique_names(result)
        self.idempotency(result, sides)
        self.inventory(result, sides)
        return result

    def shop_units(self, sides, result, grouped):
        """整店去留单元：共同版本存在、某一侧索引已删除的店铺。

        店铺库文件的库键即该店铺的 sync_uid，故「索引行 + 该库键下全部记录」构成
        一个不可拆分的单元。保留侧仍保持共同版本内容时，删除成立（不逐条产生
        immutable 冲突）；保留侧修改过店铺或其所属记录时，产生一项关联冲突，
        由用户在「采用本地/采用远端/采用共同版本」中整套取舍，不能只恢复索引行
        或只保留没有索引的业务行。
        """
        keys = sorted(set().union(*(set(s) for s in sides)))
        rows_of = {}
        for key in keys:
            database, table, _ = key.split("/", 2)
            if database == "index" and table == "shops":
                rows_of[key.split("/", 2)[2]] = [
                    k for k in keys if k == key or k.startswith(key.split("/", 2)[2] + "/")]

        for uid, rows in rows_of.items():
            index_key = "index/shops/" + uid
            if index_key not in sides[0]:
                continue
            local_has, remote_has = index_key in sides[1], index_key in sides[2]
            if local_has and remote_has:
                continue
            grouped.update(rows)
            removal = {"key": index_key, "base": display(sides[0].get(index_key)),
                       "note": "整间店铺已删除，所属记录一并移除"}
            if not local_has and not remote_has:
                # 双方都删除：整店一并移除
                self.changes.append(removal)
                continue
            kept = sides[1] if local_has else sides[2]
            content = lambda side: {k: logical(side.get(k)) for k in rows}
            if content(kept) == content(sides[0]):
                # 保留侧未改动共同版本内容：删除成立
                self.changes.append(removal)
                continue
            values = [sides[i].get(index_key) for i in range(3)]
            identity = index_key + "#$references"
            self.detail_sets[identity] = [{k: side.get(k) for k in rows} for side in sides]
            chosen = self.conflict(index_key, "$references", values, "dependencies",
                                   extra={"affected": len(rows) - 1})
            choice = self.choices.get(identity)
            if choice is None:
                # 去留尚未选择：候选内容不结算，也不提前登记逐条不可变冲突
                continue
            if chosen is None:
                # 选择了「没有该店铺」的版本：整店及其所属记录一并从候选结果移除
                self.changes.append(removal)
                continue
            source = choice.get("source", "local")
            side = sides[("base", "local", "remote").index(source)]
            for key in rows:
                base_row, row = sides[0].get(key), side.get(key)
                table = key.split("/")[1]
                if table in ("movements", "audit_log") and base_row is not None:
                    # 整店来源选择只豁免「整店删除导致的整体缺失」，不豁免已有流水
                    # 与审计的不可变约束：所选版本仍须与共同版本逐条一致
                    if row != base_row:
                        self.conflict(key, "$immutable", [base_row, sides[1].get(key), sides[2].get(key)], "immutable")
                        if self.choices.get(key + "#$immutable", {}).get("source") not in (None, "base"):
                            fail("IMMUTABLE_RECORD", "共同版本中的流水和审计只能保留原记录",
                                 user_message="已有的流水记录不能改动",
                                 user_hint="请选择「保留共同版本」；库存差异可通过追加调整流水修正")
                    result[key] = copy.deepcopy(base_row)
                elif row is not None:
                    result[key] = copy.deepcopy(row)
            if content(side) != content(sides[0]):
                # 逐行变更记录对整店单元被跳过，这里补一条整店摘要供候选总览
                self.changes.append({"key": index_key, "base": display(sides[0].get(index_key)),
                                     "result": display(side.get(index_key))})

    def describe(self, key):
        database, table, uid = key.split("/", 2)
        def find(k):
            return next((model["rows"][k] for model in reversed(self.models) if k in model["rows"]), {})
        shop = find("index/shops/" + database).get("name", "全局")
        row = find(key)
        name = row.get("name")
        if table == "custom_values":
            item, field = uid.split(":", 1)
            name = find(database + "/items/" + item).get("name", item[:8]) + " · " + find(database + "/custom_fields/" + field).get("name", field[:8])
        elif table == "movements":
            name = find(database + "/items/" + row.get("item_id", "")).get("name", "SKU") + " · 流水 " + str(row.get("_id", ""))
        types = {"shops": "店铺", "spus": "SPU", "items": "SKU", "custom_fields": "字段", "custom_values": "字段值", "movements": "库存流水", "audit_log": "审计"}
        return shop + " / " + types.get(table, table) + " / " + str(name or row.get("_id", uid[:8]))

    def details(self, identity, offset, limit):
        values = self.detail_sets.get(identity)
        if values is None:
            fail("CONFLICT_STALE", "该关联组不存在",
                 user_message="这条关联记录已不在本次冲突中",
                 user_hint="请关闭窗口后重新打开同步页面")
        keys = sorted(set().union(*(set(v) for v in values)))
        def show(side, key):
            row = values[side].get(key)
            if row and "/custom_values/" in key:
                field = self.models[side]["rows"].get(key.split("/")[0] + "/custom_fields/" + row["field_id"])
                return self.value_display(row, field)
            return display(row)
        return {"total": len(keys), "rows": [{"label": self.describe(k), "base": show(0, k),
                "local": show(1, k), "remote": show(2, k)} for k in keys[offset:offset + limit]]}

    @staticmethod
    def value_display(value, definition):
        if definition is None:
            return "字段不存在"
        if definition["type"] == "string":
            return value["value_text"]
        if definition["type"] == "bool":
            return "true" if value["value_int"] else "false"
        scale = definition["scale"] if definition["type"] == "decimal" else 2 if definition["type"] == "percent" else 0
        return str(Decimal(value["value_int"]).scaleb(-scale))

    def field_display(self, row):
        """字段定义的四列对照文本：计算字段额外展示可读公式"""
        if row is None:
            return None
        shown = {k: v for k, v in row.items() if k not in ("_id", "sync_uid")}
        if row.get("formula_json"):
            shown = dict(shown)
            shown["formula_json"] = self.formula_text(row["formula_json"])
        return display(shown)

    def formula_text(self, text):
        try:
            tokens = formulas.tokens_of(text)
        except ApiError:
            return text
        parts = []
        for token in tokens or []:
            if token["kind"] == "number":
                parts.append(token["value"])
            elif token["kind"] == "field":
                parts.append(self.field_name(token["uid"]))
            elif token["kind"] == "system":
                parts.append(SYSTEM_LABEL.get(token["key"], token["key"]))
            else:
                parts.append(formulas.SYMBOL_TEXT[token["value"]])
        return " ".join(parts)

    def field_name(self, uid):
        for model in reversed(self.models):
            for key, row in model["rows"].items():
                if "/custom_fields/" in key and row.get("sync_uid") == uid:
                    return row.get("name") or uid[:8]
        return uid[:8]

    @staticmethod
    def encode_value(value, field):
        if field is None:
            fail("FIELD_INVALID", "请先处理字段定义冲突",
                 user_message="该字段的定义还没有确定",
                 user_hint="请先处理字段定义上的冲突，再填写字段值")
        t = field["type"]
        if t == "string":
            return {"value_int": None, "value_text": str(value)}
        if t == "bool":
            if str(value).lower() not in ("true", "false"):
                fail("FIELD_INVALID", "布尔值请输入 true 或 false",
                     user_message="开关字段只能填 true 或 false",
                     user_hint="请输入 true（开）或 false（关）")
            integer = int(str(value).lower() == "true")
        else:
            try:
                number = Decimal(str(value))
                scale = field["scale"] if t == "decimal" else 2 if t == "percent" else 0
                scaled = number.scaleb(scale)
                if not scaled.is_finite() or scaled != scaled.to_integral_value():
                    raise ValueError()
                integer = int(scaled)
            except (InvalidOperation, ValueError):
                fail("FIELD_INVALID", "输入值超出字段类型或精度",
                     user_message="填写的值与字段类型或小数位数不符",
                     user_hint="请检查小数位数，或改用该字段允许的取值")
        if not -(2**63) <= integer < 2**63 or (integer < 0 and not field["allow_negative"]):
            fail("FIELD_INVALID", "输入值超出字段允许范围",
                 user_message="填写的值超出该字段允许的范围",
                 user_hint="请检查是否为负数，或数值是否过大")
        return {"value_int": integer, "value_text": None}

    def fix_dependencies(self, result, sides):
        # 删除父对象却保留子对象时，必须显式选择恢复父对象或删除关联组。
        missing = defaultdict(list)
        for key, row in result.items():
            database, table, uid = key.split("/", 2)
            for column, target in REFERENCES.get(table, {}).items():
                parent = database + "/" + target + "/" + row[column]
                if parent not in result:
                    missing[parent].append(key)
        for parent, children in missing.items():
            value = self.conflict(parent, "$references", [s.get(parent) for s in sides], "dependencies", extra={"children": children})
            if value is None:
                for child in children:
                    result.pop(child, None)
            else:
                result[parent] = copy.deepcopy(value)

    def unique_names(self, result):
        groups = defaultdict(list)
        for key, row in result.items():
            database, table, uid = key.split("/", 2)
            if table in ("shops", "custom_fields"):
                groups[(database, table, row["name"])].append(key)
        for keys in groups.values():
            if len(keys) > 1:
                for key in keys:
                    names = [model["rows"].get(key, {}).get("name") for model in self.models]
                    name = self.conflict(key, "$unique_name", names, "name", True)
                    if not isinstance(name, str) or not name.strip():
                        fail("NAME_INVALID", "名称不能为空",
                             user_message="名称不能为空",
                             user_hint="请为这条记录填写一个与其他记录不同的名称")
                    result[key]["name"] = name.strip()

    def idempotency(self, result, sides):
        groups = defaultdict(list)
        for key, row in result.items():
            if "/movements/" in key and row.get("idempotency_key"):
                groups[(key.split("/")[0], row["idempotency_key"])].append(key)
        for (_, token), keys in groups.items():
            if len(keys) < 2:
                continue
            payload = lambda row: {k: v for k, v in row.items() if k not in TECHNICAL | {"actor"}}
            remote_first = sorted(keys, key=lambda k: k not in sides[2])
            if len({dump(payload(result[k])) for k in keys}) == 1:
                keep = remote_first[0]
                self.changes.append({"key": keep, "note": "相同幂等键及业务内容已去重", "duplicates": keys})
            else:
                rows = [next((result[k] for k in keys if k in side), None) for side in sides]
                chosen = self.conflict(remote_first[0], "$idempotency", rows, "idempotency")
                keep = next((k for k in keys if result[k] == chosen), None)
            for key in keys:
                if key != keep:
                    result.pop(key, None)

    def inventory(self, result, sides):
        def quantities(rows):
            values = defaultdict(int)
            for key, row in rows.items():
                if "/movements/" in key:
                    values[(key.split("/")[0], row["item_id"])] += row["qty"]
            return values
        changed = []
        for side in sides[1:]:
            changed.append({(k.split("/")[0], row["item_id"]) for k, row in side.items()
                            if "/movements/" in k and k not in sides[0]})
        qs = [quantities(s) for s in sides]
        current = quantities(result)
        for database, item in sorted((changed[0] & changed[1]) | {k for k, v in current.items() if v < 0}):
            key = database + "/items/" + item
            vals = [q[(database, item)] for q in qs]
            movement_sets = [{k: row for k, row in side.items() if k.startswith(database + "/movements/") and row["item_id"] == item and k not in sides[0]} for side in sides]
            self.detail_sets[key + "#$inventory"] = movement_sets
            target = self.conflict(key, "$inventory", vals, "inventory", True,
                                   {"proposed_qty": str(current[(database, item)]), "affected": len(set().union(*(set(s) for s in movement_sets)))})
            try:
                target = int(str(target))
            except ValueError:
                fail("STOCK_INVALID", "目标库存必须是非负整数",
                     user_message="目标库存需要填写 0 或正整数",
                     user_hint="请输入不含小数、不小于 0 的数量")
            if not 0 <= target < 2**63:
                fail("STOCK_INVALID", "目标库存必须是非负整数",
                     user_message="目标库存超出允许范围",
                     user_hint="请填写一个合理的不小于 0 的整数")
            if key + "#$inventory" in self.choices and target != current[(database, item)]:
                uid = uuid.uuid5(uuid.NAMESPACE_URL, self.session + key).hex
                result[database + "/movements/" + uid] = {
                    "sync_uid": uid, "_id": 0, "item_id": item, "type": "adjust", "qty": target - current[(database, item)],
                    "unit_price": None, "batch_no": None, "supplier": None, "channel": None, "customer": None,
                    "note": "同步冲突复核后的库存调整", "idempotency_key": "sync:" + uid,
                    "actor": "sync:user", "created_at": self.timestamp}

    def materialize(self, result, directory):
        directory = Path(directory)
        directory.mkdir(parents=True)
        by_database = defaultdict(lambda: defaultdict(dict))
        for key, row in result.items():
            database, table, uid = key.split("/", 2)
            by_database[database][table][uid] = copy.deepcopy(row)
        mappings = {}
        for database, tables in by_database.items():
            mappings[database] = {}
            for table, rows in tables.items():
                if table == "custom_values":
                    continue
                high = max([r.get("_id", 0) for r in rows.values()] + [m["metas"].get(database, {}).get("sequences", {}).get(table, 0) for m in self.models])
                allocated = set()
                mapping = mappings[database][table] = {}
                ordered = sorted(rows, key=lambda uid: (database + "/" + table + "/" + uid not in self.models[0]["rows"],
                                                        database + "/" + table + "/" + uid not in self.models[2]["rows"], uid))
                for uid in ordered:
                    row = rows[uid]
                    old = row.pop("_id")
                    new = old
                    if new < 1 or new in allocated:
                        high += 1
                        new = high
                    allocated.add(new)
                    mapping[uid] = row["id"] = new
                    if old and old != new:
                        self.renumber.append({"database": database, "table": table, "uid": uid, "old": str(old), "new": str(new)})
            items = tables.get("items", {})
            used, highseq = defaultdict(set), defaultdict(int)
            for row in items.values():
                highseq[row["spu_id"]] = max(highseq[row["spu_id"]], row["seq"])
            for uid in sorted(items, key=lambda uid: (database + "/items/" + uid not in self.models[0]["rows"], database + "/items/" + uid not in self.models[2]["rows"], uid)):
                row = items[uid]
                spu = row["spu_id"]
                if row["seq"] in used[spu]:
                    old = row["seq"]
                    highseq[spu] += 1
                    row["seq"] = highseq[spu]
                    self.renumber.append({"database": database, "table": "items.seq", "uid": uid, "old": str(old), "new": str(row["seq"])})
                used[spu].add(row["seq"])
        shops = by_database["index"].get("shops", {})
        for uid, row in shops.items():
            row["db_file"] = "shops/" + str(row["id"]) + ".db"
        if set(by_database) - {"index"} - set(shops):
            fail("SHOP_CONFLICT", "候选中存在没有店铺索引的业务库",
                 user_message="合并结果中有店铺数据缺少对应的店铺项",
                 user_hint="请在冲突处理中确认店铺相关记录的处理方式")
        for database in ["index", *shops]:
            tables = by_database[database]
            kind = "index" if database == "index" else "shop"
            filename = directory / ("index.db" if kind == "index" else shops[database]["db_file"])
            filename.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(filename)
            try:
                conn.executescript(schema.INDEX_DDL if kind == "index" else schema.SHOP_DDL)
                for table, rows in tables.items():
                    for row in rows.values():
                        for column, target in REFERENCES.get(table, {}).items():
                            if row[column] not in mappings[database].get(target, {}):
                                fail("REFERENCE_CONFLICT", "候选结果仍有未解决的引用",
                                     user_message="合并结果中还有关联记录没有确定",
                                     user_hint="请回到冲突列表，逐项处理后再重新校验")
                            row[column] = mappings[database][target][row[column]]
                        # 审计 entity_id/detail 保存发生时的编号；当前对象只按 entity_uid 关联，
                        # 不能像业务外键一样重映射，否则会改写不可变历史。
                        columns = list(row)
                        conn.execute("INSERT INTO " + snapshot.quote(table) + " (" + ",".join(map(snapshot.quote, columns)) + ") VALUES (" + ",".join("?" for _ in columns) + ")", [row[c] for c in columns])
                if kind == "shop":
                    uid = uuid.uuid5(uuid.NAMESPACE_URL, self.session + database + ":audit").hex
                    conn.execute("INSERT INTO audit_log(sync_uid,actor,action,entity_type,entity_id,entity_uid,detail,created_at) VALUES (?,?,?,?,?,?,?,?)",
                                 (uid, "sync:user", "sync_merge", "sync", None, None,
                                  dump({"session": self.session, "renumber": [r for r in self.renumber if r["database"] == database]}), self.timestamp))
                for table in {t for m in self.models for t in m["metas"].get(database, {}).get("sequences", {})}:
                    high = max(m["metas"].get(database, {}).get("sequences", {}).get(table, 0) for m in self.models)
                    if conn.execute("SELECT 1 FROM sqlite_sequence WHERE name=?", (table,)).fetchone():
                        conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?", (high, table))
                    else:
                        conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (table, high))
                conn.execute("PRAGMA user_version=" + str(snapshot.VERSIONS[kind]))
                conn.commit()
                snapshot.validate_business(conn, kind)
            finally:
                conn.close()
        return directory
