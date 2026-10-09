"""数学计算字段：公式解析、依赖校验与逐 SKU 求值（纯领域逻辑，不访问数据库）

公式是版本化的结构化 token 序列，不保存可执行的 Python / JavaScript / SQL 表达式，
也不使用 eval。四类 token：
    {"kind": "number", "value": "12.5"}      数字常量（十进制文本，可为负）
    {"kind": "field",  "uid": "<sync_uid>"}  本店自定义字段或计算字段
    {"kind": "system", "key": "qty"}         数值型系统字段（显式白名单）
    {"kind": "symbol", "value": "*"}         运算符或括号

数值语义：普通字段的编码值先按 fieldtypes.logical_exp 还原成数学值（百分比 25% → 0.25），
全部运算用 Decimal、局部精度 50 位；中间结果不按显示位数取舍，只有输出当前字段时才
按 ROUND_HALF_UP 收敛。本模块不导入 service 包，避免 services 之间产生循环导入。
"""
import json
import re
from collections import defaultdict
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext

from . import fieldtypes
from .errors import ApiError

# 公式对象版本；本次固定为 1
VERSION = 1
# 局部计算精度（有效数字）
PRECISION = 50
# 显示精度统一 0~6 位（默认 2 位由调用方给出）
MAX_SCALE = 6
# 十进制常量：普通整数/小数，允许前导零与负号；不接受布尔、NaN、Infinity、科学计数法
NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

# 可引用的系统字段白名单：库内 ID 恰为整数不代表它可参与数学运算
SYSTEM_KEYS = {"qty": "库存数量", "spu_id": "SPU ID"}
# 计算字段的结果类型（写入 custom_fields.type 的值）
RESULT_TYPES = ("decimal", "percent")

SYMBOL_TEXT = {"+": "+", "-": "−", "*": "×", "/": "÷", "(": "(", ")": ")"}
TYPE_NAMES = {"string": "文本", "bool": "开关", "int": "整数",
              "decimal": "小数", "percent": "百分比"}
_PRECEDENCE = {"neg": 3, "*": 2, "/": 2, "+": 1, "-": 1}


def _invalid(message, hint=None, user_message=None, user_hint=None):
    return ApiError("FORMULA_INVALID", message, hint=hint, status=400,
                    user_message=user_message, user_hint=user_hint)


def _cycle_error(path):
    return ApiError("FORMULA_CYCLE", "公式存在循环引用：" + path,
                    hint="循环路径上的字段互相依赖，请调整其中一个字段的公式引用关系；"
                         "计算字段不能直接或间接引用自身", status=400,
                    user_message="公式存在循环引用：" + path,
                    user_hint="循环路径上的字段互相依赖，请调整其中一个字段的公式引用关系；"
                              "计算字段不能直接或间接引用自身")


class CellError(Exception):
    """单个 SKU 上的单元格错误：只影响该字段及其下游，不令整个列表失败

    payload 与 ApiError 同构（code/message/hint 给 Agent，user_message/user_hint 给界面）。
    """

    def __init__(self, code, message, hint=None, user_message=None, user_hint=None):
        super().__init__(message)
        self.payload = {"code": code, "message": message}
        if hint:
            self.payload["hint"] = hint
        if user_message:
            self.payload["user_message"] = user_message
        if user_hint:
            self.payload["user_hint"] = user_hint


# ---------- token 规范化 ----------

def _as_dict(token) -> dict:
    if isinstance(token, dict):
        return token
    dump = getattr(token, "model_dump", None)
    if callable(dump):
        return dump()
    raise _invalid("公式组件格式无效：应为 {kind, ...} 结构",
                   user_message="公式里有无法识别的内容",
                   user_hint="请清空公式后重新从下方按钮逐项加入")


def canonical_number(text: str) -> str:
    """十进制文本规范化：去前导零、去末尾零、统一 -0 为 0（不改变数值）"""
    d = Decimal(text)
    if d.is_zero():
        return "0"
    return format(d.normalize(), "f")


