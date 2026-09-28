from contextlib import closing
"""确定性逻辑快照；流式导出/重建，远端 SQL 仅接受本版本定义。"""
import base64
import hashlib
import re
import sqlite3
from pathlib import Path
from .. import schema
from .common import atomic_json, dump, read_json, fail

FORMAT = 1
VERSIONS = {"index": 1, "shop": 3}


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def encode(value):
    if isinstance(value, float):
        fail("SNAPSHOT_REAL", "快照包含本项目禁止的 REAL 数据")
    return {"$blob": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value


def decode(value):
    if isinstance(value, dict) and set(value) == {"$blob"}:
        return base64.b64decode(value["$blob"], validate=True)
    if value is not None and type(value) not in (int, str):
        fail("SNAPSHOT_VALUE", "快照字段类型不受支持")
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


def expected(kind):
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(schema.INDEX_DDL if kind == "index" else schema.SHOP_DDL)
        return catalog(conn)


def normalized(objects):
    return [{**o, "sql": re.sub(r"\s+", " ", o["sql"]).strip()} for o in objects]


def check_schema(objects, kind):
    if normalized(objects) != normalized(expected(kind)):
        fail("SCHEMA_UNSUPPORTED", "快照结构与当前应用不兼容，请使用相应版本的应用")


def database_files(directory):
    directory = Path(directory)
    result = [("index", "index", directory / "index.db")]
    with closing(sqlite3.connect(directory / "index.db")) as conn:
        for uid, db_file in conn.execute("SELECT sync_uid,db_file FROM shops ORDER BY sync_uid"):
            target = (directory / db_file).resolve()
            if not target.is_relative_to(directory.resolve()) or not target.is_file():
                fail("SHOP_FILE_INVALID", "店铺数据库路径缺失或越界")
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
            fail("SCHEMA_UNSUPPORTED", "数据库版本不受当前同步格式支持")
        meta = {"kind": kind, "user_version": version,
                "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
                "encoding": conn.execute("PRAGMA encoding").fetchone()[0],
                "schema": objects,
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
            fail("UID_INVALID", "数据集包含无效同步标识")
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
        fail("SNAPSHOT_INVALID", "远端不是受支持的 XM-IMS 快照")
    if not re.fullmatch(r"[a-f0-9]{32}", str(manifest.get("dataset", ""))):
        fail("SNAPSHOT_INVALID", "数据集标识无效")
    actual = set()
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts[0] == ".git":
            continue
        if p.is_symlink():
            fail("SNAPSHOT_INVALID", "快照不能包含符号链接")
        if p.is_file() and rel.as_posix() != "manifest.json":
            actual.add(rel.as_posix())
    if actual != set(manifest.get("files", {})):
        fail("SNAPSHOT_INVALID", "快照文件清单不完整或包含额外文件")
    for filename, digest in manifest["files"].items():
        p = (root / filename).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file() or sha(p) != digest:
            fail("SNAPSHOT_INVALID", "快照文件校验失败")
    keys = [d["key"] for d in manifest["databases"]]
    if len(keys) != len(set(keys)) or keys.count("index") != 1:
        fail("SNAPSHOT_INVALID", "数据库清单无效")
    expected_files = set()
    for item in manifest["databases"]:
        key, kind = item["key"], item["kind"]
        if not re.fullmatch(r"index|[a-f0-9]{32}", key) or kind != ("index" if key == "index" else "shop"):
            fail("SNAPSHOT_INVALID", "数据库标识无效")
        meta = read_json(root / key / "schema.json")
        if meta["user_version"] != VERSIONS[kind]:
            fail("SCHEMA_UNSUPPORTED", "快照数据库版本不兼容")
        check_schema(meta["schema"], kind)
        tables = {o["name"] for o in meta["schema"] if o["type"] == "table"}
        if set(meta["tables"]) != tables:
            fail("SNAPSHOT_INVALID", "数据表清单不完整")
        expected_files.add(key + "/schema.json")
        expected_files.update(key + "/" + t + ".jsonl" for t in tables)
    if actual != expected_files:
        fail("SNAPSHOT_INVALID", "快照清单含未识别内容")
    return manifest


def validate_files(root):
    try:
        return _validate_files(root)
    except (OSError, ValueError, KeyError, TypeError):
        fail("SNAPSHOT_INVALID", "快照格式或文件清单无效")


def iter_rows(root, key, table):
    import json
    with (Path(root) / key / (table + ".jsonl")).open(encoding="utf-8") as stream:
        for line in stream:
            yield {k: decode(v) for k, v in json.loads(line).items()}


def validate_business(conn, kind):
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
        fail("DATABASE_INVALID", "数据库完整性或外键校验失败")
    tables = [o["name"] for o in catalog(conn) if o["type"] == "table"]
    for table in tables:
        if table != "custom_values":
            maximum = conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {quote(table)}").fetchone()[0]
            sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            if maximum and (not sequence or sequence[0] < maximum):
                fail("SEQUENCE_INVALID", "自增序列低于已存在编号")
            for (uid,) in conn.execute(f"SELECT sync_uid FROM {quote(table)}"):
                if not re.fullmatch(r"[a-f0-9]{32}", uid):
                    fail("UID_INVALID", "记录同步标识无效")
    if kind == "shop":
        from ..service.fields import RESERVED_NAMES
        for name, in conn.execute("SELECT name FROM custom_fields"):
            if not name.strip() or name in RESERVED_NAMES:
                fail("FIELD_INVALID", "自定义字段名称为空或与系统字段冲突")
        if conn.execute("SELECT item_id FROM movements GROUP BY item_id HAVING SUM(qty)<0 LIMIT 1").fetchone():
            fail("NEGATIVE_STOCK", "候选结果包含负库存，请在库存复核中修正")
        for t, scale, negative, vi, vt in conn.execute(
            "SELECT f.type,f.scale,f.allow_negative,v.value_int,v.value_text "
            "FROM custom_values v JOIN custom_fields f ON f.id=v.field_id"):
            if not 0 <= scale <= 6:
                fail("FIELD_INVALID", "字段精度无效")
            valid = (vi is None and isinstance(vt, str)) if t == "string" else (type(vi) is int and vt is None)
            if t == "bool":
                valid = valid and vi in (0, 1)
            if not valid or (t != "string" and not negative and vi < 0):
                fail("FIELD_INVALID", "字段值与字段定义不匹配")


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
                fail("SNAPSHOT_INVALID", "表字段清单不匹配")
            statement = f"INSERT INTO {quote(table)} (" + ",".join(map(quote, columns)) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
            count = 0
            batch = []
            for row in iter_rows(root, key, table):
                if set(row) != set(columns):
                    fail("SNAPSHOT_INVALID", "记录字段不完整")
                batch.append(tuple(row[c] for c in columns))
                count += 1
                if len(batch) == 1000:
                    conn.executemany(statement, batch)
                    batch.clear()
            if batch:
                conn.executemany(statement, batch)
            if count != info["rows"]:
                fail("SNAPSHOT_INVALID", "记录数校验失败")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.executemany("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", meta["sequences"].items())
        for obj in meta["schema"]:
            if obj["type"] != "table":
                conn.execute(obj["sql"])
        conn.execute(f"PRAGMA user_version={int(meta['user_version'])}")
        conn.execute(f"PRAGMA application_id={int(meta['application_id'])}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
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
        fail("SNAPSHOT_INVALID", "索引库与店铺库清单不一致")
    for uid, filename in shops:
        target = (output / filename).resolve()
        if not re.fullmatch(r"shops/[1-9][0-9]*\.db", filename) or not target.is_relative_to(output.resolve()):
            fail("SNAPSHOT_INVALID", "店铺路径无效")
        restore_database(root, uid, target)
    return manifest


def restore_set(root, output):
    try:
        return _restore_set(root, output)
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        fail("SNAPSHOT_INVALID", "快照内容无法完整重建，请检查格式、字段或约束")
