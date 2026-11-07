"""本地备份历史：持久 Git 仓库与索引的唯一所有者。

只处理快照文件与本机 Git：接收 SyncManager 传入的快照目录和数据目录，
保存版本、列出历史、提取指定版本、管理自动上传候选与幂等回执。

不操作运行数据库、不判断业务门禁、不访问远端；目录或 Git 的可用性由调用方
决定失败范围。读取历史不创建仓库，也不要求本机已安装 Git。
"""
import copy
import re
import uuid
from pathlib import Path

from .. import config
from ..errors import ApiError
from ..storage.files import atomic_json, read_json
from . import snapshot
from .common import fail
from .gitstore import GitStore

BRANCH = "history"
# 可成为自动上传候选的原因；还原前保护只保存旧数据，不参与自动上传
CANDIDATE_REASONS = ("scheduled", "manual", "sync", "restore")
LABELS = {"scheduled": "定时备份", "manual": "手动备份", "sync": "同步前备份",
          "protect": "还原前保护", "restore": "还原结果"}


class LocalBackups:
    def __init__(self, directory, device="本机"):
        self.directory = Path(directory)
        self.repo = self.directory / "repo"
        self.index_path = self.directory / "index.json"
        self.device = device

    # ---------- 索引 ----------

    def _read(self) -> dict:
        index = read_json(self.index_path, None)
        if not isinstance(index, dict):
            return {"version": 1, "backups": [], "candidate": None, "receipts": {}}
        index.setdefault("backups", [])
        index.setdefault("receipts", {})
        index.setdefault("candidate", None)
        return index

    def _write(self, index) -> None:
        atomic_json(self.index_path, index)

    def _store(self):
        """首次正式备份时才创建持久仓库；调用方都已持有同步任务锁。"""
        return GitStore.local(self.repo, BRANCH, self.device)

    # ---------- 保存版本 ----------

    def save(self, source, dataset, reason, *, source_backup=None, idempotency_key=None):
        """保存一次备份，返回 (记录, 是否新建版本)。

        内容与最近版本相同时复用该版本，不制造重复提交；source 为已导出的完整
        快照目录，调用方负责其生命周期。
        """
        source = Path(source)
        snapshot.validate_files(source)
        digest = snapshot.sha(source / "manifest.json")
        index = self._read()
        latest = index["backups"][-1] if index["backups"] else None
        if latest and latest["digest"] == digest and latest["dataset"] == dataset:
            record = {**copy.deepcopy(latest), "created_at": config.now_iso(),
                      "reason": reason, "source_backup": source_backup}
            created = False
        else:
            record_id = uuid.uuid4().hex
            message = f"{LABELS.get(reason, '备份')} · {self.device} · {config.now_iso()} · {record_id}"
            commit = self._store().commit(source, message)
            record = {"id": record_id, "commit": commit, "digest": digest, "dataset": dataset,
                      "created_at": config.now_iso(), "reason": reason, "actor": config.ACTOR,
                      "source_backup": source_backup}
            index["backups"].append(record)
            created = True
        if reason in CANDIDATE_REASONS:
            index["candidate"] = record["id"]
        if idempotency_key:
            index["receipts"][idempotency_key] = {"kind": "backup", "backup": record["id"],
                                                  "created": created}
        self._write(index)
        return copy.deepcopy(record), created

    # ---------- 历史读取 ----------

    def list(self, offset=0, limit=20, current_dataset=None, current_digest=None):
        rows = list(reversed(self._read()["backups"]))
        page = [self._row(r, r["digest"] == current_digest, r["dataset"] == current_dataset)
                for r in rows[offset:offset + limit]]
        return {"rows": page, "total": len(rows), "offset": offset, "limit": limit}

    @staticmethod
    def _row(record, is_current, same_dataset=True):
        return {"id": record["id"], "created_at": record["created_at"], "reason": record["reason"],
                "dataset": record["dataset"], "is_current": bool(is_current),
                "same_dataset": bool(same_dataset),
                "source_backup": record.get("source_backup")}

    def row(self, record, current_dataset=None, current_digest=None):
        """单条版本描述：是否为当前数据、是否属于当前数据集由调用方给定。"""
        return self._row(record, record["digest"] == current_digest,
                         current_dataset is None or record["dataset"] == current_dataset)

    def state_view(self, current_digest):
        """状态栏需要的本地备份关系：最近版本、待上传候选与当前数据的关系。

        current_digest 为本进程已知的当前内容标识；未提供时只报告已有历史。
        """
        index = self._read()
        latest = index["backups"][-1] if index["backups"] else None
        candidate = self.candidate()
        return {
            "latest": self._row(latest, bool(latest and latest["digest"] == current_digest)) if latest else None,
            "pending_upload": bool(candidate),
            "current_backed_up": bool(current_digest) and any(
                record["digest"] == current_digest for record in index["backups"]),
            "total": len(index["backups"]),
        }

    def describe(self, backup_id):
        """按对外标识取版本；不接受任意提交标识、分支名或路径。"""
        if not isinstance(backup_id, str) or not re.fullmatch(r"[a-f0-9]{32}", backup_id):
            fail("BACKUP_NOT_FOUND", "本地备份不存在",
                 user_message="找不到这份本地备份",
                 user_hint="请刷新备份列表后重试")
        for record in self._read()["backups"]:
            if record["id"] == backup_id:
                return copy.deepcopy(record)
        fail("BACKUP_NOT_FOUND", "本地备份不存在",
             user_message="找不到这份本地备份",
             user_hint="请刷新备份列表后重试")

    def extract(self, backup_id, target):
        """把指定版本取到调用方给出的临时目录，并校验内容与索引一致。"""
        record = self.describe(backup_id)
        try:
            store = self._store()
            if not store.has_commit(record["commit"]):
                fail("BACKUP_MISSING", "本地备份版本不在历史仓库中",
                     user_message="这份本地备份已不可用",
                     user_hint="请换一个备份版本，或重新做一次备份")
            store.checkout_snapshot(record["commit"], Path(target) / "snapshot")
        except OSError as exc:
            raise ApiError("BACKUP_STORAGE_FAILED", "读取本地备份失败", status=409,
                           user_message="读取本地备份暂未完成",
                           user_hint="请检查磁盘空间和目录访问后重试") from exc
        source = Path(target) / "snapshot"
        snapshot.validate_files(source)
        if snapshot.sha(source / "manifest.json") != record["digest"]:
            fail("BACKUP_INVALID", "本地备份内容与索引不一致",
                 user_message="这份本地备份的内容不完整",
                 user_hint="请换一个备份版本，或重新做一次备份")
        return record, source

    # ---------- 自动上传候选 ----------

    def candidate(self):
        index = self._read()
        for record in index["backups"]:
            if record["id"] == index.get("candidate"):
                return copy.deepcopy(record)
        return None

    def confirm_uploaded(self, digest):
        """上传内容与候选一致时确认该候选；当前数据可能已有更新，不影响历史。"""
        index = self._read()
        for record in index["backups"]:
            if record["id"] == index.get("candidate") and record["digest"] == digest:
                index["candidate"] = None
                self._write(index)
                return

    def backed_up(self, digest):
        """该内容是否已存在于本地历史；自动上传只允许发送这类内容。"""
        return bool(digest) and any(record["digest"] == digest for record in self._read()["backups"])

    def invalidate_candidate(self):
        """切换数据集或采用远端后，旧候选不再代表当前数据。"""
        index = self._read()
        if index.get("candidate") is not None:
            index["candidate"] = None
            self._write(index)

    # ---------- 幂等回执 ----------

    def receipt(self, key):
        return copy.deepcopy(self._read()["receipts"].get(key)) if key else None

    def set_receipt(self, key, value):
        if not key:
            return
        index = self._read()
        index["receipts"][key] = value
        self._write(index)