def _one_token(token) -> dict:
    item = _as_dict(token)
    kind = item.get("kind")
    if kind == "number":
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, str) or not NUMBER_RE.fullmatch(value.strip()):
            raise _invalid(f"无效的数字常量：{value}",
                           hint="只接受普通十进制整数或小数（如 12、-3.5）；"
                                "不支持科学计数法、NaN、Infinity 或表达式",
                           user_message=f"无效的数字常量：{value}",
                           user_hint="只接受普通十进制整数或小数（如 12、-3.5）")
        return {"kind": "number", "value": canonical_number(value.strip())}
    if kind == "field":
        uid = item.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            raise _invalid("字段引用的稳定标识缺失",
                           hint="请重新从「可引用字段」区域点击选择字段",
                           user_message="公式里的字段引用已失效",
                           user_hint="请重新从「可引用字段」区域点击选择字段")
        return {"kind": "field", "uid": uid.strip()}
    if kind == "system":
        key = item.get("key")
        if key not in SYSTEM_KEYS:
            raise _invalid(f"不能引用该系统字段：{key}",
                           hint="可引用的系统字段只有：" +
                                "、".join(f"{v}（{k}）" for k, v in SYSTEM_KEYS.items()),
                           user_message=f"不能引用该系统字段：{key}",
                           user_hint="可引用的系统字段只有：" +
                                     "、".join(SYSTEM_KEYS.values()))
        return {"kind": "system", "key": key}
    if kind == "symbol":
        value = item.get("value")
        if value not in SYMBOL_TEXT:
            raise _invalid(f"无效的运算符：{value}",
                           hint="支持的运算符与括号：+ − × ÷ ( )",
                           user_message=f"无效的运算符：{value}",
                           user_hint="支持的运算符与括号：+ − × ÷ ( )")
        return {"kind": "symbol", "value": value}
    raise _invalid(f"无效的公式组件类型：{kind}",
                   hint="可用组件类型：number / field / system / symbol",
                   user_message="公式里有无法识别的内容",
                   user_hint="请清空公式后重新从下方按钮逐项加入")


# ---------- 解析 ----------

class Parsed:
    """一次解析的结果：规范化 token + RPN + 引用集合"""

    __slots__ = ("tokens", "rpn", "fields", "systems")

    def __init__(self, tokens, rpn):
        self.tokens = tokens
        self.rpn = rpn
        self.fields = {t["uid"] for t in tokens if t["kind"] == "field"}
        self.systems = {t["key"] for t in tokens if t["kind"] == "system"}


def _static_check(rpn) -> None:
    """对纯常数子表达式求值，拒绝可直接确定的除零（如 A/0、A/(2-2)）"""
    stack = []
    for item in rpn:
        if item[0] != "op":
            stack.append(Decimal(item[1]) if item[0] == "num" else None)
            continue
        op = item[1]
        if op == "neg":
            v = stack.pop()
            stack.append(None if v is None else -v)
            continue
        b, a = stack.pop(), stack.pop()
        if op == "/" and b is not None and b == 0:
            raise _invalid("公式中存在可以直接确定的除以零",
                           hint="请检查作为除数或分母的常量与括号内算式，其值不能为 0",
                           user_message="公式里出现了除以零",
                           user_hint="请检查作为除数或分母的常量与括号内算式，其值不能为 0")
        if a is None or b is None:
            stack.append(None)
            continue
        if op == "+":
            stack.append(a + b)
        elif op == "-":
            stack.append(a - b)
        elif op == "*":
            stack.append(a * b)
        else:
            stack.append(a / b)


