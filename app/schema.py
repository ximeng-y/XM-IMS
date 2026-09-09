"""全部 DDL：索引库 + 店铺库模板

索引库存账号/店铺清单等全局轻量数据；业务数据全部在各店铺库内，物理隔离。
"""

INDEX_DDL = """
CREATE TABLE IF NOT EXISTS shops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  db_file TEXT NOT NULL,            -- 相对 DATA_DIR 的路径，如 'shops/1.db'
  created_at TEXT NOT NULL          -- ISO8601 本地时区带偏移
);
"""

SHOP_DDL = """
CREATE TABLE IF NOT EXISTS spus (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- items 即 SKU：真实的存储单元（自定义字段值、库存流水都挂在 SKU 上）
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  spu_id INTEGER NOT NULL REFERENCES spus(id),
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_spu ON items(spu_id);

-- 自定义字段注册表（每店独立）
CREATE TABLE IF NOT EXISTS custom_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  actor TEXT NOT NULL,
  action TEXT NOT NULL,      -- item_create/item_rename/item_spu_relink/field_value_change/
                              -- field_clear/field_create/field_rename/field_convert/field_delete/
                              -- spu_create/spu_rename
  entity_type TEXT NOT NULL,
  entity_id INTEGER,
  detail TEXT NOT NULL,      -- JSON，如 {"field":"售价","old":"12.34","new":"13.50"}
  created_at TEXT NOT NULL
);
"""
