"""数学计算字段：公式解析、依赖校验与逐对象求值（纯领域逻辑，不访问数据库）

公式是版本化的结构化 token 序列，不保存可执行的 Python / JavaScript / SQL 表达式，
也不使用 eval。四类基础 token：
    {"kind": "number", "value": "12.5"}      数字常量（十进制文本，可为负）
    {"kind": "field",  "uid": "<sync_uid>"}  本店自定义字段或计算字段
    {"kind": "system", "key": "qty"}         数值型系统字段（显式白名单）
    {"kind": "symbol", "value": "*"}         运算符或括号
SPU 公式还支持一个聚合 token：
    {"kind": "aggregate", "op": "sum", "tokens": [...]}
聚合内部只允许基础 token，对每个子 SKU 求值后相加。

数值语义：普通字段的编码值先按 fieldtypes.logical_exp 还原成数学值（百分比 25% → 0.25），
全部运算用 Decimal、局部精度 50 位；中间结果不按显示位数取舍，只有输出当前字段时才
按 ROUND_HALF_UP 收敛。整数字段在输出边界量化为零位小数，该取整结果同时用于显示、
JSON 数值与下游引用；小数/百分比字段供下游引用的仍是未按显示位数取舍的结果。
本模块不导入 service 包，避免 services 之间产生循环导入。
"""
import json
import re
from collections import defaultdict
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext

from . import fieldtypes
from ..errors import ApiError

# 普通公式仍为 v1；只有包含聚合的公式使用 v2
BASE_VERSION = 1
AGGREGATE_VERSION = 2
VERSION = AGGREGATE_VERSION
# 局部计算精度（有效数字）
PRECISION = 50
# 显示精度统一 0~6 位（默认 2 位由调用方给出）
MAX_SCALE = 6
# 十进制常量：普通整数/小数，允许前导零与负号；不接受布尔、NaN、Infinity、科学计数法
NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

# 可引用的系统字段白名单：库内 ID 恰为整数不代表它可参与数学运算。
# 内层聚合只允许 SKU 的 qty；SPU 外层还可引用合计库存与 SKU 数。
SYSTEM_KEYS_BY_TARGET = {
    "sku": {"qty": "库存数量"},
    "spu": {"total_qty": "合计库存", "sku_count": "SKU 数"},
}
AGGREGATE_SYSTEM_KEYS = {"qty": "库存数量"}
SYSTEM_KEYS = {**SYSTEM_KEYS_BY_TARGET["sku"], **SYSTEM_KEYS_BY_TARGET["spu"]}
# 计算字段的结果类型（写入 custom_fields.type 的值）：整数按四舍五入取整，供下游引用取整后的值
RESULT_TYPES = ("int", "decimal", "percent")

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


def _one_token(token, allow_aggregate=True) -> dict:
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
    if kind == "aggregate":
        if not allow_aggregate:
            raise _invalid("对子 SKU 求和不能再次嵌套",
                           hint="SUM 内部只允许字段、数字、运算符与括号",
                           user_message="对子 SKU 求和中不能再放一个求和",
                           user_hint="请先完成当前求和表达式，或把内部内容改成字段、数字与运算符")
        if item.get("op") != "sum":
            raise _invalid(f"不支持的聚合运算：{item.get('op')}",
                           hint="当前只支持 sum（对子 SKU 表达式求和）",
                           user_message="当前只支持对子 SKU 求和",
                           user_hint="请使用「对子 SKU 求和」入口")
        raw_tokens = item.get("tokens")
        if not isinstance(raw_tokens, list):
            raise _invalid("对子 SKU 求和的内部表达式格式无效",
                           user_message="对子 SKU 求和的内容无法识别",
                           user_hint="请删除这一项后重新添加求和")
        child = parse(raw_tokens, allow_aggregate=False)
        return {"kind": "aggregate", "op": "sum", "tokens": child.tokens}
    raise _invalid(f"无效的公式组件类型：{kind}",
                   hint="可用组件类型：number / field / system / symbol / aggregate",
                   user_message="公式里有无法识别的内容",
                   user_hint="请清空公式后重新从下方按钮逐项加入")


def version_of(tokens) -> int:
    """公式内容对应的版本：含聚合为 v2，否则继续使用 v1。"""
    return AGGREGATE_VERSION if any(
        isinstance(t, dict) and t.get("kind") == "aggregate" for t in (tokens or [])
    ) else BASE_VERSION