def parse(tokens) -> Parsed:
    """校验并解析公式 token 序列；返回供校验与求值使用的内部表示。

    语法：乘除同级、加减同级、同级从左向右；负号只允许出现在公式开头及左括号之后
    （负常量本身是一个操作数，如 A × [-3] 合法）；不支持隐式乘法。使用显式栈
    （调度场算法）而非递归下降，任意嵌套深度都不会触发递归限制。
    """
    items = [_one_token(t) for t in (tokens or [])]
    if not items:
        raise _invalid("公式为空：请至少加入一个字段或数字",
                       hint="空公式没有可计算的内容，无法保存",
                       user_message="公式还是空的",
                       user_hint="请至少加入一个字段或数字")

    output, operators = [], []
    expect_operand, prev = True, None
    for item in items:
        if item["kind"] != "symbol":
            if not expect_operand:
                raise _invalid("两个字段或数字相邻，缺少运算符",
                               hint="如 A(B+C) 需要写成 A×(B+C)；不支持隐式乘法",
                               user_message="两个字段或数字相邻，缺少运算符",
                               user_hint="如 A(B+C) 需要写成 A×(B+C)")
            key = item["value"] if item["kind"] == "number" else (
                item["uid"] if item["kind"] == "field" else item["key"])
            output.append(({"number": "num", "field": "field", "system": "sys"}[item["kind"]], key))
            prev, expect_operand = "operand", False
            continue

        sym = item["value"]
        if sym == "(":
            if not expect_operand:
                raise _invalid("左括号前缺少运算符",
                               hint="如 A(B+C) 需要写成 A×(B+C)；不支持隐式乘法",
                               user_message="左括号前缺少运算符",
                               user_hint="如 A(B+C) 需要写成 A×(B+C)")
            operators.append(sym)
            prev = "("
            continue
        if sym == ")":
            if expect_operand:
                raise _invalid("括号内容不完整或为空",
                               hint="请检查括号内是否缺少字段、数字或运算符",
                               user_message="括号内容不完整或为空",
                               user_hint="请检查括号内是否缺少字段、数字或运算符")
            while operators and operators[-1] != "(":
                output.append(("op", operators.pop()))
            if not operators:
                raise _invalid("右括号没有对应的左括号",
                               hint="请检查括号是否配对",
                               user_message="右括号没有对应的左括号",
                               user_hint="请检查括号是否配对")
            operators.pop()
            prev, expect_operand = ")", False
            continue
        if sym == "-" and expect_operand:
            # 负号：仅允许公式开头及左括号之后；减法需要左右都有操作数
            if prev is None or prev == "(":
                operators.append("neg")
                prev = "op"
                continue
            raise _invalid("负号只能出现在公式开头或左括号之后",
                           hint="表示减法的负号要求左右都有操作数；"
                                "负常量请直接输入负数（如 -3）",
                           user_message="负号只能出现在公式开头或左括号之后",
                           user_hint="表示减法的负号要求左右都有操作数；"
                                     "负常量请直接输入负数（如 -3）")
        if expect_operand:
            raise _invalid(f"运算符「{SYMBOL_TEXT[sym]}」的位置不正确：缺少参与运算的字段或数字",
                           hint="公式不能以运算符开头，也不能出现连续的运算符",
                           user_message=f"运算符「{SYMBOL_TEXT[sym]}」的位置不正确："
                                        "缺少参与运算的字段或数字",
                           user_hint="公式不能以运算符开头，也不能出现连续的运算符")
        while operators and operators[-1] != "(" and _PRECEDENCE[operators[-1]] >= _PRECEDENCE[sym]:
            output.append(("op", operators.pop()))
        operators.append(sym)
        prev, expect_operand = "op", True

    if any(op == "(" for op in operators):
        raise _invalid("左括号没有对应的右括号", hint="请检查括号是否配对",
                       user_message="左括号没有对应的右括号",
                       user_hint="请检查括号是否配对")
    if expect_operand:
        raise _invalid("公式结尾缺少字段或数字：不能以运算符结尾",
                       hint="请在结尾补上参与运算的字段或数字",
                       user_message="公式结尾缺少字段或数字：不能以运算符结尾",
                       user_hint="请在结尾补上参与运算的字段或数字")
    while operators:
        output.append(("op", operators.pop()))
    _static_check(output)
    return Parsed(items, output)


