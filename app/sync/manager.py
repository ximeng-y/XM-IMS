"""同步状态机：本地 SQLite 始终经一致性副本进入 Git。"""
import contextlib
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from .. import config, db
from ..errors import ApiError
from . import snapshot
from .common import atomic_json, read_json, remove_tree, replace_path, fail, ProcessLock
from .gitstore import GitStore
from .merge import Merger

DEFAULTS = {"remote": "", "branch": "main", "auto": True, "interval": 10, "device": "本机"}


class SyncManager:
    def __init__(self, root=None, allow_file=False):
        self.root = Path(root or config.DATA_ROOT)
        self.directory = self.root / "sync"
        self.runtime = self.root / "runtime"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings = {**DEFAULTS, **read_json(self.directory / "settings.json", {})}
        self.state = read_json(self.directory / "state.json", {"cache_order": [], "pending": []})
        self.mutex = threading.RLock()
        self.process_lock = ProcessLock(self.runtime / "instance.lock")
        self.allow_file = allow_file
        self.active = read_json(self.runtime / "active.json")
        self.captured_epoch = None
        self.status_name = "checking"
        self.error = None
        self.busy = False
        self.started = False
        self._merger = None
        self._space = (0.0, 0)

    def save(self):
        atomic_json(self.directory / "state.json", self.state)

    def start(self):
        with self.mutex:
            self._start()

    def _start(self):
        self.process_lock.acquire()
        self.started = True
        db.set_ready(False)
        db.close_all()
        if self.active:
            config.use_generation(self.runtime / self.active["generation"])
            with db.internal_access():
                db.init()
                self.upgrade_active_shops()
        # repo 目录替换中断时先恢复旧仓库，不丢未上传对象。
        if not (self.directory / "repo").exists() and (self.directory / "repo-old").exists():
            replace_path(self.directory / "repo-old", self.directory / "repo", self.directory)
        journal = self.state.get("journal")
        if journal and journal.get("confirmed"):
            if journal.get("apply"):
                self.apply_snapshot(journal["hash"])
            self.mark_synced(journal["hash"], journal["commit"], compact=False)
        if self.settings["remote"]:
            self.run(startup=True, synchronize=True)
        else:
            self.status_name = "local" if self.active else "setup"
            db.set_ready(bool(self.active))

    def upgrade_active_shops(self):
        """内部访问阶段把当前代次的店铺库升到当前结构版本

        必须在开放业务之前完成：升级后的店铺库才与 v4 快照一致，否则首次捕获会因
        版本不匹配而失败，业务接口也会读到未迁移的结构。
        """
        index = db.index_db()
        with index.lock:
            shops = index.conn.execute("SELECT id FROM shops ORDER BY id").fetchall()
        for row in shops:
            db.shop_db(row["id"])

    def stop(self):
        db.set_ready(False)
        db.close_all()
        self.process_lock.close()
        self.started = False

    @contextlib.contextmanager
    def operation(self):
        if not self.mutex.acquire(blocking=False):
            fail("SYNC_BUSY", "已有同步任务运行，请稍后重试")
        if self.busy:
            self.mutex.release()
            fail("SYNC_BUSY", "已有同步任务运行，请稍后重试")
        self.busy = True
        was_ready = db.business_ready
        try:
            yield
        except OSError as exc:
            error = ApiError("SYNC_STORAGE_FAILED", "同步文件写入或移动失败，请检查磁盘空间、目录权限或文件占用后重试", status=409)
            self.error = {"code": error.code, "message": error.message}
            self.status_name = "conflict" if self.state.get("requires_resolution") else "blocked"
            db.set_ready(was_ready)
            raise error from exc
        finally:
            self.busy = False
            self.mutex.release()

    def git(self):
        if not self.settings["remote"]:
            fail("SYNC_NOT_CONFIGURED", "请先配置数据仓库")
        return GitStore(self.directory / "repo", self.settings["remote"], self.settings["branch"],
                        self.settings["device"], self.allow_file)

    def temporary(self, label):
        return self.directory / "work" / (label + "-" + uuid.uuid4().hex)

    def cache(self, source):
        source = Path(source)
        snapshot.validate_files(source)
        digest = snapshot.sha(source / "manifest.json")
        destination = self.directory / "objects" / digest
        if destination.exists():
            try:
                snapshot.validate_files(destination)
                if snapshot.sha(destination / "manifest.json") != digest:
                    fail("SNAPSHOT_INVALID", "缓存内容与快照标识不一致")
            except ApiError:
                remove_tree(destination, self.directory)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = self.temporary("cache")
            try:
                shutil.copytree(source, staging, ignore=shutil.ignore_patterns(".git"))
                snapshot.validate_files(staging)
                if snapshot.sha(staging / "manifest.json") != digest:
                    fail("SNAPSHOT_INVALID", "复制期间快照内容已变化")
                replace_path(staging, destination, self.directory)
            finally:
                remove_tree(staging, self.directory)
        order = self.state.setdefault("cache_order", [])
        if digest in order:
            order.remove(digest)
        order.append(digest)
        self.save()
        return digest

    def _snapshot_source(self, digest):
        """返回已校验的只读来源；工作树路径只在当前同步任务内有效。"""
        p = self.directory / "objects" / digest
        if p.exists():
            snapshot.validate_files(p)
            if snapshot.sha(p / "manifest.json") != digest:
                fail("SNAPSHOT_INVALID", "缓存内容与快照标识不一致")
            return p
        repo = self.directory / "repo"
        if (repo / "manifest.json").exists() and snapshot.sha(repo / "manifest.json") == digest:
            snapshot.validate_files(repo)
            return repo
        fail("BASE_MISSING", "所需同步基线不存在，不能安全比较，请重新绑定并选择数据来源")

    def object(self, digest):
        source = self._snapshot_source(digest)
        if source == self.directory / "repo":
            # 提交会覆盖工作树，必须返回独立副本；纯状态检查只使用只读来源。
            if self.cache(source) != digest:
                fail("SNAPSHOT_INVALID", "缓存期间快照内容已变化")
            return self.directory / "objects" / digest
        return source

    def capture(self, force=False):
        if not self.active:
            return None
        marker = db.epoch()
        if not force and marker == self.captured_epoch and self.state.get("local_hash"):
            return self.state["local_hash"]
        started = time.perf_counter()
        raw, text = self.temporary("capture-db"), self.temporary("capture-json")
        raw.mkdir(parents=True)
        try:
            with db._coordinator, db.internal_access():
                locked = time.perf_counter()
                index = db.index_db()
                shops = index.conn.execute("SELECT id,db_file FROM shops ORDER BY id").fetchall()
                pairs = [(index, raw / "index.db")] + [(db.shop_db(row["id"]), raw / row["db_file"]) for row in shops]
                for database, target in pairs:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(target)
                    try:
                        database.conn.backup(conn)
                    finally:
                        conn.close()
                captured = db.epoch()
                pause = time.perf_counter() - locked
            snapshot.export_set(raw, text, self.active["dataset"])
            digest = self.cache(text)
            self.state["last_capture"] = {"write_pause_seconds": pause, "total_seconds": time.perf_counter() - started}
            self.state["local_hash"] = digest
            self.captured_epoch = captured
            self.save()
            return digest
        finally:
            remove_tree(raw, self.directory)
            remove_tree(text, self.directory)

    def local_commit(self, git, digest):
        head = git.head()
        if head and ((digest == self.state.get("committed_hash") and head == self.state.get("committed_head")) or (digest == self.state.get("baseline") and head == self.state.get("base_commit"))):
            return head
        # 提交前保护即将被替换的工作树，尤其是共同基线。
        if (git.root / "manifest.json").exists():
            self.cache(git.root)
        previous = git.head()
        commit = git.commit(self.object(digest), "自动快照 · " + self.settings["device"] + " · " + config.now_iso())
        if commit != previous and commit not in self.state.setdefault("pending", []):
            self.state["pending"].append(commit)
        self.state.update(committed_hash=digest, committed_head=commit)
        self.save()
        return commit

    def remote_snapshot(self, git, commit):
        for commit_key, hash_key in (("base_commit", "baseline"), ("remote_commit", "remote_hash")):
            if commit == self.state.get(commit_key) and self.state.get(hash_key):
                try:
                    self._snapshot_source(self.state[hash_key])
                    return self.state[hash_key]
                except ApiError:
                    pass
        target = self.temporary("remote")
        try:
            git.checkout_snapshot(commit, target)
            return self.cache(target)
        finally:
            remove_tree(target, self.directory)

    def new_local(self):
        if self.active:
            return
        generation = uuid.uuid4().hex
        updated = {"generation": generation, "dataset": uuid.uuid4().hex}
        previous_directory = config.DATA_DIR
        db.set_ready(False)
        config.use_generation(self.runtime / generation)
        try:
            with db.internal_access():
                db.init()
            atomic_json(self.runtime / "active.json", updated)
        except BaseException:
            db.close_all()
            config.use_generation(previous_directory)
            raise
        self.active = updated

    def initialize(self, source):
        previously_ready = db.business_ready
        try:
            return self._initialize(source)
        except ApiError as exc:
            if exc.code not in ("SYNC_PENDING", "SYNC_BUSY"):
                self.error = {"code": exc.code, "message": exc.message}
                self.status_name = "offline" if exc.code in ("GIT_FAILED", "GIT_UNAVAILABLE") else "blocked"
                db.set_ready(previously_ready)
            raise

    def _initialize(self, source):
        if source not in ("local", "remote"):
            fail("INITIALIZE_INVALID", "请选择本地或远端作为初始化来源")
        with self.operation():
            if any(self.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
                self.error = None
                fail("SYNC_PENDING", "请先同步未上传提交或处理冲突，再重新初始化；现有历史和草稿已保留")
            self.error = None
            db.set_ready(False)
            if not self.settings["remote"]:
                if source != "local":
                    fail("SYNC_NOT_CONFIGURED", "请先配置远端")
                self.new_local()
                self.capture(force=True)
                self.status_name = "local"
                db.set_ready(True)
                return self.status()
            git = self.git()
            tip = git.fetch()
            remote = self.remote_snapshot(git, tip) if tip else None
            if source == "remote":
                if not remote:
                    fail("REMOTE_EMPTY", "远端尚无可恢复快照")
                self.capture(force=True)
                db.set_ready(False)
                self.apply_snapshot(remote)
                self.mark_synced(remote, tip)
            else:
                self.new_local()
                local = self.capture(force=True)
                db.set_ready(False)
                commit = self.local_commit(git, local)
                if tip and not git.is_ancestor(tip, commit):
                    commit = git.commit(self.object(local), "确认以本地数据初始化绑定", [tip, commit])
                self.publish(git, local, commit, apply=False)
            db.set_ready(True)
            self.state.pop("requires_resolution", None)
            self.save()
            return self.status()

    def settings_update(self, values):
        with self.operation():
            unknown = set(values) - set(DEFAULTS)
            if unknown:
                fail("SETTINGS_INVALID", "设置包含未知字段")
            updated = {**self.settings, **values}
            if type(updated["interval"]) is not int or updated["interval"] < 1 or type(updated["auto"]) is not bool:
                fail("SETTINGS_INVALID", "自动同步间隔必须是至少 1 分钟的整数")
            if not isinstance(updated["device"], str) or not 1 <= len(updated["device"].strip()) <= 80:
                fail("SETTINGS_INVALID", "设备名称长度应为 1–80 个字符")
            if not isinstance(updated["branch"], str) or not updated["branch"] or updated["branch"].startswith("-"):
                fail("SETTINGS_INVALID", "分支名无效")
            if not isinstance(updated["remote"], str):
                fail("SETTINGS_INVALID", "仓库地址必须是文本")
            if updated["remote"]:
                GitStore.validate_remote(updated["remote"], self.allow_file)
            rebound = (updated["remote"], updated["branch"]) != (self.settings["remote"], self.settings["branch"])
            if rebound and any(self.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
                fail("SYNC_PENDING", "请先处理未上传提交或冲突，再更换远端")
            if rebound:
                repo = self.directory / "repo"
                if repo.exists():
                    if (repo / "manifest.json").exists():
                        self.cache(repo)
                    remove_tree(repo, self.directory)
                for key in ("baseline", "base_commit", "write_verified", "read_verified", "requires_resolution"):
                    self.state.pop(key, None)
            self.settings = updated
            atomic_json(self.directory / "settings.json", self.settings)
            self.save()
            return self.settings

    def test_connection(self):
        with self.operation():
            git = self.git()
            git.fetch()
            self.state["read_verified"] = True
            self.save()
            return {"read_verified": True, "write_verified": bool(self.state.get("write_verified"))}

    def run(self, startup=False, synchronize=False):
        with self.operation():
            was_ready = db.business_ready
            self.error = None
            if synchronize:
                db.set_ready(False)
            try:
                if not self.settings["remote"]:
                    self.status_name = "local" if self.active else "setup"
                    db.set_ready(bool(self.active))
                    return self.status()
                git = self.git()
                local = self.capture()
                try:
                    tip = git.fetch()
                except ApiError:
                    if local and self.state.get("baseline") and not self.state.get("session"):
                        self.local_commit(git, local)
                    raise
                self.state["read_verified"] = True
                remote = self.remote_snapshot(git, tip) if tip else None
                baseline = self.state.get("baseline")
                if baseline is None:
                    empty = not self.active
                    if self.active:
                        with db.internal_access(), db._coordinator:
                            empty = db.index_db().conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0] == 0
                    if remote and empty:
                        db.set_ready(False)
                        self.apply_snapshot(remote)
                        self.mark_synced(remote, tip)
                        db.set_ready(True)
                    else:
                        self.status_name = "binding" if remote else "initialize"
                        db.set_ready(was_ready and not startup)
                    return self.status()
                if not remote:
                    fail("REMOTE_MISSING", "已绑定的远端分支消失，禁止自动重新初始化")
                if read_json(self._snapshot_source(remote) / "manifest.json")["dataset"] != self.active["dataset"]:
                    fail("DATASET_CONFLICT", "远端数据集已改变，请重新绑定并明确选择数据来源")
                self.state["remote_hash"], self.state["remote_commit"] = remote, tip
                journal = self.state.get("journal")
                session = self.state.get("session")
                if journal and journal.get("apply") and tip == journal["commit"] and remote == journal["hash"] and session and local == session["local"]:
                    journal["confirmed"] = True
                    self.save()
                    self.apply_snapshot(remote)
                    self.mark_synced(remote, tip)
                    db.set_ready(True)
                    return self.status()
                if local == remote:
                    head = git.head()
                    if self.state.get("pending") and head != tip and not git.is_ancestor(head, tip):
                        commit = git.commit(self.object(local), "合并内容一致的离线历史", [tip, head])
                        self.publish(git, local, commit, apply=False)
                    else:
                        self.mark_synced(local, tip)
                    db.set_ready(True)
                elif remote == baseline:
                    commit = self.local_commit(git, local)
                    if not git.is_ancestor(tip, commit):
                        commit = git.commit(self.object(local), "保留双方版本历史", [tip, commit])
                    self.publish(git, local, commit, apply=False)
                    db.set_ready(True)
                elif local == baseline:
                    self.status_name = "behind"
                    if startup or synchronize:
                        db.set_ready(False)
                        if self.state.get("pending"):
                            commit = git.commit(self.object(remote), "采用远端并保留离线中间历史", [tip, git.head()])
                            self.publish(git, remote, commit, apply=True)
                        else:
                            self.apply_snapshot(remote)
                            self.mark_synced(remote, tip)
                        db.set_ready(True)
                    else:
                        db.set_ready(was_ready)
                else:
                    commit = self.local_commit(git, local)
                    if git.is_ancestor(tip, commit):
                        self.publish(git, local, commit, apply=False)
                        db.set_ready(True)
                    else:
                        self.status_name = "conflict"
                        self.state["requires_resolution"] = True
                        if startup or synchronize:
                            db.set_ready(False)
                            self.begin_session(baseline, local, remote, tip, commit)
                        else:
                            db.set_ready(was_ready)
                self.save()
            except ApiError as exc:
                self.error = {"code": exc.code, "message": exc.message}
                self.status_name = "offline" if exc.code in ("GIT_FAILED", "GIT_UNAVAILABLE") else "blocked"
                if self.state.get("requires_resolution"):
                    self.status_name = "conflict"
                db.set_ready(was_ready and not startup)
                self.save()
            return self.status()

    def begin_session(self, base, local, remote, tip, head):
        old = self.state.get("session")
        if old and [old[k] for k in ("base", "local", "remote", "remote_commit")] == [base, local, remote, tip]:
            return
        self.state["session"] = {"id": uuid.uuid4().hex, "base": base, "local": local, "remote": remote,
                                 "remote_commit": tip, "local_commit": head, "created_at": config.now_iso(),
                                 "choices": {}, "revision": 0}
        self._merger = None
        self.save()

    def session(self, identity=None):
        session = self.state.get("session")
        if not session or (identity and session["id"] != identity):
            fail("CONFLICT_STALE", "冲突会话已过期，请重新检查同步")
        return session

    def merger(self):
        session = self.session()
        if self._merger is None:
            self._merger = Merger(*(self.object(session[k]) for k in ("base", "local", "remote")),
                                  session["id"], session["created_at"])
        return self._merger

    def conflict_page(self, identity, offset=0, limit=30, unresolved=False):
        with self.mutex:
            session = self.session(identity)
            merger = self.merger()
            merger.build(session["choices"])
            conflicts = [c for c in merger.conflicts if not unresolved or not c["resolved"]]
            return {"session": identity, "revision": session["revision"], "total": len(conflicts),
                    "unresolved": sum(not c["resolved"] for c in merger.conflicts),
                    "conflicts": conflicts[offset:offset + limit], "changes_total": len(merger.changes),
                    "changes": [{**c, "label": merger.describe(c["key"])} for c in merger.changes[offset:offset + limit]], "validated": session.get("validated", False),
                    "renumber": session.get("renumber", [])}

    def conflict_details(self, identity, conflict_id, offset, limit):
        with self.mutex:
            session = self.session(identity)
            merger = self.merger()
            merger.build(session["choices"])
            return merger.details(conflict_id, offset, limit)

    def changes_page(self, identity, offset, limit):
        with self.mutex:
            session = self.session(identity)
            merger = self.merger()
            merger.build(session["choices"])
            return {"total": len(merger.changes), "rows": [{**c, "label": merger.describe(c["key"])} for c in merger.changes[offset:offset + limit]]}

    def resolve(self, identity, revision, choices):
        with self.operation():
            session = self.session(identity)
            if revision != session["revision"] or db.business_ready:
                fail("CONFLICT_STALE", "冲突草稿已过期，请重新进入同步")
            merger = self.merger()
            merger.build(session["choices"])
            known = {c["id"] for c in merger.conflicts}
            if set(choices) - known:
                fail("RESOLUTION_INVALID", "请求包含未知冲突项")
            updated = {**session["choices"], **choices}
            merger.build(updated)
            session["choices"] = updated
            session["revision"] += 1
            session["validated"] = False
            self.save()
            return {"revision": session["revision"]}

    def validate_resolution(self, identity, revision):
        with self.operation():
            session = self.session(identity)
            if revision != session["revision"] or db.business_ready:
                fail("CONFLICT_STALE", "冲突草稿已过期")
            merger = self.merger()
            result = merger.build(session["choices"])
            if any(not c["resolved"] for c in merger.conflicts):
                fail("CONFLICT_UNRESOLVED", "请先处理全部冲突和库存复核项")
            candidate, text = self.temporary("merge-db"), self.temporary("merge-json")
            try:
                merger.materialize(result, candidate)
                snapshot.export_set(candidate, text, self.active["dataset"])
                session["candidate"] = self.cache(text)
                session["renumber"] = [{**r, "label": merger.describe(r["database"] + "/" + r["table"].replace(".seq", "") + "/" + r["uid"])} for r in merger.renumber]
                session["validated"] = True
                self.save()
                return {"validated": True, "candidate": session["candidate"], "renumber": session["renumber"],
                        "changes": len(merger.changes)}
            except (sqlite3.Error, KeyError, ValueError) as exc:
                fail("MERGE_INVALID", "候选结果仍有名称、引用或字段冲突，请调整处理结果")
            finally:
                remove_tree(candidate, self.directory)
                remove_tree(text, self.directory)

    def apply_resolution(self, identity, revision, confirm):
        with self.operation():
            session = self.session(identity)
            if revision != session["revision"] or not session.get("validated") or not confirm or db.business_ready:
                fail("CONFLICT_STALE", "请先校验并确认当前合并总览")
            git = self.git()
            tip = git.fetch()
            local = self.capture(force=True)
            if tip != session["remote_commit"] or local != session["local"]:
                if not tip:
                    fail("REMOTE_MISSING", "远端分支已消失")
                remote = self.remote_snapshot(git, tip)
                self.begin_session(session["base"], local, remote, tip, self.local_commit(git, local))
                self.save()
                fail("CONFLICT_STALE", "本地或远端已变化，已重新生成比较，请重新确认")
            candidate = session["candidate"]
            commit = git.commit(self.object(candidate), "手动解决数据冲突 · " + self.settings["device"],
                                [tip, session["local_commit"]], update=False)
            self.publish(git, candidate, commit, apply=True)
            db.set_ready(True)
            return self.status()

    def cancel(self, identity):
        with self.operation():
            session = self.session(identity)
            session["validated"] = False
            self.state.pop("journal", None)
            db.set_ready(bool(self.active))
            self.status_name = "conflict"
            self.save()
            return self.status()

    def offline(self):
        # 与同步状态切换共用互斥，防止等待远端时重新开放业务写入。
        with self.operation():
            if not self.active or self.status_name != "offline" or self.state.get("requires_resolution"):
                fail("OFFLINE_NOT_ALLOWED", "当前状态不能跳过同步检查")
            db.set_ready(True)
            return self.status()

    def publish(self, git, digest, commit, apply):
        self.state["journal"] = {"hash": digest, "commit": commit, "apply": apply, "confirmed": False}
        self.save()
        git.push(commit)
        self.state["write_verified"] = True
        self.state["journal"]["confirmed"] = True
        self.save()
        if apply:
            self.apply_snapshot(digest)
        self.mark_synced(digest, commit)

    def apply_snapshot(self, digest):
        source = self.object(digest)
        generation = uuid.uuid4().hex
        target = self.runtime / generation
        upgraded = self.needs_upgrade(source)
        try:
            manifest = snapshot.restore_set(source, target)
        except BaseException:
            remove_tree(target, self.runtime)
            raise
        old = self.active
        updated = {"generation": generation, "dataset": manifest["dataset"], "previous": old["generation"] if old else None}
        with db._coordinator, db.internal_access():
            db.close_all()
            try:
                atomic_json(self.runtime / "active.json", updated)
            except OSError:
                # 以磁盘上唯一活动指针为准；不能让内存状态提前指向未生效的代次。
                self.active = read_json(self.runtime / "active.json", old)
                if self.active:
                    config.use_generation(self.runtime / self.active["generation"])
                    db.init()
                raise
            self.active = updated
            config.use_generation(target)
            db.init()
            self.upgrade_active_shops()
        self.state["local_hash"] = digest
        self.captured_epoch = db.epoch()
        self.save()
        if upgraded:
            # 恢复来源含 v3 店铺库，实际运行库已升到 v4：不能再把它当成与旧快照相同
            self.invalidate_capture_cache()

    @staticmethod
    def needs_upgrade(source) -> bool:
        """恢复来源中是否存在低于当前结构版本的店铺库"""
        for item in read_json(Path(source) / "manifest.json", {}).get("databases", []):
            meta = read_json(Path(source) / item["key"] / "schema.json", {})
            if meta.get("kind") == "shop" and meta.get("user_version") != snapshot.VERSIONS["shop"]:
                return True
        return False

    def invalidate_capture_cache(self):
        """使本地捕获缓存失效，让下一次 capture 重新生成实际快照

        local_hash 与 captured_epoch 必须一并清掉：只改前者时，没有业务写入的
        情况下 capture 仍会命中「epoch 未变 + 有 local_hash」的复用分支，于是一直
        沿用旧 hash。远端与共同基线的原始 hash 不受影响。
        """
        self.state.pop("local_hash", None)
        self.state.pop("committed_hash", None)
        self.state.pop("committed_head", None)
        self.captured_epoch = None
        self.save()

    def mark_synced(self, digest, commit, compact=True):
        unchanged = self.state.get("base_commit") == commit and not any(self.state.get(k) for k in ("pending", "session", "journal", "cleanup_pending"))
        self.state.update(baseline=digest, base_commit=commit, pending=[])
        for key in ("session", "requires_resolution", "journal"):
            self.state.pop(key, None)
        self._merger = None
        self.status_name = "synced"
        self.save()
        if compact and unchanged:
            self.prune()
        elif compact:
            try:
                if self.git().compact(commit):
                    self.prune()
                else:
                    self.state["cleanup_pending"] = True
                    self.save()
            except (ApiError, OSError):
                # 已同步结果不因可重试的本地历史收缩失败而回滚。
                self.state["cleanup_pending"] = True
                self.save()

    def prune(self):
        if self.state.get("pending") or self.state.get("session") or self.state.get("journal"):
            return
        repo = self.directory / "repo"
        current = snapshot.sha(repo / "manifest.json") if (repo / "manifest.json").exists() else None
        order = self.state.get("cache_order", [])
        older = [h for h in order if h != current]
        keep = set(older[-2:])
        baseline = self.state.get("baseline")
        if baseline and baseline != current:
            keep.add(baseline)
        objects = self.directory / "objects"
        if objects.exists():
            for child in objects.iterdir():
                if child.name not in keep:
                    remove_tree(child, self.directory)
        self.state["cache_order"] = [h for h in order if h in keep or h == current]
        if self.active:
            generations = {self.active["generation"], self.active.get("previous")}
            for child in self.runtime.iterdir():
                if child.is_dir() and len(child.name) == 32 and child.name not in generations:
                    remove_tree(child, self.runtime)
        self.state.pop("cleanup_pending", None)
        self.save()

    def status(self):
        if time.monotonic() - self._space[0] > 60:
            try:
                total_bytes = sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file())
                self._space = (time.monotonic(), total_bytes)
            except OSError:
                pass
        return {"state": self.status_name, "business_ready": db.business_ready, "busy": self.busy,
                "dirty": self.active is not None and db.epoch() != self.captured_epoch,
                "error": self.error, "configured": bool(self.settings["remote"]),
                "pending_commits": len(self.state.get("pending", [])),
                "session": self.state.get("session", {}).get("id"),
                "read_verified": bool(self.state.get("read_verified")),
                "write_verified": bool(self.state.get("write_verified")),
                "cleanup_pending": bool(self.state.get("cleanup_pending")), "storage_bytes": self._space[1],
                "snapshots": self.state.get("cache_order", [])[-3:],
                "retention_exception": bool(self.state.get("pending") or self.state.get("session")),
                "legacy_present": (self.root / "index.db").exists()}
