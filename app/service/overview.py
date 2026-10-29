"""概览统计：库存总数量与异常条目计数（异常判定走可插拔规则注册表）

异常条目 == 命中任一已注册规则的 SKU，条目级去重：同一条目命中多条规则只计一次。
规则是「稳定标识 + 展示名 + 判定函数」的组合，判定函数接收单品求值快照：

    ctx = {"id": 内部整数, "ref": 对外标识「SPU 号-组内序号」, "qty": 当前库存,
           "values": 自定义字段显示值, "calculations": 计算字段结果与错误}

后续「自定义异常条目满足条件」按同一形态向 ANOMALY_RULES 追加规则，summary()
与接口结构都不用改动。
"""
from .. import db
from . import items

# 规则注册表：顺序即接口 anomalies[] 的输出顺序
ANOMALY_RULES: list[dict] = []


def anomaly_rule(code: str, label: str):
    """注册异常规则的装饰器"""
    def register(check):
        ANOMALY_RULES.append({"code": code, "label": label, "check": check})
        return check
    return register


@anomaly_rule("computed_field_error", "计算字段无法计算")
def _computed_field_error(ctx) -> bool:
    """任一计算字段求值失败（含上游失败传导）即视为无法计算"""
    return any(cell["error"] for cell in ctx["calculations"].values())


@anomaly_rule("negative_qty", "负库存")
def _negative_qty(ctx) -> bool:
    """当前库存为负数"""
    return ctx["qty"] < 0


def summary(shop_id: int) -> dict:
    """全店概览：库存总数量 + 异常条目数量（按规则细分）

    库存总数量 = 全部 SKU 当前库存的算术合计，负库存按负数直接计入。
    一次查询取全部 SKU，一次批量求值算出自定义字段，逐条目跑规则。
    """
    d = db.shop_db(shop_id)
    with d.lock:
        rows = d.conn.execute(
            "SELECT i.id, i.seq, i.spu_id, "
            "COALESCE((SELECT SUM(m.qty) FROM movements m WHERE m.item_id = i.id), 0) AS qty "
            "FROM items i ORDER BY i.spu_id, i.seq").fetchall()
        contexts = [{"id": r["id"], "qty": r["qty"], "spu_id": r["spu_id"]} for r in rows]
        values, calculations = items._load_values(d.conn, contexts)

    total_qty = sum(ctx["qty"] for ctx in contexts)
    counts = {rule["code"]: 0 for rule in ANOMALY_RULES}
    anomaly_items = 0
    for row, ctx in zip(rows, contexts):
        item_ctx = {"id": row["id"],
                    "ref": items.format_item_ref(row["spu_id"], row["seq"]),
                    "qty": row["qty"],
                    "values": values[row["id"]],
                    "calculations": calculations[row["id"]]}
        hit = False
        for rule in ANOMALY_RULES:
            if rule["check"](item_ctx):
                counts[rule["code"]] += 1
                hit = True
        if hit:
            anomaly_items += 1
    return {"total_qty": total_qty, "anomaly_items": anomaly_items,
            "anomalies": [{"code": r["code"], "label": r["label"], "count": counts[r["code"]]}
                          for r in ANOMALY_RULES]}
