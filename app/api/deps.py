"""API 依赖：X-Shop-Id 请求头解析与店铺存在性校验"""
from fastapi import Header

from .. import db
from ..errors import ApiError


def require_shop(x_shop_id: str | None = Header(default=None, alias="X-Shop-Id")) -> int:
    """业务端点统一依赖：从 X-Shop-Id 头取店铺并校验"""
    if x_shop_id is None:
        raise ApiError("SHOP_HEADER_MISSING", "缺少 X-Shop-Id 请求头",
                       hint="业务端点需要 X-Shop-Id 头指定店铺；GET /api/v1/shops 查看店铺列表",
                       status=400)
    try:
        shop_id = int(x_shop_id)
    except ValueError:
        raise ApiError("SHOP_NOT_FOUND", f"无效的 X-Shop-Id：{x_shop_id}",
                       hint="应为店铺的数字 ID", status=404)
    if db.get_shop_row(shop_id) is None:
        raise ApiError("SHOP_NOT_FOUND", f"店铺 {shop_id} 不存在",
                       hint="GET /api/v1/shops 查看店铺列表", status=404)
    return shop_id
