from contextlib import closing
"""确定性逻辑快照；流式导出/重建，远端 SQL 仅接受本版本定义。"""
import base64
import hashlib
import re
import sqlite3
from pathlib import Path
from .. import formulas, schema
from ..errors import ApiError
from ..migrate import migrate_connection
from .common import atomic_json, dump, read_json, fail

FORMAT = 1
# 当前导出版本：索引库 1、店铺库 4（custom_fields 带 formula_json）
VERSIONS = {"index": 1, "shop": 4}
# 可读取的旧店铺库版本：v3 快照按已验证的旧结构重建后再升到 v4
READABLE_VERSIONS = {"index": (1,), "shop": (3, 4)}


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def encode(value):
    if isinstance(value, float):
        fail("SNAPSHOT_REAL", "快照包含本项目禁止的 REAL 数据",
             user_message="同步数据中包含不受支持的数值格式",
             user_hint="这通常是数据异常导致的，请回到同步页面重试；若持续出现，请反馈该问题")
    return {"$blob": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value


def decode(value):
    if isinstance(value, dict) and set(value) == {"$blob"}:
        return base64.b64decode(value["$blob"], validate=True)
    if value is not None and type(value) not in (int, str):
        fail("SNAPSHOT_VALUE", "快照字段类型不受支持",
             user_message="同步数据中包含无法识别的字段内容",
             user_hint="请回到同步页面重试；若持续出现，说明两侧数据格式不一致")
    return value


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def catalog(conn):
    return [dict(zip(("type", "name", "tbl_name", "sql"), row)) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name")]


def _template(script, alter=False):
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(script)
        if alter:
            conn.execute("ALTER TABLE custom_fields ADD COLUMN formula_json TEXT")
        return catalog(conn)


def expected(kind):
    """当前版本的标准 catalog"""
    return _template(schema.INDEX_DDL if kind == "index" else schema.SHOP_DDL)


def _known_catalogs(kind):
    """允许出现的 catalog 白名单：标准新建的 v4、已知 v3、以及 v3 执行本次 ALTER 的结果

    SQLite 会把 ADD COLUMN 后的建表语句按原文本重新保存，所以「v3 + ALTER」与
    「直接建 v4」的 SQL 文本不同。两者都是合法结构，不能只认其中一种，也不能
    放宽成忽略 CHECK、外键等细节的松散比较。
    """
    if kind == "index":
        return [_template(schema.INDEX_DDL)]
    return [_template(schema.SHOP_DDL), _template(schema.SHOP_DDL_V3),
            _template(schema.SHOP_DDL_V3, alter=True)]


def strip_comments(sql):
    """去掉 DDL 里的 `--` 行注释，保留字符串字面量

    SQLite 把建表语句原文抄进库文件，注释也在其中且之后无法改写。同一结构在不同
    版本里只要改过注释，库里抄下的原文就对不上当前模板，会被误判为结构不兼容。
    因此比对前先剥掉注释，只比较真实结构。

    逐字符扫描而非正则替换：字符串字面量里的 `--` 不是注释，不能用 `--.*$` 一刀切。
    """
    result, index, quoted = [], 0, None
    while index < len(sql):
        char = sql[index]
        if quoted:
            result.append(char)
            if char == quoted:
                quoted = None
            index += 1
        elif char in ("'", '"'):
            quoted = char
            result.append(char)
            index += 1
        elif char == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            index = len(sql) if end < 0 else end
        else:
            result.append(char)
            index += 1
    return "".join(result)


def normalized(objects):
    # 注释不参与比较，结构细节（列、类型、约束、索引、外键）仍逐字比对
    return [{**o, "sql": re.sub(r"\s+", " ", strip_comments(o["sql"])).strip()} for o in objects]



def check_schema(objects, kind):
    if normalized(objects) not in [normalized(c) for c in _known_catalogs(kind)]:
        fail("SCHEMA_UNSUPPORTED", "快照结构与当前应用不兼容，请使用相应版本的应用",
             user_message="这份数据与当前程序版本不兼容",
             user_hint="请在两台设备上使用相同版本的程序，升级后再重新同步")


def database_files(directory):
    directory = Path(directory)
    result = [("index", "index", directory / "index.db")]
    with closing(sqlite3.connect(directory / "index.db")) as conn:
        for uid, db_file in conn.execute("SELECT sync_uid,db_file FROM shops ORDER BY sync_uid"):
            target = (directory / db_file).resolve()
            if not target.is_relative_to(directory.resolve()) or not target.is_file():
                fail("SHOP_FILE_INVALID", "店铺数据库路径缺失或越界",
                     user_message="店铺数据文件缺失或位置不正确",
                     user_hint="请确认数据目录下的店铺文件没有被移动或删除")
            result.append((uid, "shop", target))
    return result


def export_database(source, destination, kind):
    destination.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(source)
    try:
        objects = catalog(conn)
        check_schema(objects, kind)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != VERSIONS[kind]:
            fail("SCHEMA_UNSUPPORTED", "数据库版本不受当前同步格式支持",
                 user_message="本地数据的版本与当前同步格式不匹配",
                 user_hint="请在另一台设备上升级程序后重新同步")
        meta = {"kind": kind, "user_version": version,
                "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
                "encoding": conn.execute("PRAGMA encoding").fetchone()[0],
                # 统一写入当前版本的标准 catalog：升级库与新建库不会因 SQLite 保存的
                # DDL 文本不同而反复产生快照差异（实际结构已由上面的白名单确认）
                "schema": expected(kind),
                "sequences": dict(conn.execute("SELECT name,seq FROM sqlite_sequence")), "tables": {}}
        for obj in objects:
            if obj["type"] != "table":
                continue
            table = obj["name"]
            info = conn.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            columns = [row[1] for row in info]
            order = ["sync_uid"] if "sync_uid" in columns else [row[1] for row in sorted(info, key=lambda r: r[5]) if row[5]]
            count = 0
            with (destination / (table + ".jsonl")).open("w", encoding="utf-8", newline="\n") as output:
                for row in conn.execute(f"SELECT * FROM {quote(table)} ORDER BY " + ",".join(map(quote, order))):
                    output.write(dump(dict(zip(columns, map(encode, row)))) + "\n")
                    count += 1
            meta["tables"][table] = {"columns": columns, "rows": count}
        atomic_json(destination / "schema.json", meta)
    finally:
        conn.close()


def export_set(directory, output, dataset):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    databases = []
    for uid, kind, source in database_files(directory):
        if not re.fullmatch(r"index|[a-f0-9]{32}", uid):
            fail("UID_INVALID", "数据集包含无效同步标识",
                 user_message="这份数据缺少有效的标识信息",
                 user_hint="请换用另一端的数据重新同步，或在该设备上重新初始化")
        export_database(source, output / uid, kind)
        databases.append({"key": uid, "kind": kind})
    files = {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob("*")) if p.is_file() and p.name != "manifest.json"}
    manifest = {"format": FORMAT, "dataset": dataset, "databases": databases, "files": files}
    atomic_json(output / "manifest.json", manifest)
    return sha(output / "manifest.json")


def _validate_files(root):
    root = Path(root)
    manifest = read_json(root / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        fail("SNAPSHOT_INVALID", "远端不是受支持的 XM-IMS 快照",
             user_message="远端仓库里的内容不是本程序的数据",
             user_hint="请确认填写的仓库和分支正确，或改用一个空仓库重新开始")
    if not re.fullmatch(r"[a-f0-9]{32}", str(manifest.get("dataset", ""))):
        fail("SNAPSHOT_INVALID", "数据集标识无效",
             user_message="远端数据缺少有效的标识信息",
             user_hint="请在其他设备上重新同步一次，再回到本机重试")
    actual = set()
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts[0] == ".git":
            continue
        if p.is_symlink():
            fail("SNAPSHOT_INVALID", "快照不能包含符号链接",
                 user_message="远端数据中包含不支持的链接文件",
                 user_hint="请不要在数据仓库里手动添加文件或链接")
        if p.is_file() and rel.as_posix() != "manifest.json":
            actual.add(rel.as_posix())
    if actual != set(manifest.get("files", {})):
        fail("SNAPSHOT_INVALID", "快照文件清单不完整或包含额外文件",
             user_message="远端数据不完整，或混入了额外的文件",
             user_hint="请不要在数据仓库里手动增删文件，改为在本程序里重新同步")
    for filename, digest in manifest["files"].items():
        p = (root / filename).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file() or sha(p) != digest:
            fail("SNAPSHOT_INVALID", "快照文件校验失败",
                 user_message="远端数据与记录的内容对不上",
                 user_hint="数据可能在传输中损坏，请在其他设备上重新同步一次")
    keys = [d["key"] for d in manifest["databases"]]
    if len(keys) != len(set(keys)) or keys.count("index") != 1:
        fail("SNAPSHOT_INVALID", "数据库清单无效",
             user_message="远端数据的内容清单不正确",
             user_hint="请不要手工改动数据仓库，改为在本程序里重新同步")
    expected_files = set()
    for item in manifest["databases"]:
        key, kind = item["key"], item["kind"]
        if not re.fullmatch(r"index|[a-f0-9]{32}", key) or kind != ("index" if key == "index" else "shop"):
            fail("SNAPSHOT_INVALID", "数据库标识无效",
                 user_message="远端数据的内容清单不正确",
                 user_hint="请不要手工改动数据仓库，改为在本程序里重新同步")
        reader = read_json(root / key / "schema.json")
        if reader["user_version"] not in READABLE_VERSIONS[kind]:
            fail("SCHEMA_UNSUPPORTED", "快照数据库版本不兼容，请让其他设备升级应用后重新同步",
                 user_message="远端数据的版本与本机程序不匹配",
                 user_hint="请让其他设备把程序升级到相同版本后重新同步")
        check_schema(reader["schema"], kind)
        tables = {o["name"] for o in reader["schema"] if o["type"] == "table"}
        if set(reader["tables"]) != tables:
            fail("SNAPSHOT_INVALID", "数据表清单不完整",
                 user_message="远端数据缺少部分内容",
                 user_hint="请在其他设备上重新同步一次，再回到本机重试")
        expected_files.add(key + "/schema.json")
        expected_files.update(key + "/" + t + ".jsonl" for t in tables)
    if actual != expected_files:
        fail("SNAPSHOT_INVALID", "快照清单含未识别内容",
             user_message="远端数据里混入了本程序不认识的内容",
             user_hint="请不要在数据仓库里手动添加文件")
    return manifest


def validate_files(root):
    try:
        return _validate_files(root)
    except (OSError, ValueError, KeyError, TypeError):
        fail("SNAPSHOT_INVALID", "快照格式或文件清单无效",
             user_message="从远端取回的数据无法识别",
             user_hint="请在其他设备上重新同步一次，再回到本机重试")


def iter_rows(root, key, table):
    import json
    with (Path(root) / key / (table + ".jsonl")).open(encoding="utf-8") as stream:
        for line in stream:
            yield {k: decode(v) for k, v in json.loads(line).items()}


def validate_business(conn, kind):
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
        fail("DATABASE_INVALID", "数据库完整性或外键校验失败",
             user_message="数据自检没有通过",
             user_hint="请先在另一台设备上完成同步，再回到本机重新同步")
    tables = [o["name"] for o in catalog(conn) if o["type"] == "table"]
    for table in tables:
        if table != "custom_values":
            maximum = conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {quote(table)}").fetchone()[0]
            sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            if maximum and (not sequence or sequence[0] < maximum):
                fail("SEQUENCE_INVALID", "自增序列低于已存在编号",
                     user_message="数据编号出现异常",
                     user_hint="请在另一台设备上完成同步后再回到本机重试")
            for (uid,) in conn.execute(f"SELECT sync_uid FROM {quote(table)}"):
                if not re.fullmatch(r"[a-f0-9]{32}", uid):
                    fail("UID_INVALID", "记录同步标识无效",
                         user_message="部分记录缺少有效的标识信息",
                         user_hint="请在另一台设备上完成同步后再回到本机重试")
    if kind == "shop":
        from ..service.fields import RESERVED_NAMES
        for name, in conn.execute("SELECT name FROM custom_fields"):
            if not name.strip() or name in RESERVED_NAMES:
                fail("FIELD_INVALID", "自定义字段名称为空或与系统字段冲突",
                     user_message="自定义字段的名称与系统字段重复",
                     user_hint="请在另一台设备上修改该字段名后再同步")
        if conn.execute("SELECT item_id FROM movements GROUP BY item_id HAVING SUM(qty)<0 LIMIT 1").fetchone():
            fail("NEGATIVE_STOCK", "候选结果包含负库存，请在库存复核中修正",
                 user_message="合并后的库存会出现负数",
                 user_hint="请在冲突处理中逐项确认最终库存，必要时追加调整流水")
        for t, scale, negative, vi, vt in conn.execute(
            "SELECT f.type,f.scale,f.allow_negative,v.value_int,v.value_text "
            "FROM custom_values v JOIN custom_fields f ON f.id=v.field_id"):
            if not 0 <= scale <= 6:
                fail("FIELD_INVALID", "字段精度无效",
                     user_message="字段的小数位数超出允许范围",
                     user_hint="请在另一台设备上调整该字段的小数位后再同步")
            valid = (vi is None and isinstance(vt, str)) if t == "string" else (type(vi) is int and vt is None)
            if t == "bool":
                valid = valid and vi in (0, 1)
            if not valid or (t != "string" and not negative and vi < 0):
                fail("FIELD_INVALID", "字段值与字段定义不匹配",
                     user_message="部分字段值与字段定义对不上",
                     user_hint="请在另一台设备上检查该字段的取值后再同步")
        _validate_formulas(conn)


def _validate_formulas(conn) -> None:
    """公式校验：所有字段定义（不只是有值的字段）都要合法、引用数值型、无循环

    某个 SKU 缺值、动态除零或结果为负属于单元格状态，不是拒绝恢复整份快照的理由。
    """
    defs, parsed = [], {}
    for uid, name, t, scale, negative, text in conn.execute(
            "SELECT sync_uid,name,type,scale,allow_negative,formula_json FROM custom_fields"):
        if not 0 <= scale <= 6:
            fail("FIELD_INVALID", "字段精度无效",
                 user_message="字段的小数位数超出允许范围",
                 user_hint="请在另一台设备上调整该字段的小数位后再同步")
        row = {"uid": uid, "name": name, "type": t, "scale": scale,
               "allow_negative": bool(negative), "formula": None}
        defs.append(row)
        if text is None:
            continue
        if t not in formulas.RESULT_TYPES:
            fail("FIELD_INVALID", "计算字段的结果类型必须是整数、小数或百分比",
                 user_message="计算字段的结果类型不受支持",
                 user_hint="请在另一台设备上重新设置该计算字段的结果类型")
        if t == "int" and scale != 0:
            fail("FIELD_INVALID", "整数计算字段的显示位数必须为 0",
                 user_message="整数计算字段的小数位不正确",
                 user_hint="请在另一台设备上重新保存该计算字段的结果格式")
        try:
            row["formula"] = formulas.tokens_of(text)
            parsed[uid] = formulas.parse(row["formula"])
        except ApiError as exc:
            fail(exc.code, exc.message, exc.hint,
                 user_message=exc.user_message, user_hint=exc.user_hint)
    by_uid = {d["uid"]: d for d in defs}
    for parsed_field in parsed.values():
        try:
            formulas.check_refs(parsed_field, by_uid)
        except ApiError as exc:
            fail(exc.code, exc.message, exc.hint,
                 user_message=exc.user_message, user_hint=exc.user_hint)
    cycle = formulas.find_cycle(formulas.graph_of(parsed))
    if cycle:
        names = {d["uid"]: d["name"] for d in defs}
        fail("FORMULA_CYCLE", "候选结果中的公式存在循环引用：" + formulas.cycle_text(cycle, names),
             "请调整公式引用关系后再应用",
             user_message="合并后公式之间出现了循环引用：" + formulas.cycle_text(cycle, names),
             user_hint="请调整公式引用的字段后再应用")
    if conn.execute("SELECT 1 FROM custom_values v JOIN custom_fields f ON f.id=v.field_id "
                    "WHERE f.formula_json IS NOT NULL LIMIT 1").fetchone():
        fail("FIELD_INVALID", "计算字段不应有落库值",
             user_message="计算字段的数据状态异常",
             user_hint="请在另一台设备上重新保存该字段的公式后再同步")


def restore_database(root, key, target):
    meta = read_json(Path(root) / key / "schema.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        for obj in meta["schema"]:
            if obj["type"] == "table":
                conn.execute(obj["sql"])
        for table, info in meta["tables"].items():
            columns = info["columns"]
            actual = [r[1] for r in conn.execute(f"PRAGMA table_info({quote(table)})")]
            if columns != actual:
                fail("SNAPSHOT_INVALID", "表字段清单不匹配",
                     user_message="远端数据的结构与本机不一致",
                     user_hint="请在两台设备上使用相同版本的程序后重新同步")
            statement = f"INSERT INTO {quote(table)} (" + ",".join(map(quote, columns)) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
            count = 0
            batch = []
            for row in iter_rows(root, key, table):
                if set(row) != set(columns):
                    fail("SNAPSHOT_INVALID", "记录字段不完整",
                         user_message="远端数据中有记录内容不完整",
                         user_hint="请在其他设备上重新同步一次，再回到本机重试")
                batch.append(tuple(row[c] for c in columns))
                count += 1
                if len(batch) == 1000:
                    conn.executemany(statement, batch)
                    batch.clear()
            if batch:
                conn.executemany(statement, batch)
            if count != info["rows"]:
                fail("SNAPSHOT_INVALID", "记录数校验失败",
                     user_message="远端数据的记录数量对不上",
                     user_hint="请在其他设备上重新同步一次，再回到本机重试")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.executemany("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", meta["sequences"].items())
        for obj in meta["schema"]:
            if obj["type"] != "table":
                conn.execute(obj["sql"])
        conn.execute(f"PRAGMA user_version={int(meta['user_version'])}")
        conn.execute(f"PRAGMA application_id={int(meta['application_id'])}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        if meta["kind"] == "shop" and int(meta["user_version"]) < VERSIONS["shop"]:
            # v3 快照按已验证的旧结构完整重建后，用与运行库相同的迁移入口升到当前版本
            migrate_connection(conn)
        validate_business(conn, meta["kind"])
    finally:
        conn.close()


def _restore_set(root, output):
    manifest = validate_files(root)
    output = Path(output)
    if output.exists():
        raise ValueError("恢复目录必须是尚不存在的新代次")
    output.mkdir(parents=True)
    restore_database(root, "index", output / "index.db")
    with closing(sqlite3.connect(output / "index.db")) as conn:
        shops = conn.execute("SELECT sync_uid,db_file FROM shops").fetchall()
    if {uid for uid, _ in shops} != {d["key"] for d in manifest["databases"] if d["kind"] == "shop"}:
        fail("SNAPSHOT_INVALID", "索引库与店铺库清单不一致",
             user_message="远端数据中的店铺清单与本机对不上",
             user_hint="请在其他设备上重新同步一次，再回到本机重试")
    for uid, filename in shops:
        target = (output / filename).resolve()
        if not re.fullmatch(r"shops/[1-9][0-9]*\.db", filename) or not target.is_relative_to(output.resolve()):
            fail("SNAPSHOT_INVALID", "店铺路径无效",
                 user_message="远端数据中的店铺文件位置不正确",
                 user_hint="请不要手工改动数据仓库，改为在本程序里重新同步")
        restore_database(root, uid, target)
    return manifest


def restore_set(root, output):
    try:
        return _restore_set(root, output)
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        fail("SNAPSHOT_INVALID", "快照内容无法完整重建，请检查格式、字段或约束",
             user_message="远端数据无法完整恢复",
             user_hint="请在其他设备上重新同步一次，再回到本机重试")