def dump(tokens) -> str:
    """规范 JSON：固定键顺序与分隔符，避免同样的公式产生无意义的同步差异"""
    items = [_one_token(t) for t in (tokens or [])]
    return json.dumps({"version": VERSION, "tokens": items},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load(text) -> dict:
    """读取落库的公式 JSON；失败视为数据损坏，返回结构化错误"""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise ApiError("FORMULA_INVALID", "公式定义不是合法 JSON", status=409)
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ApiError("FORMULA_INVALID",
                       f"公式定义版本不受支持：{data.get('version') if isinstance(data, dict) else '?'}",
                       hint=f"当前支持的公式版本为 {VERSION}，请升级应用后重试", status=409,
                       user_message="该公式由更高版本的程序保存，当前版本无法识别",
                       user_hint="请升级程序后重新打开该字段的公式")
    parse(data.get("tokens") or [])
    return {"version": VERSION, "tokens": [_one_token(t) for t in (data.get("tokens") or [])]}


def tokens_of(text) -> list | None:
    """落库文本 → token 列表；NULL/空文本返回 None"""
    if not text:
        return None
    return load(text)["tokens"]


# ---------- 引用、依赖图与循环检测 ----------

def check_refs(parsed: Parsed, by_uid: dict) -> None:
    """引用必须指向本店存在的数值型字段（整数/小数/百分比/计算字段）"""
    for uid in sorted(parsed.fields):
        target = by_uid.get(uid)
        if target is None:
            raise ApiError("FORMULA_REF_INVALID",
                           f"公式引用的字段不存在（标识 {uid[:8]}）",
                           hint="该字段可能已被删除；请重新从「可引用字段」区域选择", status=400,
                           user_message="公式引用的字段不存在",
                           user_hint="该字段可能已被删除；请重新从「可引用字段」区域选择")
        if target["type"] not in fieldtypes.NUMERIC_TYPES:
            raise ApiError("FORMULA_REF_INVALID",
                           f"字段「{target['name']}」是{TYPE_NAMES.get(target['type'], target['type'])}类型，"
                           "不能参与数学计算",
                           hint="只有整数、小数、百分比字段以及计算字段可以引用", status=400,
                           user_message=f"字段「{target['name']}」是"
                                        f"{TYPE_NAMES.get(target['type'], target['type'])}类型，"
                                        "不能参与数学计算",
                           user_hint="只有整数、小数、百分比字段以及计算字段可以引用")


def graph_of(parsed_by_uid: dict) -> dict:
    """依赖图：计算字段 → 它引用的自定义字段（系统字段为叶子，不产生边）"""
    graph = {}
    for uid, parsed in parsed_by_uid.items():
        graph[uid] = set(parsed.fields)
    return graph


def find_cycle(graph: dict):
    """返回实际环路（键序列，首尾相接）或 None；显式栈实现，不限制层级"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {key: WHITE for key in graph}
    for start in sorted(graph):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        path = [start]
        stack = [(start, iter(sorted(graph[start])))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = BLACK
                stack.pop()
                path.pop()
                continue
            if nxt not in color:
                continue            # 引用不存在的键由 check_refs 单独报告
            if color[nxt] == GRAY:
                return path[path.index(nxt):] + [nxt]
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                path.append(nxt)
                stack.append((nxt, iter(sorted(graph[nxt]))))
    return None


def cycle_text(cycle, names: dict) -> str:
    return " → ".join(names.get(key) or key[:8] for key in cycle)


def topological(nodes, graph: dict, names: dict | None = None) -> list:
    """按依赖先后排序（Kahn）；存在环时报告实际环路"""
    names = names or {}
    members = set(nodes)
    deps = {}
    for node in members:
        required = set(graph.get(node, ()))
        if node in required:
            raise _cycle_error(cycle_text([node, node], {node: node}))
        deps[node] = {x for x in required if x in members}
    followers = defaultdict(list)
    for node, required in deps.items():
        for item in required:
            followers[item].append(node)
    ready = sorted(node for node, required in deps.items() if not required)
    order = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for follower in followers[node]:
            deps[follower].discard(node)
            if not deps[follower]:
                ready.append(follower)
        ready.sort()
    if len(order) != len(members):
        cycle = find_cycle(graph) or sorted(members - set(order))
        raise _cycle_error(cycle_text(cycle, names))
    return order


def dependents(parsed_by_uid: dict, uid: str) -> list:
    """返回引用了该字段的计算字段 uid（用于删除与类型转换的依赖保护）"""
    return sorted(other for other, parsed in parsed_by_uid.items() if uid in parsed.fields)


def check(defs, candidate=None) -> dict:
    """校验整店字段定义的公式：语法、引用与完整依赖图上的循环。

    defs: [{"uid","name","type","scale","allow_negative","formula": token 列表或 None}]
    candidate: 可选，候选定义（创建时为尚未落库的新字段，uid 用空串占位）；
               会替换/并入完整依赖图后再做环检测。返回 uid → Parsed。
    """
    by_uid = {d["uid"]: d for d in defs}
    candidate_uid = None
    if candidate is not None:
        candidate_uid = candidate["uid"]
        by_uid[candidate_uid] = dict(candidate)
    parsed = {}
    for row in defs:
        if row["formula"]:
            parsed[row["uid"]] = parse(row["formula"])
    if candidate is not None and candidate["formula"]:
        parsed[candidate_uid] = parse(candidate["formula"])
    for item in parsed.values():
        check_refs(item, by_uid)
    cycle = find_cycle(graph_of(parsed))
    if cycle:
        raise _cycle_error(cycle_text(cycle, {key: row["name"] for key, row in by_uid.items()}))
    return parsed


# ---------- 求值 ----------

def _quantize(value: Decimal, scale: int) -> Decimal:
    """按显示位数取值。结果超出上下文精度时产生的是带指数位数的结果，
    format(…, "f") 仍可正常输出，故这里无需额外兜底；异常交给 compute 的
    单元格错误边界统一转换。"""
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def display_text(field: dict, value: Decimal) -> str:
    """当前字段的输出文本：小数按显示位数取值，百分比乘以 100 并带 % 号"""
    shown = _quantize(value * 100 if field["type"] == "percent" else value, field["scale"])
    return format(shown, "f") + ("%" if field["type"] == "percent" else "")


def display_number(field: dict, value: Decimal) -> float:
    """values 里回给客户端的显示数值（与手填数值同一语义：百分比 25 表示 25%）"""
    number = value * 100 if field["type"] == "percent" else value
    return float(_quantize(number, field["scale"]))


class Engine:
    """一批查询内共用：公式只解析一次，每个 SKU 按拓扑序对每个计算字段求值一次"""

    def __init__(self, defs):
        self.fields = list(defs)
        self.by_uid = {f["uid"]: f for f in self.fields}
        self.plain = [f for f in self.fields
                      if not f["formula"] and f["type"] in fieldtypes.NUMERIC_TYPES]
        self.computed = [f for f in self.fields if f["formula"]]
        self.parsed = {f["uid"]: parse(f["formula"]) for f in self.computed}
        self.order = topological([f["uid"] for f in self.computed], graph_of(self.parsed),
                                 {f["uid"]: f["name"] for f in self.fields})

    def _reference(self, kind, key, values, errors, systems, field):
        if kind == "sys":
            return systems[key]
        if key in errors:
            return _raise_upstream(field, self.by_uid.get(key), errors[key])
        if key not in values:
            name = (self.by_uid.get(key) or {}).get("name") or key[:8]
            raise CellError("FORMULA_SOURCE_MISSING",
                            f"字段「{field['name']}」引用的「{name}」没有值",
                            hint=f"请先为该 SKU 填写「{name}」，或调整公式",
                            user_message=f"字段「{field['name']}」引用的「{name}」没有值",
                            user_hint=f"请先为该单品填写「{name}」，或调整公式")
        return values[key]

    def _evaluate(self, parsed, values, errors, systems, field):
        stack = []
        try:
            for item in parsed.rpn:
                if item[0] == "num":
                    stack.append(Decimal(item[1]))
                    continue
                if item[0] != "op":
                    stack.append(self._reference(item[0], item[1], values, errors, systems, field))
                    continue
                op = item[1]
                if op == "neg":
                    stack.append(-stack.pop())
                    continue
                b, a = stack.pop(), stack.pop()
                if op == "+":
                    stack.append(a + b)
                elif op == "-":
                    stack.append(a - b)
                elif op == "*":
                    stack.append(a * b)
                else:
                    if b == 0:
                        raise CellError("FORMULA_DIV_ZERO",
                                        f"字段「{field['name']}」的计算出现了除以零",
                                        hint="请检查作为除数或分母的字段取值",
                                        user_message=f"字段「{field['name']}」的计算出现了除以零",
                                        user_hint="请检查作为除数或分母的字段取值")
                    stack.append(a / b)
        except CellError:
            raise
        except DecimalException:
            raise CellError("FORMULA_RANGE",
                            f"字段「{field['name']}」的计算结果无法表示",
                            hint="请检查公式取值范围是否过大",
                            user_message=f"字段「{field['name']}」的计算结果无法表示",
                            user_hint="请检查公式取值范围是否过大")
        result = stack[0]
        if not result.is_finite():
            raise CellError("FORMULA_RANGE",
                            f"字段「{field['name']}」的计算结果无法表示",
                            hint="请检查公式取值范围是否过大",
                            user_message=f"字段「{field['name']}」的计算结果无法表示",
                            user_hint="请检查公式取值范围是否过大")
        if result < 0 and not field["allow_negative"]:
            raise CellError("FORMULA_NEGATIVE",
                            f"字段「{field['name']}」的计算结果为负数，但该字段不允许负数",
                            hint="如确需负值，请编辑该字段并勾选「允许负数」；否则请调整公式",
                            user_message=f"字段「{field['name']}」的计算结果为负数，但该字段不允许负数",
                            user_hint="如确需负值，请编辑该字段并勾选「允许负数」；否则请调整公式")
        return result

    def _output(self, field, result):
        """结果 → 显示文本与显示数值。量化与浮点转换都在这层出错边界内：
        两者都可能抛出 DecimalException（如结果位数超出上下文精度），
        若不拦住，一个字段就能让整个 SKU 列表/详情查询失败。"""
        try:
            return display_text(field, result), display_number(field, result)
        except (DecimalException, ArithmeticError, OverflowError, ValueError):
            raise CellError("FORMULA_RANGE",
                            f"字段「{field['name']}」的计算结果无法按显示位数输出",
                            hint="结果数值过大或位数过多，请缩小公式的取值范围",
                            user_message=f"字段「{field['name']}」的计算结果无法按显示位数输出",
                            user_hint="结果数值过大或位数过多，请缩小公式的取值范围")

    def compute(self, context):
        """context: {"qty": int, "spu_id": int, "stored": {字段 uid: 编码值}}

        返回 (计算字段的显示数值 {字段名: 数值或 None}, calculations {字段名: {display, error}})。
        失败只记为该单元格错误，不影响同批其它 SKU 与其它字段。
        """
        with localcontext() as ctx:
            ctx.prec = PRECISION
            values, errors = {}, {}
            stored = context.get("stored") or {}
            for field in self.plain:
                raw = stored.get(field["uid"])
                if raw is None:
                    continue
                values[field["uid"]] = Decimal(raw).scaleb(-fieldtypes.logical_exp(
                    field["type"], field["scale"]))
            systems = {"qty": Decimal(context["qty"]), "spu_id": Decimal(context["spu_id"])}
            out_values, calculations = {}, {}
            for uid in self.order:
                field = self.by_uid[uid]
                try:
                    result = self._evaluate(self.parsed[uid], values, errors, systems, field)
                    shown, number = self._output(field, result)
                except CellError as exc:
                    errors[uid] = exc.payload
                    out_values[field["name"]] = None
                    calculations[field["name"]] = {"display": None, "error": exc.payload}
                    continue
                values[uid] = result          # 下游用内部高精度结果，不按显示位数取舍
                out_values[field["name"]] = number
                calculations[field["name"]] = {"display": shown, "error": None}
            return out_values, calculations


def _raise_upstream(field, source, payload):
    name = (source or {}).get("name") or "上游字段"
    raise CellError("FORMULA_UPSTREAM",
                    f"字段「{field['name']}」依赖的「{name}」计算失败",
                    hint=payload.get("message") or "",
                    user_message=f"字段「{field['name']}」依赖的「{name}」计算失败",
                    user_hint=payload.get("user_message") or payload.get("message") or "")
