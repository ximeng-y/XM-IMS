"""字段类型领域核心：类型定义、编码/解码、转换矩阵与取整（纯函数，无 IO）

数值编码（禁用 REAL，存储全 INTEGER）：
    string  → value_text
    bool    → value_int 0/1
    int     → value_int 原值
    decimal → value_int = 逻辑值 × 10^scale
    percent → value_int = 逻辑值 × 10^4（数学语义：25% 的逻辑值是 0.25，存储 2500）

「逻辑值」= 数值的数学本值；「显示值」= JSON 收发所用：
    decimal 显示值 = 逻辑值；percent 显示值 = 逻辑值 × 100（25% 收发用 25）
类型转换保持逻辑值不变：存储值 × 10^(k2-k1)，k 为各类型的逻辑值指数。
"""
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

TYPES = ("string", "bool", "int", "decimal", "percent")
NUMERIC_TYPES = ("int", "decimal", "percent")
ROUNDINGS = ("ceil", "floor", "round")
# 类型 → 界面上使用的中文名称（面向用户文案统一取此处，避免界面出现 string/int 等标识）
TYPE_LABELS = {"string": "文本", "bool": "开关", "int": "整数",
               "decimal": "小数", "percent": "百分比"}
# 取整方式 → 中文名称
ROUNDING_LABELS = {"ceil": "向上取整", "floor": "向下取整", "round": "四舍五入"}

_ROUNDING_MAP = {"ceil": ROUND_CEILING, "floor": ROUND_FLOOR, "round": ROUND_HALF_UP}


def logical_exp(field_type: str, scale: int) -> int:
    """逻辑值指数：逻辑值 = 存储值 / 10^k"""
    if field_type == "decimal":
        return scale
    if field_type == "percent":
        return 4
    return 0  # int


def display_divisor(field_type: str, scale: int) -> int:
    """显示值 = 存储值 / divisor"""
    if field_type == "decimal":
        return 10 ** scale
    if field_type == "percent":
        return 100
    return 1  # int / bool


def validate_and_encode(field_type: str, scale: int, allow_negative: bool, value) -> tuple:
    """JSON 输入（显示语义）→ 存储编码。返回 (encoded, display_echo)。

    encoded 对 string 为 None（值走 value_text）；输入超字段精度按四舍五入收敛，
    返回的 display_echo 即实际存储值对应的显示值（回显给调用方）。
    校验失败抛 ValueError（中文消息，由上层转为 ApiError）。
    """
    if field_type == "string":
        if not isinstance(value, str):
            raise ValueError("需要文本值")
        return None, value
    if field_type == "bool":
        if not isinstance(value, bool):
            raise ValueError("需要 true/false")
        return (1 if value else 0), value

    # 数值类：bool 是 int 的子类，需显式排除
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"需要数值（当前收到 {type(value).__name__}）")
    d = Decimal(str(value))
    if field_type == "int":
        if d != d.to_integral_value():
            raise ValueError("需要整数值")
        encoded = int(d)
    elif field_type == "decimal":
        q = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
        encoded = int(q.scaleb(scale))
    else:  # percent：显示值最多 2 位小数（存储 = 显示 × 100）
        q = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        encoded = int(q.scaleb(2))
    if encoded < 0 and not allow_negative:
        raise ValueError("该字段不允许负数")
    return encoded, decode(field_type, scale, encoded)


def decode(field_type: str, scale: int, stored) -> object:
    """存储值 → 显示值（string 传 value_text，其余传 value_int）"""
    if field_type == "string":
        return stored
    if field_type == "bool":
        return bool(stored)
    if field_type == "int":
        return stored
    return float(Decimal(stored) / display_divisor(field_type, scale))


def conversion_kind(t1: str, s1: int, t2: str, s2: int):
    """判定转换类别：
    - "no_change"：值无需换算（可能仅 allow_negative 变化，由调用方处理）
    - "clear"：string/bool 与其他类型互转，不可换算，需确认后清空全部值
    - ("numeric", k1, k2)：数值换算，存储值 ×10^(k2-k1) 保持逻辑值不变
    """
    if (t1, s1) == (t2, s2):
        return "no_change"
    if t1 != t2 and (t1 in ("string", "bool") or t2 in ("string", "bool")):
        return "clear"
    return ("numeric", logical_exp(t1, s1), logical_exp(t2, s2))


def convert_encoded(n: int, k1: int, k2: int, rounding: str | None = None) -> tuple:
    """单个存储值按「逻辑值不变」换算。返回 (new_n, lossless)。

    k2 >= k1 时恒无损（乘法）；否则做除法，余数 ≠ 0 即有损：
    有损且未提供 rounding 时返回 (None, False)，由调用方决定报错或预览。
    """
    if k2 >= k1:
        return n * (10 ** (k2 - k1)), True
    div = 10 ** (k1 - k2)
    q, r = divmod(n, div)
    if r == 0:
        return q, True
    if rounding is None:
        return None, False
    d = (Decimal(n) / div).quantize(Decimal(1), rounding=_ROUNDING_MAP[rounding])
    return int(d), False
