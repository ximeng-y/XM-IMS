"""SKU 对外标识：格式「SPU 号-组内序号」（如 1-2）的拼装与解析

对外标识由 items.spu_id 与 items.seq 实时拼出，内部仍以 items.id（整数）作为
流水与字段值的外键。解析只使用调用方连接，不开库、不提交事务。
"""
import re

from ..errors import ApiError

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
                            "GET /api/v1/items 查看全部单品及其标识", status=400,
                       user_message=f"无效的单品编号：{ref}",
                       user_hint="单品编号形如 1-2；请在单品下拉框中选择")
    spu_id, seq = int(m.group(1)), int(m.group(2))
    if spu_id > REF_PART_MAX or seq > REF_PART_MAX:
        raise ApiError("ITEM_REF_INVALID", f"单品标识超出取值范围：{ref}",
                       hint=f"SPU 号与组内序号都不得超过 {REF_PART_MAX}；"
                            "GET /api/v1/items 查看全部单品及其标识", status=400,
                       user_message=f"单品编号超出取值范围：{ref}",
                       user_hint="请在单品下拉框中选择")
    row = conn.execute("SELECT id FROM items WHERE spu_id = ? AND seq = ?",
                       (spu_id, seq)).fetchone()
    if row is None:
        raise ApiError("ITEM_NOT_FOUND", f"单品 {spu_id}-{seq} 不存在",
                       hint=existing_hint(conn), status=404,
                       user_message=f"单品 {spu_id}-{seq} 不存在",
                       user_hint=existing_user_hint(conn))
    return row["id"]


def existing_hint(conn) -> str:
    rows = conn.execute(
        "SELECT spu_id, seq, name FROM items ORDER BY spu_id, seq LIMIT 3").fetchall()
    if not rows:
        return "该店铺还没有单品，POST /api/v1/items 创建"
    return "现有单品：" + "、".join(
        f"{format_item_ref(r['spu_id'], r['seq'])}（{r['name']}）" for r in rows)


def existing_user_hint(conn) -> str:
    """界面用提示：只给可识别的单品编号与名称，不提创建端点"""
    rows = conn.execute(
        "SELECT spu_id, seq, name FROM items ORDER BY spu_id, seq LIMIT 3").fetchall()
    if not rows:
        return "该店铺还没有单品，请先新建单品"
    return "现有单品：" + "、".join(
        f"{format_item_ref(r['spu_id'], r['seq'])}（{r['name']}）" for r in rows)
