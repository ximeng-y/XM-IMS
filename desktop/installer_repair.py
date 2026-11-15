"""安装前处理中断更新：检查资料、保护备份、复用试运行恢复入口。

由安装包临时解出的最新源码运行，不启动应用，不依赖 Git 或第三方库。
只有用户确认补救后才备份并恢复；备份与失败资料均保留，不自动清理。
"""
import argparse
import configparser
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

# 嵌入式 Python 的 ._pth 不会自动加入脚本目录，也不采用 PYTHONPATH。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from process_lock import LockBusy, ProcessLock
from update_trial import data_path, instance_lock, read_trial, restore


INTERRUPTED = {"pending", "starting", "committing"}
NORMAL = {"stable", "prepared"}
SLOT = re.compile(r"(?:legacy|[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{32})\Z")
ATTEMPT = re.compile(r"[0-9a-f]{16,32}\Z")
GENERATION = re.compile(r"[0-9a-f]{32}\Z")


class RepairError(RuntimeError):
    """可直接显示给安装用户的停止原因。"""


def read_ini(path):
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-16-le" if b"\x00" in raw[:64] else "utf-8-sig")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return parser


def inspect_installation(root):
    """只读取该安装目录的选择与恢复资料，不把残留标记当作运行进程。"""
    data = root / "data"
    selection = data / "update" / "desktop" / "selection.ini"
    if not selection.is_file():
        return None
    try:
        parser = read_ini(selection)
        values = parser["selection"]
        phase = values["phase"]
        if values.get("protocol") != "1" or phase not in NORMAL | INTERRUPTED:
            raise ValueError("selection")
    except (OSError, UnicodeError, configparser.Error, KeyError, ValueError):
        raise RepairError("更新记录无法读取，已停止安装。请保留原目录中的数据。") from None
    if phase in NORMAL:
        return None

    active, candidate, attempt = (
        values.get("active", ""), values.get("candidate", ""), values.get("attempt", "")
    )
    if not SLOT.fullmatch(active) or not SLOT.fullmatch(candidate) or candidate == "legacy":
        raise RepairError("更新记录中的版本不完整，已停止安装。请保留原目录中的数据。")
    previous = root if active == "legacy" else root / "versions" / active
    executable = previous / ("xm-ims-legacy.exe" if active == "legacy" else "xm-ims.exe")
    if not executable.is_file():
        raise RepairError("找不到用于恢复的旧版程序，已停止安装。请保留原目录中的数据。")
    if phase == "pending":
        if attempt:
            raise RepairError("更新阶段与恢复记录不一致，已停止安装。")
    else:
        if not ATTEMPT.fullmatch(attempt):
            raise RepairError("本次更新的恢复记录不完整，已停止安装。")
        directory = data / "update" / "desktop" / "attempts" / attempt
        try:
            request = read_ini(directory / "request.ini")["request"]
            control = read_ini(directory / "control.ini")["control"]
            if (request.get("protocol") != "1" or request.get("attempt") != attempt
                    or request.get("from_slot") != active or request.get("to_slot") != candidate
                    or control.get("protocol") != "1" or control.get("attempt") != attempt):
                raise ValueError("attempt")
            command = control.get("command")
            if command not in ("wait", "commit"):
                raise ValueError("control")
            record = read_trial(data, attempt)
            if data_path(data, attempt).exists():
                if (record is None or not isinstance(record.get("original"), dict)
                        or not isinstance(record["original"].get("exists"), bool)
                        or not GENERATION.fullmatch(record["generation"])
                        or "dataset" not in record):
                    raise ValueError("data")
                original = record["original"]
                previous_value = original.get("value")
                if original["exists"]:
                    if (not isinstance(previous_value, dict)
                            or not GENERATION.fullmatch(previous_value.get("generation", ""))
                            or not isinstance(previous_value.get("dataset"), str)
                            or not previous_value["dataset"]
                            or record["dataset"] != previous_value["dataset"]
                            or not (data / "runtime" / previous_value["generation"] / "index.db").is_file()):
                        raise ValueError("original")
                elif previous_value is not None:
                    raise ValueError("unexpected original")
            elif phase == "committing" or command != "wait":
                # prepare 先保存 data.json，提交阶段不能把缺失资料当成“没动过数据”。
                raise ValueError("missing data")
        except (OSError, UnicodeError, configparser.Error, KeyError, ValueError, TypeError):
            raise RepairError("本次更新的恢复记录不完整，已停止安装。请保留原目录中的数据。") from None
    return {"parser": parser, "selection": selection, "phase": phase, "attempt": attempt}