# ---------- 解析 ----------

class Parsed:
    """一次解析的结果：规范化 token + RPN + 外层/聚合内层引用集合"""

    __slots__ = ("tokens", "rpn", "outer_fields", "aggregate_fields", "fields",
                 "outer_systems", "aggregate_systems", "systems", "has_aggregate")

    def __init__(self, tokens, rpn):
        self.tokens = tokens
        self.rpn = rpn
        self.outer_fields = {t["uid"] for t in tokens if t["kind"] == "field"}
        self.outer_systems = {t["key"] for t in tokens if t["kind"] == "system"}
        aggregates = [t for t in tokens if t["kind"] == "aggregate"]
        self.has_aggregate = bool(aggregates)
        self.aggregate_fields = {
            child["uid"] for item in aggregates for child in item["tokens"]
            if child["kind"] == "field"
        }
        self.aggregate_systems = {
            child["key"] for item in aggregates for child in item["tokens"]
            if child["kind"] == "system"
        }
        self.fields = self.outer_fields | self.aggregate_fields
        self.systems = self.outer_systems | self.aggregate_systems


def _static_check(rpn) -> None:
    """对纯常数子表达式求值，拒绝可直接确定的除零（如 A/0、A/(2-2)）"""
    stack = []
    for item in rpn:
        if item[0] == "agg":
            stack.append(None)
            continue
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


def parse(tokens, allow_aggregate=True) -> Parsed:
    """校验并解析公式 token 序列；返回供校验与求值使用的内部表示。

    语法：乘除同级、加减同级、同级从左向右；负号只允许出现在公式开头及左括号之后
    （负常量本身是一个操作数，如 A × [-3] 合法）；不支持隐式乘法。使用显式栈
    （调度场算法）而非递归下降，任意嵌套深度都不会触发递归限制。
    """
    items = [_one_token(t, allow_aggregate=allow_aggregate) for t in (tokens or [])]
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
            if item["kind"] == "aggregate":
                output.append(("agg", parse(item["tokens"], allow_aggregate=False)))
            else:
                key = item["value"] if item["kind"] == "number" else (
                    item["uid"] if item["kind"] == "field" else item["key"])
                output.append(({"number": "num", "field": "field", "system": "sys"}[item["kind"]],
                               key))
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


