"""首次启动试运行数据：副本准备、指针提交与失败恢复。

只使用标准库，不导入 FastAPI、应用入口、业务服务或候选版本模块，因此同一份源码
既能在候选应用内运行，也能被复制到固定安装根的 bootstrap-support 后由旧版 Python
独立执行恢复。

- 原库只以只读连接读取，用 sqlite3.backup 复制到新的专属代次；原库与正式指针在
  提交前保持不变
- 本次资料保存原 active.json 完整内容或“原本不存在”，先落盘再操作副本
- 正式指针只在提交时写一次；恢复只处理本次资料描述的指针，不按目录扫描猜测
"""
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

PROTOCOL = 1
_RESULT_PREFIX = "XM-IMS-RESTORE"


def _now():
    return datetime.now().astimezone().isoformat()


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def runtime_dir(data_root):
    return Path(data_root) / "runtime"


def active_path(data_root):
    return runtime_dir(data_root) / "active.json"


def instance_lock(data_root):
    return runtime_dir(data_root) / "instance.lock"


def attempt_dir(data_root, attempt):
    return Path(data_root) / "update" / "desktop" / "attempts" / str(attempt)


def data_path(data_root, attempt):
    return attempt_dir(data_root, attempt) / "data.json"


def read_trial(data_root, attempt):
    """读取本次试运行资料；缺失或结构不符时返回 None。"""
    value = _read_json(data_path(data_root, attempt))
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        return None
    if value.get("attempt") != str(attempt) or not isinstance(value.get("generation"), str):
        return None
    return value


def _write_trial(data_root, attempt, value):
    _atomic_json(data_path(data_root, attempt), value)


def _copy_databases(sources, target_dir, progress=None):
    """把所有数据库复制到新代次；返回复制的数据库数量。

    只读取已确认存在的源库，复制期间按实际推进页数回调；单页数较大时多轮
    调用才能拿到中间进度。
    """
    prepared = []
    for relative, source in sources:
        if not source.is_file():
            raise FileNotFoundError(f"数据文件缺失：{relative}")
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as conn:
            prepared.append((relative, source, conn.execute("PRAGMA page_count").fetchone()[0]))
    total = sum(item[2] for item in prepared) or 1
    done = 0
    for relative, source, pages in prepared:
        destination = target_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        writer = sqlite3.connect(str(destination))
        try:
            def report(status, remaining, _total, _done=done):
                if progress is not None:
                    progress(_done + max(0, pages - remaining), total)
            reader.backup(writer, pages=64, progress=report, sleep=0.02)
        finally:
            reader.close()
            writer.close()
        done += pages
        if progress is not None:
            progress(done, total)
    return len(prepared)


def prepare(data_root, attempt, sources, progress=None):
    """准备本次试运行数据：保存原指针 → 复制数据库到新代次。

    sources 为 [(相对路径, 源库 Path)]，由调用方用同步域已有的数据库枚举得到。
    返回 {generation, dataset, previous, had_active}；原指针与源库保持不变。
    """
    data_root = Path(data_root)
    existing = read_trial(data_root, attempt)
    if existing is not None:
        raise RuntimeError("本次启动的试运行资料已经存在")
    original_file = active_path(data_root)
    original_exists = original_file.is_file()
    original = _read_json(original_file) if original_exists else None
    if original_exists and not isinstance(original, dict):
        raise RuntimeError("原数据指针无法读取，不能开始试运行")
    generation = uuid.uuid4().hex
    dataset = original.get("dataset") if original else None
    record = {
        "protocol": PROTOCOL,
        "attempt": str(attempt),
        "created_at": _now(),
        "generation": generation,
        "dataset": dataset,
        "status": "copying",
        "original": {"exists": original_exists, "value": original},
        "databases": [],
    }
    # 先落盘本次恢复资料，再创建副本目录与复制；失败时原指针从未改变。
    _write_trial(data_root, attempt, record)
    target = runtime_dir(data_root) / generation
    try:
        target.mkdir(parents=True, exist_ok=False)
        count = _copy_databases(sources, target, progress)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        record["status"] = "failed"
        _write_trial(data_root, attempt, record)
        raise
    record.update(status="ready", databases=[relative for relative, _ in sources])
    _write_trial(data_root, attempt, record)
    return {"generation": generation, "dataset": dataset, "had_active": original_exists,
            "previous": original.get("generation") if original else None, "count": count}


