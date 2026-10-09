"""Pydantic 请求/响应模型（OpenAPI 契约主体）

JSON 收发用显示语义：金额 unit_price 以「元」传（存储转分）；
decimal/percent 字段值以显示数值传（12.34；25 表示 25%）。

单品（SKU）标识 item_id 为字符串，格式「SPU 号-组内序号」（如 "1-2"），
即该 SKU 所属 SPU 的 ID 加它在组内自 1 起的序号。

数学计算字段：formula 是版本化的结构化 token 序列（见 app/formulas.py），
计算字段的读写只经公式端点，值不可通过 SKU 接口手填或清除。
"""
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field

ITEM_ID_DESC = "单品标识，格式「SPU 号-组内序号」，如 1-2"
FORMULA_DESC = "数学计算公式（结构化组件序列），仅计算字段使用"

# ---------- 公式组件 ----------


class FormulaNumber(BaseModel):
    kind: Literal["number"]
    value: str = Field(description="十进制常量文本（可为负），如 \"12\"、\"-3.5\"；不接受科学计数法")


class FormulaFieldRef(BaseModel):
    kind: Literal["field"]
    uid: str = Field(description="被引用字段的 sync_uid（仅数值型自定义字段与计算字段）")


class FormulaSystemRef(BaseModel):
    kind: Literal["system"]
    key: Literal["qty"] = Field(description="数值型系统字段：qty 库存数量（SPU ID 是编号，不可参与计算）")


class FormulaSymbol(BaseModel):
    kind: Literal["symbol"]
    value: Literal["+", "-", "*", "/", "(", ")"] = Field(description="运算符或括号")


FormulaToken = Annotated[Union[FormulaNumber, FormulaFieldRef, FormulaSystemRef, FormulaSymbol],
                         Field(discriminator="kind")]


class Formula(BaseModel):
    version: int = Field(default=1, description="公式结构版本，当前为 1")
    tokens: list[FormulaToken] = Field(description=FORMULA_DESC)


# ---------- 请求模型 ----------


class ShopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="店铺名，全局唯一")


class SpuCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="SPU（商品）名称")


class FieldCreate(BaseModel):
    name: str = Field(min_length=1, max_length=50, description="字段显示名，不得与系统字段重名")
    type: str = Field(description="string | bool | int | decimal | percent；带 formula 时为结果类型（int/decimal/percent）")
    allow_negative: bool = Field(default=False, description="是否允许负数，仅数值类（int/decimal/percent）有效")
    scale: Optional[int] = Field(default=None, ge=0, le=6,
                                 description="小数位数：decimal 字段 0~6（默认 2）；计算字段为显示位数"
                                             "（默认 2；结果类型为整数时只能为 0）")
    formula: Optional[Formula] = Field(default=None,
                                       description="提交即创建数学计算字段：type 为结果类型"
                                                   "（int/decimal/percent），整数结果按四舍五入取整")
    dry_run: bool = Field(default=False, description="只校验公式与引用，不落库")


class FieldFormulaIn(BaseModel):
    formula: Formula = Field(description="完整公式定义，整体替换")
    type: Optional[str] = Field(default=None,
                                description="结果类型：int | decimal | percent；缺省保持不变")
    scale: Optional[int] = Field(default=None, ge=0, le=6,
                                 description="显示小数位 0~6；缺省保持不变。"
                                             "结果类型为整数时只能省略或传 0")
    allow_negative: Optional[bool] = Field(default=None, description="是否允许负数；缺省保持不变")
    dry_run: bool = Field(default=False, description="只校验，不落库")


class FieldRename(BaseModel):
    name: str = Field(min_length=1, max_length=50)


class FieldConvert(BaseModel):
    new_type: Optional[str] = Field(default=None, description="目标类型；缺省保持不变")
    new_scale: Optional[int] = Field(default=None, ge=0, le=6,
                                     description="目标小数位，仅目标类型为 decimal 时有效")
    new_allow_negative: Optional[bool] = Field(default=None, description="目标是否允许负数；缺省保持不变")
    rounding: Optional[str] = Field(default=None,
                                    description="精度损失时的取整方式：ceil（向上）| floor（向下）| round（四舍五入）")
    confirm_clear: bool = Field(default=False,
                                description="string/bool 与其他类型互转时不可换算，需确认清空全部值")
    dry_run: bool = Field(default=False, description="只返回转换报告，不落库")


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="SKU 名称")
    spu_id: Optional[int] = Field(default=None, description="挂载的 SPU ID（与 spu_name 二选一必填）")
    spu_name: Optional[str] = Field(default=None,
                                    description="SPU 名称：精确匹配已有 SPU 则复用，否则自动创建")
    values: dict[str, Any] = Field(default_factory=dict,
                                   description='自定义字段值，按字段名传，如 {"售价": 12.34, "供应商": "XX贸易"}')
    qty: int = Field(default=0, ge=0,
                     description="初始库存数量；大于 0 时在同一事务内记一条 type=in 的入库流水"
                                 "（不带单价/批次/供应商），省略或为 0 则不记流水")


class ItemPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    spu_id: Optional[int] = Field(default=None, description="换挂的目标 SPU ID（与 spu_name 二选一）")
    spu_name: Optional[str] = Field(default=None, description="换挂的目标 SPU 名称（精确匹配复用，否则自动创建）")
    values: Optional[dict[str, Any]] = Field(default=None,
                                             description="待修改的字段值；某字段传 null 表示清除该值")


class InboundIn(BaseModel):
    item_id: str = Field(description=ITEM_ID_DESC)
    qty: int = Field(gt=0, description="入库数量（正整数）")
    unit_price: Optional[float] = Field(default=None, description="实际采购单价（元）")
    batch_no: Optional[str] = None
    supplier: Optional[str] = None
    note: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=100,
                                           description="幂等键：同 key 重复提交返回首次结果，不重复入库")
    dry_run: bool = Field(default=False, description="只校验并预览结果，不落库")


class OutboundIn(BaseModel):
    item_id: str = Field(description=ITEM_ID_DESC)
    qty: int = Field(gt=0, description="出库数量（正整数）")
    unit_price: Optional[float] = Field(default=None, description="实际成交单价（元）")
    channel: Optional[str] = None
    customer: Optional[str] = None
    note: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=100)
    dry_run: bool = Field(default=False)


class AdjustIn(BaseModel):
    item_id: str = Field(description=ITEM_ID_DESC)
    target_qty: int = Field(ge=0, description="盘点后的目标库存（非增量）")
    note: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=100)
    dry_run: bool = Field(default=False)


# ---------- 响应模型 ----------


class ShopOut(BaseModel):
    id: int
    name: str
    created_at: str


class ShopList(BaseModel):
    shops: list[ShopOut]


class SpuOut(BaseModel):
    id: int
    name: str
    sku_count: int
    created_at: str
    updated_at: str


class SpuList(BaseModel):
    spus: list[SpuOut]


class FieldOut(BaseModel):
    system: bool = Field(default=False, description="是否系统字段")
    key: Optional[str] = Field(default=None, description="系统字段标识（系统字段专用）")
    id: Optional[int] = Field(default=None, description="字段 ID（自定义字段专用；dry_run 预览为空）")
    uid: Optional[str] = Field(default=None, description="字段稳定标识 sync_uid（公式引用与同步用）")
    name: str
    type: str = Field(description="string | bool | int | decimal | percent；计算字段为结果类型")
    readonly: bool = Field(default=False, description="计算字段为 true：值由公式算出，不可手填或清除")
    allow_negative: bool = Field(default=False)
    scale: int = Field(default=2)
    formula: Optional[Formula] = Field(default=None, description="计算字段的公式定义；普通字段为 null")
    computed: bool = Field(default=False, description="是否数学计算字段")


class FieldWriteOut(FieldOut):
    dry_run: bool = Field(default=False, description="本次仅校验预览，未落库")


class FieldList(BaseModel):
    fields: list[FieldOut]


class CalculationOut(BaseModel):
    display: Optional[str] = Field(default=None,
                                   description="后端格式化的结果文本（保留显示位数与百分号）；失败为 null")
    error: Optional[dict[str, Any]] = Field(default=None,
                                            description="单元格错误 {code, message, hint?}；成功为 null")


class ItemOut(BaseModel):
    id: str = Field(description=ITEM_ID_DESC)
    name: str
    spu_id: int
    spu_name: str
    qty: int = Field(description="当前库存 = SUM(movements)")
    values: dict[str, Any] = Field(description="自定义字段值（显示语义）；计算字段成功时为结果数值，失败为 null")
    calculations: dict[str, CalculationOut] = Field(default_factory=dict,
                                                    description="计算字段的结果与错误，按字段名索引")
    created_at: str
    updated_at: str


class ItemCreatedOut(ItemOut):
    spu_auto_created: bool = Field(default=False, description="创建单品时是否自动新建了 SPU")


class ItemList(BaseModel):
    items: list[ItemOut]
    total: int


class MovementOut(BaseModel):
    id: Optional[int] = Field(default=None, description="dry_run 预览无 id")
    item_id: str = Field(description=ITEM_ID_DESC)
    type: str = Field(description="in | out | adjust")
    qty_delta: int = Field(description="带符号增量：入库 + / 出库 − / 盘点 目标−当前")
    unit_price: Optional[float] = Field(default=None, description="实际单价（元）")
    batch_no: Optional[str] = None
    supplier: Optional[str] = None
    channel: Optional[str] = None
    customer: Optional[str] = None
    note: Optional[str] = None
    target_qty: Optional[int] = Field(default=None, description="仅盘点调整：目标库存")
    actor: str
    created_at: str


class MovementList(BaseModel):
    movements: list[MovementOut]
    total: int


class MovementResult(BaseModel):
    movement: MovementOut
    current_qty: int = Field(description="当前库存；成功执行时即本次操作后的库存")
    new_qty: Optional[int] = Field(default=None, description="仅 dry_run 预览时给出")
    dry_run: bool = False
    idempotent_replay: bool = Field(default=False, description="幂等键命中，返回的是首次提交的结果")