def write_json(path, value):
    with path.open("w", encoding="utf-8", newline="\n") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())


def backup_data(root, state, lock_content, progress):
    """完整复制原 data；单独写出锁文件，避免读取本进程已锁定的字节。"""
    now = datetime.now().astimezone()
    destination = root / "repair-backups" / (now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    destination.mkdir(parents=True, exist_ok=False)
    progress["backup"] = str(destination)
    metadata = {
        "format": 1, "created_at": now.isoformat(), "install_root": str(root),
        "phase": state["phase"], "attempt": state["attempt"], "complete": False,
    }
    write_json(destination / "backup.json", metadata)
    data = root / "data"
    locked_directory = instance_lock(data).parent

    def ignore_lock(directory, names):
        return ["instance.lock"] if Path(directory) == locked_directory else []

    shutil.copytree(data, destination / "data", ignore=ignore_lock)
    if lock_content is not None:
        saved_lock = destination / "data" / "runtime" / "instance.lock"
        saved_lock.write_bytes(lock_content)
        shutil.copystat(instance_lock(data), saved_lock)
    metadata["complete"] = True
    write_json(destination / "backup.json", metadata)
    return destination


def finish_selection(state):
    """仅收尾本次失败选择；保留旧 active、上一版本及所有程序和数据副本。"""
    parser = state["parser"]
    values = parser["selection"]
    values["phase"] = "stable"
    for key in ("candidate", "candidate_version", "candidate_tag", "attempt",
                "fallback_error", "fallback_target", "fallback_version"):
        values[key] = ""
    path = state["selection"]
    temporary = path.with_name(path.name + ".repairing")
    # Node 与 NSIS 都读取 ASCII 选择字段；不把它改成仅 Win32 支持的 UTF-16。
    with temporary.open("w", encoding="utf-8", newline="\r\n") as out:
        parser.write(out, space_around_delimiters=False)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def run(root, repair, progress):
    state = inspect_installation(root)
    if state is None:
        return False
    lock_path = instance_lock(root / "data")
    # 保存加锁前的文件内容，完整备份时不需要打开已锁定的字节。
    lock_content = lock_path.read_bytes() if repair and lock_path.is_file() else None
    lock = ProcessLock(lock_path)
    try:
        lock.acquire()
    except LockBusy:
        raise RepairError("XM-IMS 仍在运行，请退出后重新安装。") from None
    try:
        state = inspect_installation(root)
        if state is None:
            return False
        if not repair:
            return True
        try:
            backup_data(root, state, lock_content, progress)
        except OSError:
            raise RepairError("保护备份没有完成，已停止安装，原数据未恢复。") from None
        try:
            result = restore(root / "data", state["attempt"], remove_copy=False) if state["attempt"] else "no_data"
            if result not in ("no_data", "unchanged", "restored"):
                raise RepairError("当前数据与更新记录不一致，已停止安装。保护备份已保留。")
            finish_selection(state)
        except RepairError:
            raise
        except Exception:
            raise RepairError("上次更新未能恢复，已停止安装。保护备份已保留。") from None
        progress["result"] = result
        return True
    finally:
        lock.close()


def write_result(path, status, message, progress):
    parser = configparser.ConfigParser(interpolation=None)
    parser["repair"] = {"status": status, "message": message, "backup": progress.get("backup", "")}
    # Win32 ReadINIStr 使用 UTF-16 BOM 正确读取中文路径和提示。
    with Path(path).open("w", encoding="utf-16", newline="\r\n") as out:
        parser.write(out, space_around_delimiters=False)


def main():
    parser = argparse.ArgumentParser(description="安装前检查或恢复中断更新")
    parser.add_argument("command", choices=("check", "repair"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    progress = {}
    try:
        needed = run(Path(args.root).resolve(), args.command == "repair", progress)
        status = "repaired" if needed and args.command == "repair" else "interrupted" if needed else "normal"
        write_result(args.result, status, "", progress)
        print("XM-IMS-INSTALL-REPAIR " + status, flush=True)
        return 10 if status == "interrupted" else 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, RepairError) else "无法处理上次更新，已停止安装。请保留原目录中的数据。"
        write_result(args.result, "failed", message, progress)
        # 不把配置、数据库内容或原始异常写入安装器输出。
        print("XM-IMS-INSTALL-REPAIR failed", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