def dump(tokens, version=None) -> str:
    """规范 JSON：版本由内容决定，固定键顺序与分隔符，避免同步差异。"""
    items = [_one_token(t) for t in (tokens or [])]
    actual = version_of(items)
    if version is not None and int(version) != actual:
        raise _invalid(f"公式版本与内容不匹配：版本 {version}，内容应为版本 {actual}",
                       hint="不含聚合的公式使用版本 1；包含「对子 SKU 求和」的公式使用版本 2",
                       user_message="公式版本与当前内容不一致",
                       user_hint="请关闭窗口后重新打开公式编辑器再保存")
    return json.dumps({"version": actual, "tokens": items},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load(text) -> dict:
    """读取落库的公式 JSON；失败视为数据损坏，返回结构化错误"""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise ApiError("FORMULA_INVALID", "公式定义不是合法 JSON", status=409)
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(data, dict) or version not in (BASE_VERSION, AGGREGATE_VERSION):
        raise ApiError("FORMULA_INVALID",
                       f"公式定义版本不受支持：{version if version is not None else '?'}",
                       hint="当前支持的公式版本为 1 和 2，请升级应用后重试", status=409,
                       user_message="该公式由更高版本的程序保存，当前版本无法识别",
                       user_hint="请升级程序后重新打开该字段的公式")
    parsed = parse(data.get("tokens") or [])
    actual = version_of(parsed.tokens)
    if actual != version:
        raise ApiError("FORMULA_INVALID",
                       f"公式版本与内容不匹配：版本 {version}，内容应为版本 {actual}",
                       status=409,
                       user_message="公式的版本信息与内容不一致",
                       user_hint="请回到字段页重新打开该公式并保存")
    return {"version": actual, "tokens": parsed.tokens}


def tokens_of(text) -> list | None:
    """落库文本 → token 列表；NULL/空文本返回 None"""
    if not text:
        return None
    return load(text)["tokens"]


# ---------- 引用、依赖图与循环检测 ----------

def _check_one_ref(uid, by_uid, target):
    target_row = by_uid.get(uid)
    if target_row is None:
        raise ApiError("FORMULA_REF_INVALID",
                       f"公式引用的字段不存在（标识 {uid[:8]}）",
                       hint="该字段可能已被删除；请重新从「可引用字段」区域选择", status=400,
                       user_message="公式引用的字段不存在",
                       user_hint="该字段可能已被删除；请重新从「可引用字段」区域选择")
    if target_row.get("target", "sku") != target:
        target_name = "SPU" if target == "spu" else "单品"
        raise ApiError("FORMULA_REF_INVALID",
                       f"字段「{target_row['name']}」不属于 {target_name} 属性，不能在此公式引用",
                       hint=f"外层公式只能引用{target_name}属性；对子 SKU 求和请在求和表达式内选择单品属性",
                       status=400,
                       user_message=f"字段「{target_row['name']}」不能在这里引用",
                       user_hint=f"外层公式只能引用{target_name}属性；单品属性可放进「对子 SKU 求和」")
    if target_row["type"] not in fieldtypes.NUMERIC_TYPES:
        raise ApiError("FORMULA_REF_INVALID",
                       f"字段「{target_row['name']}」是{TYPE_NAMES.get(target_row['type'], target_row['type'])}类型，"
                       "不能参与数学计算",
                       hint="只有整数、小数、百分比字段以及计算字段可以引用", status=400,
                       user_message=f"字段「{target_row['name']}」是"
                                    f"{TYPE_NAMES.get(target_row['type'], target_row['type'])}类型，"
                                    "不能参与数学计算",
                       user_hint="只有整数、小数、百分比字段以及计算字段可以引用")


def check_refs(parsed: Parsed, by_uid: dict, target: str = "sku") -> None:
    """按公式作用域校验引用：外层归属目标，SUM 内层固定为 SKU。"""
    if target not in SYSTEM_KEYS_BY_TARGET:
        raise _invalid(f"未知的公式作用域：{target}",
                       user_message="公式作用域无法识别",
                       user_hint="请关闭窗口后重新打开公式编辑器")
    allowed_systems = SYSTEM_KEYS_BY_TARGET[target]
    invalid_systems = sorted(parsed.outer_systems - set(allowed_systems))
    if invalid_systems:
        key = invalid_systems[0]
        raise _invalid(f"当前公式不能引用系统字段：{key}",
                       hint="可引用的系统字段只有：" +
                            "、".join(f"{v}（{k}）" for k, v in allowed_systems.items()),
                       user_message=f"不能引用系统字段：{key}",
                       user_hint="可引用的系统字段只有：" + "、".join(allowed_systems.values()))
    for uid in sorted(parsed.outer_fields):
        _check_one_ref(uid, by_uid, target)
    if parsed.has_aggregate:
        if target != "spu":
            raise _invalid("普通单品公式不能包含「对子 SKU 求和」",
                           user_message="对子 SKU 求和只能用于 SPU 属性",
                           user_hint="请在 SPU 属性的公式中新增求和，或删除这一项")
        invalid_inner = sorted(parsed.aggregate_systems - set(AGGREGATE_SYSTEM_KEYS))
        if invalid_inner:
            key = invalid_inner[0]
            raise _invalid(f"对子 SKU 求和不能引用系统字段：{key}",
                           hint="求和内部只允许单品库存数量（qty）",
                           user_message=f"对子 SKU 求和不能引用系统字段：{key}",
                           user_hint="求和内部只允许引用单品库存数量")
        for uid in sorted(parsed.aggregate_fields):
            _check_one_ref(uid, by_uid, "sku")


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
    for uid, item in parsed.items():
        row = by_uid.get(uid) or {}
        check_refs(item, by_uid, row.get("target", "sku"))
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


def _normalize(field: dict, value: Decimal) -> Decimal:
    """供下游引用与显示共用的规范值：整数字段量化为零位小数（四舍五入，-0 归一为 0），
    小数/百分比字段保持原结果——引用精度不被本字段的显示位数影响。"""
    if field["type"] != "int":
        return value
    rounded = _quantize(value, 0)
    return Decimal(0) if rounded.is_zero() else rounded


def display_text(field: dict, value: Decimal) -> str:
    """当前字段的输出文本：小数按显示位数取值，整数字段用取整结果，百分比乘以 100 并带 % 号"""
    if field["type"] == "int":
        return format(value, "f")
    shown = _quantize(value * 100 if field["type"] == "percent" else value, field["scale"])
    return format(shown, "f") + ("%" if field["type"] == "percent" else "")


def display_number(field: dict, value: Decimal) -> int | float:
    """values 里回给客户端的显示数值（与手填数值同一语义：百分比 25 表示 25%）；
    整数字段返回 Python int（JSON 中为整数）。"""
    if field["type"] == "int":
        return int(value)
    number = value * 100 if field["type"] == "percent" else value
    return float(_quantize(number, field["scale"]))


class Engine:
    """一批查询内共用：公式只解析一次，按拓扑序求值。

    SKU 引擎按单品上下文求值；SPU 引擎通过 sku_engine 对每个子 SKU 计算 SUM
    内部表达式。聚合内部只读取 SKU 引擎的 Decimal 规范值，不经过 JSON 或显示文本。
    """

    def __init__(self, defs, target="sku", sku_engine=None):
        self.fields = list(defs)
        self.target = target
        self.sku_engine = sku_engine
        self.by_uid = {f["uid"]: f for f in self.fields}
        self.plain = [f for f in self.fields
                      if not f["formula"] and f["type"] in fieldtypes.NUMERIC_TYPES]
        self.computed = [f for f in self.fields if f["formula"]]
        self.parsed = {f["uid"]: parse(f["formula"]) for f in self.computed}
        self.order = topological([f["uid"] for f in self.computed], graph_of(self.parsed),
                                 {f["uid"]: f["name"] for f in self.fields})

    def _reference(self, kind, key, values, errors, systems, field, definitions=None):
        if kind == "sys":
            return systems[key]
        definitions = definitions or self.by_uid
        if key in errors:
            return _raise_upstream(field, definitions.get(key), errors[key])
        if key not in values:
            name = (definitions.get(key) or {}).get("name") or key[:8]
            where = "SPU" if self.target == "spu" else "单品"
            raise CellError("FORMULA_SOURCE_MISSING",
                            f"字段「{field['name']}」引用的「{name}」没有值",
                            hint=f"请先为该{where}填写「{name}」，或调整公式",
                            user_message=f"字段「{field['name']}」引用的「{name}」没有值",
                            user_hint=f"请先为该{where}填写「{name}」，或调整公式")
        return values[key]

    def _sum(self, child, field, skus):
        if self.target != "spu" or self.sku_engine is None:
            raise CellError("FORMULA_SUM_SCOPE",
                            f"字段「{field['name']}」的对子 SKU 求和不能在此作用域计算",
                            user_message=f"字段「{field['name']}」的求和作用域不正确",
                            user_hint="对子 SKU 求和只能用于 SPU 属性")
        total = Decimal(0)
        for sku in skus or []:
            ref = sku.get("ref") or str(sku.get("id", "")) or "未知单品"
            try:
                raw_values, raw_errors = self.sku_engine.compute_raw(sku)
                term = self.sku_engine._evaluate(
                    child, raw_values, raw_errors, {"qty": Decimal(sku.get("qty", 0))},
                    field, definitions=self.sku_engine.by_uid, check_negative=False)
            except CellError as exc:
                payload = exc.payload
                raise CellError(
                    "FORMULA_SUM_SOURCE",
                    f"字段「{field['name']}」汇总子单品「{ref}」时失败",
                    hint=payload.get("message") or "",
                    user_message=f"字段「{field['name']}」汇总子单品「{ref}」时失败",
                    user_hint=payload.get("user_hint") or payload.get("hint") or "")
            total += term
        return total

    def _evaluate(self, parsed, values, errors, systems, field,
                  definitions=None, check_negative=True, skus=None):
        stack = []
        try:
            for item in parsed.rpn:
                if item[0] == "num":
                    stack.append(Decimal(item[1]))
                    continue
                if item[0] == "agg":
                    stack.append(self._sum(item[1], field, skus))
                    continue
                if item[0] != "op":
                    stack.append(self._reference(
                        item[0], item[1], values, errors, systems, field, definitions))
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
        if check_negative and result < 0 and not field["allow_negative"]:
            raise CellError("FORMULA_NEGATIVE",
                            f"字段「{field['name']}」的计算结果为负数，但该字段不允许负数",
                            hint="如确需负值，请编辑该字段并勾选「允许负数」；否则请调整公式",
                            user_message=f"字段「{field['name']}」的计算结果为负数，但该字段不允许负数",
                            user_hint="如确需负值，请编辑该字段并勾选「允许负数」；否则请调整公式")
        return result

    def _calculate(self, context):
        """返回内部 Decimal 值 {(uid): Decimal} 与错误，供聚合复用。"""
        values, errors = {}, {}
        stored = context.get("stored") or {}
        for field in self.plain:
            raw = stored.get(field["uid"])
            if raw is None:
                continue
            values[field["uid"]] = Decimal(raw).scaleb(-fieldtypes.logical_exp(
                field["type"], field["scale"]))
        if self.target == "spu":
            systems = {"total_qty": Decimal(context.get("total_qty", 0)),
                       "sku_count": Decimal(context.get("sku_count", 0))}
        else:
            systems = {"qty": Decimal(context.get("qty", 0))}
        skus = context.get("skus") or []
        for uid in self.order:
            field = self.by_uid[uid]
            try:
                result = self._evaluate(
                    self.parsed[uid], values, errors, systems, field, skus=skus)
                values[uid] = _normalize(field, result)
            except CellError as exc:
                errors[uid] = exc.payload
        return values, errors

    def compute_raw(self, context):
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return self._calculate(context)

    def _output(self, field, result):
        """结果 → (供下游引用与显示共用的规范值, 显示文本, 显示数值)。
        量化与浮点转换都在这层出错边界内：两者都可能抛出 DecimalException
        （如结果位数超出上下文精度），若不拦住，一个字段就能让整个 SKU 列表/详情查询失败。"""
        try:
            normalized = _normalize(field, result)
            return normalized, display_text(field, normalized), display_number(field, normalized)
        except (DecimalException, ArithmeticError, OverflowError, ValueError):
            raise CellError("FORMULA_RANGE",
                            f"字段「{field['name']}」的计算结果无法按显示位数输出",
                            hint="结果数值过大或位数过多，请缩小公式的取值范围",
                            user_message=f"字段「{field['name']}」的计算结果无法按显示位数输出",
                            user_hint="结果数值过大或位数过多，请缩小公式的取值范围")

    def compute(self, context):
        """context: {"qty": int, "stored": {字段 uid: 编码值}}；
        SPU 另含 {"total_qty", "sku_count", "skus": [{"qty","stored","ref"}]}。

        返回 (计算字段的显示数值 {字段名: 数值或 None}, calculations {字段名: {display, error}})。
        失败只记为该单元格错误，不影响同批其它 SKU 与其它字段。
        """
        with localcontext() as ctx:
            ctx.prec = PRECISION
            values, errors = self._calculate(context)
            out_values, calculations = {}, {}
            for uid in self.order:
                field = self.by_uid[uid]
                try:
                    if uid in errors:
                        raise CellError(**errors[uid])
                    value = values[uid]
                    _, shown, number = self._output(field, value)
                except CellError as exc:
                    errors[uid] = exc.payload
                    out_values[field["name"]] = None
                    calculations[field["name"]] = {"display": None, "error": exc.payload}
                    continue
                # 下游用规范值：整数已取整，小数/百分比保留内部高精度结果
                values[uid] = value
                out_values[field["name"]] = number
                calculations[field["name"]] = {"display": shown, "error": None}
            return out_values, calculations


def _raise_upstream(field, source, payload):
    """下游字段转述上游失败。

    user_hint 取上游的「提示」而非「描述」：上游的 user_message 是问题描述（如
    「字段 A 的计算结果为负数…」），user_hint 才是可操作的那一步（如「请勾选允许负数」）。
    取错会让下游单元格只显示问题、丢失修复建议。
    """
    name = (source or {}).get("name") or "上游字段"
    raise CellError("FORMULA_UPSTREAM",
                    f"字段「{field['name']}」依赖的「{name}」计算失败",
                    hint=payload.get("message") or "",
                    user_message=f"字段「{field['name']}」依赖的「{name}」计算失败",
                    user_hint=payload.get("user_hint") or payload.get("hint") or "")
