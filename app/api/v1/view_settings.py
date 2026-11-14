"""页面列设置端点：读取与保存当前店铺、当前页面的列顺序、隐藏列与侧栏宽度

设置只保存在本机文件中，不写业务数据库、审计或库存流水，也不参与同步快照。
view 只支持三个带列设置的页面；请求与响应使用同一组字段。
"""
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ...service import view_settings
from ..access import view_settings_request
from ..deps import require_shop


def local_view_settings_request(request: Request):
    """在直连后端和前端代理边界使用同一访问规则。"""
    view_settings_request(request)


router = APIRouter(prefix="/view-settings", tags=["页面设置"],
                   dependencies=[Depends(local_view_settings_request)])


class ViewSettings(BaseModel):
    order: list[str] = Field(default_factory=list,
                             description="完整列顺序，包含已隐藏列和可排序的交互控件")
    hidden: list[str] = Field(default_factory=list, description="已隐藏的信息列标识")
    sidebar_width: int | None = Field(default=None, ge=1,
                                      description="列设置侧栏宽度（像素）；null 表示使用默认宽度")


@router.get("/{view}", summary="读取页面列设置", response_model=ViewSettings)
def get_view_settings(view: Literal["spus", "items", "inventory"],
                      shop_id: int = Depends(require_shop)):
    """无记录时 order、hidden 为空，sidebar_width 为 null，页面据此使用默认列顺序与默认宽度。"""
    return view_settings.load(shop_id, view)


@router.put("/{view}", summary="保存页面列设置", response_model=ViewSettings)
def put_view_settings(view: Literal["spus", "items", "inventory"], body: ViewSettings,
                      shop_id: int = Depends(require_shop)):
    """完整替换当前店铺、当前页面的记录；其他店铺与页面不受影响。"""
    return view_settings.save(shop_id, view, body.model_dump())
