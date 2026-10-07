"""全部 DDL：索引库 + 店铺库模板

索引库存账号/店铺清单等全局轻量数据；业务数据全部在各店铺库内，物理隔离。
审计 audit_log.entity_id 是事件发生时的历史编号，不是当前对象的外键。
合并改号后按 entity_type + entity_uid 关联对应表的 sync_uid；对象已删除时不得回退匹配 entity_id。
entity_id 与 detail 原文保持不可变，本次编号变化另记于 sync_merge 审计。

店铺库结构版本：
- v3：无 formula_json 的自定义字段表
- v4：custom_fields 末尾追加可空 formula_json，承载数学计算字段的公式定义
  （NULL = 普通手填字段；非空 = 公式对象的规范 JSON）。
  仅计算字段使用 decimal/percent 结果类型，不向 type 的 CHECK 增加 formula 项。
  计算字段不落 custom_values 行。
SHOP_DDL_V3 保留为已知旧快照的校验依据，当前 SHOP_DDL 即 v4。
"""

INDEX_DDL = """
CREATE TABLE IF NOT EXISTS shops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_uid TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL UNIQUE,
  db_file TEXT NOT NULL,            -- 相对 DATA_DIR 的路径，如 'shops/1.db'
  created_at TEXT NOT NULL          -- ISO8601 本地时区带偏移
);
"""

SHOP_DDL_V3 = """
CREATE TABLE IF NOT EXISTS spus (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_uid TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- items 即 SKU：真实的存储单元（自定义字段值、库存流水都挂在 SKU 上）
-- 对外标识为「SPU 号-组内序号」（如 1-2）；seq 为该 SPU 内自 1 递增的序号，
-- 号与序号都不复用空位（只取 max+1），避免历史流水里的旧编号指向新 SKU
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_uid TEXT NOT NULL UNIQUE,
  spu_id INTEGER NOT NULL REFERENCES spus(id),
  seq INTEGER NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_spu ON items(spu_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_spu_seq ON items(spu_id, seq);

-- 自定义字段注册表（每店独立）
CREATE TABLE IF NOT EXISTS custom_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_uid TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL CHECK (type IN ('string','bool','int','decimal','percent')),
  allow_negative INTEGER NOT NULL DEFAULT 0,   -- 仅数值类可为 1
  scale INTEGER NOT NULL DEFAULT 2,            -- 仅 decimal 用，0~6
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- EAV 值表：值与字段定义的类型匹配由 service 层校验
CREATE TABLE IF NOT EXISTS custom_values (
  item_id INTEGER NOT NULL REFERENCES items(id),
  field_id INTEGER NOT NULL REFERENCES custom_fields(id) ON DELETE CASCADE,
  value_int INTEGER,        -- bool/int/decimal/percent 的编码存储
  value_text TEXT,          -- string 的存储
  PRIMARY KEY (item_id, field_id)
);
CREATE INDEX IF NOT EXISTS idx_values_field ON custom_values(field_id);

-- 不可变库存流水：库存 = SUM(qty)，禁止 UPDATE 数量
CREATE TABLE IF NOT EXISTS movements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_uid TEXT NOT NULL UNIQUE,
  item_id INTEGER NOT NULL REFERENCES items(id),
  type TEXT NOT NULL CHECK (type IN ('in','out','adjust')),
  qty INTEGER NOT NULL,      -- 带符号增量：in=+ / out=− / adjust=目标−当前
  unit_price INTEGER,        -- 实际成交单价（分），可空
  batch_no TEXT, supplier TEXT, channel TEXT, customer TEXT, note TEXT,
  idempotency_key TEXT UNIQUE,
  actor TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mov_item ON movements(item_id, created_at);

-- 主数据变更审计（movement 自带 actor，不在此重复记录）
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_uid TEXT NOT NULL UNIQUE,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,      -- item_create/item_rename/item_spu_relink/field_value_change/
                              -- field_clear/field_create/field_rename/field_convert/field_delete/
                              -- field_formula/spu_create/spu_rename
  entity_type TEXT NOT NULL,
  entity_id INTEGER,
  entity_uid TEXT,
  detail TEXT NOT NULL,      -- JSON，如 {"field":"售价","old":"12.34","new":"13.50"}
  created_at TEXT NOT NULL
);
"""

# 当前店铺库模板（v4）：与 v3 的唯一差别是 custom_fields 末尾的 formula_json
SHOP_DDL = SHOP_DDL_V3.replace(
    """  scale INTEGER NOT NULL DEFAULT 2,            -- 仅 decimal 用，0~6
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);""",
    """  scale INTEGER NOT NULL DEFAULT 2,            -- 仅 decimal 用，0~6；计算字段表示显示位数
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  formula_json TEXT           -- NULL = 普通手填字段；非空 = 计算字段的公式对象 JSON
);""")
