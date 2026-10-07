"""自定义字段端点"""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from ...schemas import (FieldCreate, FieldConvert, FieldFormulaIn, FieldList, FieldOut,
                        FieldRename, FieldWriteOut)
from ...service import fields
from ..deps import require_shop

router = APIRouter(prefix="/fields", tags=["自定义字段"])
ShopDep = Annotated[int, Depends(require_shop)]


@router.post("", response_model=FieldWriteOut, summary="创建自定义字段（普通或数学计算）")
def create_field(shop_id: ShopDep, body: FieldCreate, response: Response):
    """创建自定义字段；与系统字段（ID/名称/库存数量/SPU ID/SPU 名称）重名返回 409。

    不带 formula 时按普通字段创建，保持原有行为：返回 201，字段可手填值。
    带 formula 时创建数学计算字段：type 为结果类型（decimal/percent）、scale 为显示位数，
    公式的语法、引用与循环引用一并校验；dry_run=true 只校验并预览（返回 200，不分配 ID）。
    """
    if body.dry_run:
        response.status_code = status.HTTP_200_OK
    else:
        response.status_code = status.HTTP_201_CREATED
    return fields.create_field(shop_id, body)


@router.get("", response_model=FieldList, summary="字段列表（5 个系统字段 + 自定义字段）")
def list_fields(shop_id: ShopDep):
    return fields.list_fields(shop_id)


@router.patch("/{field_id}", response_model=FieldOut, summary="字段改名（数据不动）")
def rename_field(shop_id: ShopDep, field_id: int, body: FieldRename):
    """改名不影响公式引用：公式按 sync_uid 引用字段，界面重新读取元数据后显示新名称。"""
    return fields.rename_field(shop_id, field_id, body.name)


@router.put("/{field_id}/formula", response_model=FieldWriteOut,
            summary="更新计算字段的公式与结果格式")
def update_formula(shop_id: ShopDep, field_id: int, body: FieldFormulaIn):
    """完整提交 formula/type/scale/allow_negative，整体替换该计算字段的定义。

    仅计算字段可用（普通字段返回 409 FORMULA_NOT_COMPUTED）；普通字段与计算字段不能互转。
    提交与现值完全相同的定义不会重复写审计。dry_run=true 只校验，不落库；统一返回 200。
    """
    return fields.update_formula(shop_id, field_id, body)


@router.post("/{field_id}/convert", response_model=None, summary="转换字段结构")
def convert_field(shop_id: ShopDep, field_id: int, body: FieldConvert) -> Any:
    """转换字段结构，返回转换报告：
    {description, lossy, affected_items,
     samples: [{item_id, item_name, old, new}]}，其中 item_id 为单品标识
    「SPU 号-组内序号」（如 1-2）。

    - string/bool 与其他类型互转需 confirm_clear=true（清空全部值）；
      dry_run=true 时只返回 {requires_clear, affected_items, samples} 作为预览，
      不要求 confirm_clear
    - 有损转换必须带 rounding（ceil/floor/round），缺失返回 400 ROUNDING_REQUIRED
    - dry_run=true 只返回报告不落库
    """
    return fields.convert_field(shop_id, field_id, body.new_type, body.new_scale,
                                body.new_allow_negative, body.rounding,
                                body.confirm_clear, body.dry_run)


@router.delete("/{field_id}", response_model=None, summary="删除字段")
def delete_field(shop_id: ShopDep, field_id: int,
                 confirm: bool = Query(default=False,
                                       description="必须显式传 true 才会执行删除")) -> Any:
    """删除自定义字段及其全部值（级联），返回 {deleted, field, deleted_values}"""
    return fields.delete_field(shop_id, field_id, confirm)