def commit(data_root, attempt):
    """把正式指针原子切换到本次副本；没有原数据时保持“无活动数据”。"""
    data_root = Path(data_root)
    record = read_trial(data_root, attempt)
    if record is None:
        raise RuntimeError("本次启动的试运行资料缺失，不能提交")
    if record.get("status") == "committed":
        return record
    if record.get("status") != "ready":
        raise RuntimeError("本次启动的数据副本尚未准备完成")
    original = record["original"]
    target = runtime_dir(data_root) / record["generation"]
    if original.get("exists"):
        updated = {
            "generation": record["generation"],
            "dataset": record["dataset"],
            "previous": (original.get("value") or {}).get("generation"),
        }
        _atomic_json(active_path(data_root), updated)
    elif (target / "index.db").is_file():
        # 原本没有活动数据，但首次使用流程已在副本里建好数据：此时才发布副本指针。
        updated = {"generation": record["generation"], "dataset": record["dataset"],
                   "previous": None}
        _atomic_json(active_path(data_root), updated)
    record["status"] = "committed"
    record["committed_at"] = _now()
    _write_trial(data_root, attempt, record)
    return record


def _same_pointer(left, right):
    if left is None or right is None:
        return left is right
    return (left.get("generation"), left.get("dataset")) == (right.get("generation"), right.get("dataset"))


def _selection_phase(data_root):
    """读取桌面程序选择资料的持久阶段；文件缺失或无法读取时返回空串。

    启动器只在首屏与数据都确认后写入 phase=stable，因此它也是“本次换版已确认”
    的唯一权威标记。选择资料由 NSIS 以 UTF-16LE 或 ANSI 写出，这里两种都接受。
    """
    path = Path(data_root) / "update" / "desktop" / "selection.ini"
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    encoding = "utf-16-le" if b"\x00" in raw[:64] else "utf-8"
    try:
        text = raw.decode(encoding, errors="ignore")
    except (UnicodeDecodeError, ValueError):
        return ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "phase":
            return value.strip()
    return ""


def _trial_confirmed(data_root):
    return _selection_phase(data_root) == "stable"


def restore(data_root, attempt, remove_copy=True):
    """恢复本次未确认试运行：只在指针仍是原指针或本次副本指针时执行。

    返回 no_data / committed / unchanged / restored / mismatch。committed 表示本次
    换版已被启动器持久确认（phase=stable），不再回退，调用方必须保留现有数据；
    unchanged / restored 不写正式指针以外的东西。mismatch 表示资料与当前指针
    不一致，调用方必须停止自动交接。
    """
    data_root = Path(data_root)
    record = read_trial(data_root, attempt)
    if record is None:
        return "no_data"
    if record.get("status") == "committed" and _trial_confirmed(data_root):
        return "committed"
    current = _read_json(active_path(data_root))
    original = record["original"]
    expected_original = original.get("value") if original.get("exists") else None
    copy_pointer = {"generation": record["generation"], "dataset": record["dataset"]}
    if _same_pointer(current, expected_original):
        result = "unchanged"
    elif _same_pointer(current, copy_pointer):
        if original.get("exists"):
            _atomic_json(active_path(data_root), expected_original)
        else:
            with contextlib.suppress(FileNotFoundError):
                active_path(data_root).unlink()
        result = "restored"
    else:
        return "mismatch"
    if remove_copy:
        target = runtime_dir(data_root) / record["generation"]
        if target.is_dir() and target.parent == runtime_dir(data_root):
            shutil.rmtree(target, ignore_errors=True)
    record["status"] = "restored"
    record["restored_at"] = _now()
    _write_trial(data_root, attempt, record)
    return result


def _load_lock():
    """独立运行时使用同目录的共享锁；应用内运行时使用应用包内的同一实现。"""
    if __package__:
        from ..storage.process_lock import LockBusy, ProcessLock
    else:
        # 嵌入式 Python 的 _pth 不包含脚本目录，也不读取 PYTHONPATH。
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from process_lock import LockBusy, ProcessLock
    return ProcessLock, LockBusy


def main(argv=None):
    """独立恢复入口：python update_trial.py restore --data <data目录> --attempt <标识>"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 5 or argv[0] != "restore" or argv[1] != "--data" or argv[3] != "--attempt":
        print(f"{_RESULT_PREFIX} usage", flush=True)
        return 2
    data_root, attempt = Path(argv[2]), argv[4]
    ProcessLock, LockBusy = _load_lock()
    lock = ProcessLock(instance_lock(data_root))
    try:
        lock.acquire()
    except LockBusy:
        print(f"{_RESULT_PREFIX} locked", flush=True)
        return 3
    try:
        result = restore(data_root, attempt)
    except Exception as exc:  # 恢复失败必须让调用方停止自动交接
        print(f"{_RESULT_PREFIX} failed {exc}", flush=True)
        return 1
    finally:
        lock.close()
    print(f"{_RESULT_PREFIX} {result}", flush=True)
    return 0 if result in ("no_data", "restored", "unchanged") else 4


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
