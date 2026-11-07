"""同步状态机：本地 SQLite 始终经一致性副本进入 Git。"""
import contextlib
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from .. import config, db
from ..errors import ApiError, error_dict
from . import settings as sync_settings, snapshot
from .common import fail, ProcessLock
from ..storage.files import atomic_json, read_json, remove_tree, replace_path
from .gitstore import GitStore
from .local_backup import LocalBackups
from .merge import Merger

DEFAULTS = sync_settings.DEFAULTS


class SyncManager:
    def __init__(self, root=None, allow_file=False):
        self.root = Path(root or config.DATA_ROOT)
        self.directory = self.root / "sync"
        self.runtime = self.root / "runtime"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings = sync_settings.normalize(read_json(self.directory / "settings.json", {}))
        self.state = read_json(self.directory / "state.json", {"cache_order": [], "pending": []})
        self.mutex = threading.RLock()
        self.process_lock = ProcessLock(self.runtime / "instance.lock")
        self.allow_file = allow_file
        self.active = read_json(self.runtime / "active.json")
        self.captured_epoch = None
        self.status_name = "checking"
        self.error = None
        self.local_error = None
        self.busy = False
        self.started = False
        self._merger = None
        self._space = (0.0, 0)
        # (epoch, digest)：仅用于本进程内的内容关系缓存，不能替代持久备份/上传状态。
        self._relation = None
        self.backups = LocalBackups(self.directory / "local-backups", self.settings["device"])

    @staticmethod
    def _error_entry(exc):
        """状态栏错误：同时保留面向接口的文案与面向界面的文案，界面优先取后者。"""
        return error_dict(exc.code, exc.message, exc.hint, exc.user_message, exc.user_hint)

    def settings_snapshot(self):
        """配置副本，另带只供同步包内部使用的 allow_file；不含可写配置引用。"""
        return {**self.settings, "allow_file": self.allow_file}

    def settings_view(self):
        """HTTP 输出用的配置副本：字段与保存时的设置完全一致。"""
        return {**self.settings}

    def record_background_error(self, error):
        """记录后台同步错误；不改变业务门禁，已开放的业务继续可用。"""
        self.error = self._error_entry(error) if isinstance(error, ApiError) else error

    def record_local_error(self, error):
        """本地备份错误与远端错误分开表达，互不覆盖。"""
        self.local_error = self._error_entry(error) if isinstance(error, ApiError) else error

    def background_window(self):
        """后台自动任务的节奏与当前可执行性。

        应用生命周期用这些公开读取决定是否触发一次运行，不必直接读设置或内部字典。
        同步切换期间（存在冲突会话而业务未就绪）不自动运行；本地阶段与云端阶段
        各自判断前置条件。
        """
        settings = self.settings_snapshot()
        return {"interval": settings["interval"], "remote": bool(settings["remote"]),
                "auto": bool(settings["auto"]), "local_auto": bool(settings["local_auto"]),
                "started": self.started, "busy": self.busy,
                "session_open": bool(self.state.get("session")) and not db.business_ready}

    def block_start(self, error):
        """启动失败：记录错误并关闭业务门禁，由生命周期决定进程是否继续。"""
        self.status_name = "blocked"
        self.error = self._error_entry(error) if isinstance(error, ApiError) else error
        db.set_ready(False)

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
        if not self.settings["auto"]:
            # 关闭自动上云：启动阶段不访问远端；本地自动备份仍按开关执行。
            self._start_offline_cloud()
            if self.settings["local_auto"] and db.business_ready:
                self._automatic_local_backup("scheduled")
            return
        if self.settings["local_auto"] and self.active:
            # 先保存当前版本，保证启动采用远端数据前已有本地保护。
            self._automatic_local_backup("scheduled")
        if self.settings["remote"]:
            self.run(startup=True, synchronize=True)
        else:
            self.status_name = "local" if self.active else "setup"
            db.set_ready(bool(self.active))

    def _start_offline_cloud(self):
        """关闭自动上云后的启动收尾：只按本机数据与已知状态开放业务。

        未解决的冲突、未完成的冲突会话与未确认的推送/替换日志都属于已知问题，
        不能为了离线使用而跳过；无活动数据时仍进入首次使用流程。
        """
        if self.state.get("requires_resolution") or self.state.get("session"):
            self.status_name = "conflict"
            db.set_ready(False)
            return
        journal = self.state.get("journal")
        if journal and not journal.get("confirmed"):
            self.status_name = "blocked"
            self.error = error_dict(
                "SYNC_JOURNAL_PENDING", "上次同步未确认，需联网检查后才能继续",
                user_message="上次同步没有完成",
                user_hint="请打开数据同步页面，点击「检查远端」完成上次的操作")
            db.set_ready(False)
            return
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
            fail("SYNC_BUSY", "已有同步任务运行，请稍后重试",
                 user_message="同步正在进行中", user_hint="请等待当前同步结束后再试")
        if self.busy:
            self.mutex.release()
            fail("SYNC_BUSY", "已有同步任务运行，请稍后重试",
                 user_message="同步正在进行中", user_hint="请等待当前同步结束后再试")
        self.busy = True
        was_ready = db.business_ready
        try:
            yield
        except OSError as exc:
            error = ApiError("SYNC_STORAGE_FAILED", "同步文件写入或移动失败，请检查磁盘空间、目录权限或文件占用后重试", status=409,
                             user_message="同步数据写入失败",
                             user_hint="请检查磁盘剩余空间，并确认文件没有被其他程序占用")
            self.error = self._error_entry(error)
            self.status_name = "conflict" if self.state.get("requires_resolution") else "blocked"
            db.set_ready(was_ready)
            raise error from exc
        finally:
            self.busy = False
            self.mutex.release()

    def git(self):
        if not self.settings["remote"]:
            fail("SYNC_NOT_CONFIGURED", "请先配置数据仓库",
                 user_message="尚未配置同步仓库", user_hint="请先在下方填写仓库地址、分支和设备名并保存")
        return GitStore(self.directory / "repo", self.settings["remote"], self.settings["branch"],
                        self.settings["device"], self.allow_file,
                        github_account=self.settings.get("github_account", ""))

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
                    fail("SNAPSHOT_INVALID", "缓存内容与快照标识不一致",
                     user_message="本地暂存的同步数据与预期不一致",
                     user_hint="请点击「立即同步」重新比较")
            except ApiError:
                remove_tree(destination, self.directory)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = self.temporary("cache")
            try:
                shutil.copytree(source, staging, ignore=shutil.ignore_patterns(".git"))
                snapshot.validate_files(staging)
                if snapshot.sha(staging / "manifest.json") != digest:
                    fail("SNAPSHOT_INVALID", "复制期间快照内容已变化",
                     user_message="同步期间数据又发生了变化",
                     user_hint="请点击「立即同步」重新比较")
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
                fail("SNAPSHOT_INVALID", "缓存内容与快照标识不一致",
                     user_message="本地暂存的同步数据与预期不一致",
                     user_hint="请点击「立即同步」重新比较")
            return p
        repo = self.directory / "repo"
        if (repo / "manifest.json").exists() and snapshot.sha(repo / "manifest.json") == digest:
            snapshot.validate_files(repo)
            return repo
        fail("BASE_MISSING", "所需同步基线不存在，不能安全比较，请重新绑定并选择数据来源",
             user_message="本地与远端没有共同的历史版本，无法安全比较",
             user_hint="请在下方明确选择以本地数据还是远端数据为准")

    def object(self, digest):
        source = self._snapshot_source(digest)
        if source == self.directory / "repo":
            # 提交会覆盖工作树，必须返回独立副本；纯状态检查只使用只读来源。
            if self.cache(source) != digest:
                fail("SNAPSHOT_INVALID", "缓存期间快照内容已变化",
                 user_message="同步期间数据又发生了变化",
                 user_hint="请点击「立即同步」重新比较")
            return self.directory / "objects" / digest
        return source

    def capture(self, force=False, advance=True):
        """生成一致性快照并纳入内容缓存。

        advance=False 供状态查询使用：只取内容标识，不推进「已捕获」标记，
        因此 dirty 仍按原有的捕获口径表达。
        """
        if not self.active:
            return None
        marker = db.epoch()
        if not force and marker == self.captured_epoch and self.state.get("local_hash"):
            return self.state["local_hash"]
        started = time.perf_counter()
        raw, text = self.temporary("capture-db"), self.temporary("capture-json")
        raw.mkdir(parents=True)
        try:
            with db.coordinated_access(), db.internal_access():
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
            if advance:
                self.captured_epoch = captured
            self._relation = (captured, digest)
            self.save()
            return digest
        finally:
            remove_tree(raw, self.directory)
            remove_tree(text, self.directory)

    def current_digest(self):
        """当前运行数据的内容标识；只复用已有捕获缓存，不新导出整套数据。

        状态轮询每几秒一次，不能每次都完整导出。尚未捕获的写入返回 None，
        由调用方的「有修改待备份」表达；需要精确内容时走 capture(force=True)。
        """
        if not self.active:
            return None
        marker = db.epoch()
        if marker == self.captured_epoch and self.state.get("local_hash"):
            return self.state["local_hash"]
        if self._relation and self._relation[0] == marker:
            return self._relation[1]
        return None

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
            self._record_initialize_failure(exc, previously_ready)
            raise

    def _record_initialize_failure(self, exc, previously_ready):
        """初始化失败后的状态与业务门禁恢复；向导最终应用复用同一处理。"""
        if exc.code not in ("SYNC_PENDING", "SYNC_BUSY"):
            self.error = self._error_entry(exc)
            self.status_name = "offline" if exc.code in ("GIT_FAILED", "GIT_UNAVAILABLE") else "blocked"
            db.set_ready(previously_ready)

    def _initialize(self, source):
        if source not in ("local", "remote"):
            fail("INITIALIZE_INVALID", "请选择本地或远端作为初始化来源",
             user_message="请先选择使用本地数据还是远端数据",
             user_hint="点击页面上对应的按钮即可")
        with self.operation():
            return self._initialize_body(source)

    def _initialize_body(self, source, *, checked_remote=None):
        # 已持有 operation 锁时（向导最终应用）可直接调用本方法，不再嵌套进入 operation()。
        if any(self.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
            self.error = None
            fail("SYNC_PENDING", "请先同步未上传提交或处理冲突，再重新初始化；现有历史和草稿已保留",
             user_message="还有没有上传的改动或待处理的冲突",
             user_hint="请先完成同步或处理完冲突，再重新选择数据来源")
        self.error = None
        db.set_ready(False)
        if not self.settings["remote"]:
            if source != "local":
                fail("SYNC_NOT_CONFIGURED", "请先配置远端",
                 user_message="尚未配置同步仓库",
                 user_hint="请先在下方填写仓库地址、分支和设备名并保存")
            self.new_local()
            self.capture(force=True)
            self.status_name = "local"
            db.set_ready(True)
            return self.status()
        git = self.git()
        tip = git.fetch()
        # 新手流程绑定已确认的地址、分支和提交；旧初始化入口仍保留原行为。
        if checked_remote is not None and (git.remote, git.branch, tip) != checked_remote:
            fail("ONBOARDING_REMOTE_CHANGED", "实际初始化的远端提交与确认结果不一致",
                 user_message="备份内容有新的变化",
                 user_hint="请重新检查后确认")
        remote = self.remote_snapshot(git, tip) if tip else None
        if source == "remote":
            if not remote:
                fail("REMOTE_EMPTY", "远端尚无可恢复快照",
                 user_message="远端仓库里还没有可恢复的数据",
                 user_hint="请先在已有数据的设备上同步一次")
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
            return self._settings_update_locked(values)

    def _settings_update_locked(self, values):
        # 已持有 operation 锁时（向导最终应用）可直接调用；行为与原 settings_update 完全一致。
        unknown = set(values) - set(DEFAULTS)
        if unknown:
            fail("SETTINGS_INVALID", "设置包含未知字段",
             user_message="提交的设置中包含无法识别的项",
             user_hint="请刷新页面后重新填写")
        updated = sync_settings.merge(self.settings, values)
        if type(updated["interval"]) is not int or updated["interval"] < 1 \
                or type(updated["auto"]) is not bool or type(updated["local_auto"]) is not bool:
            fail("SETTINGS_INVALID", "自动同步间隔必须是至少 1 分钟的整数",
             user_message="自动同步间隔至少为 1 分钟",
             user_hint="请填写一个不小于 1 的整数")
        if not isinstance(updated["device"], str) or not 1 <= len(updated["device"].strip()) <= 80:
            fail("SETTINGS_INVALID", "设备名称长度应为 1–80 个字符",
             user_message="设备名称需为 1–80 个字符",
             user_hint="请缩短或补全设备名称")
        if not isinstance(updated["github_account"], str) or len(updated["github_account"]) > 100:
            fail("SETTINGS_INVALID", "GitHub 账号必须是不超过 100 字符的文本",
             user_message="GitHub 账号填写有误",
             user_hint="请在配置引导中重新选择账号")
        if not isinstance(updated["branch"], str) or not updated["branch"] or updated["branch"].startswith("-"):
            fail("SETTINGS_INVALID", "分支名无效",
             user_message="分支名无效", user_hint="请填写一个合法的分支名，例如 main")
        if not isinstance(updated["remote"], str):
            fail("SETTINGS_INVALID", "仓库地址必须是文本",
             user_message="仓库地址填写不正确",
             user_hint="请填写以 https:// 或 git@ 开头的仓库地址")
        if updated["remote"]:
            GitStore.validate_remote(updated["remote"], self.allow_file)
        rebound = (updated["remote"], updated["branch"]) != (self.settings["remote"], self.settings["branch"])
        if rebound and any(self.state.get(k) for k in ("pending", "session", "journal", "requires_resolution")):
            fail("SYNC_PENDING", "请先处理未上传提交或冲突，再更换远端",
             user_message="还有没有上传的改动或待处理的冲突",
             user_hint="请先完成同步或处理完冲突，再更换仓库地址")
        if rebound:
            repo = self.directory / "repo"
            if repo.exists():
                if (repo / "manifest.json").exists():
                    self.cache(repo)
                remove_tree(repo, self.directory)
            for key in ("baseline", "base_commit", "write_verified", "read_verified", "requires_resolution"):
                self.state.pop(key, None)
        self.settings = updated
        self.backups.device = updated["device"]
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

    def run(self, startup=False, synchronize=False, automatic=False):
        """执行一次同步检查；automatic=True 表示后台自动上云，只能用已保存的本地备份上传。"""
        with self.operation():
            return self._run_locked(startup=startup, synchronize=synchronize, automatic=automatic)

    def _run_locked(self, startup=False, synchronize=False, automatic=False):
        was_ready = db.business_ready
        self.error = None
        if synchronize:
            db.set_ready(False)
        try:
            if not self.settings["remote"]:
                if not startup and self.active:
                    # 未配置远端也能显式操作本地历史；失败只记录本地错误。
                    self._save_current("sync")
                self.status_name = "local" if self.active else "setup"
                db.set_ready(bool(self.active))
                return self.status()
            git = self.git()
            if synchronize and self.active:
                # 显式同步处理当前数据，先把它保存到本地历史，形成可上传候选。
                self._save_current("sync")
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
                    with db.internal_access(), db.coordinated_access():
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
                fail("REMOTE_MISSING", "已绑定的远端分支消失，禁止自动重新初始化",
                 user_message="远端的分支不存在了",
                 user_hint="请确认远端仓库和分支，或在下方重新选择数据来源")
            if read_json(self._snapshot_source(remote) / "manifest.json")["dataset"] != self.active["dataset"]:
                fail("DATASET_CONFLICT", "远端数据集已改变，请重新绑定并明确选择数据来源",
                 user_message="远端换成了另一套数据",
                 user_hint="请在下方明确选择以本地数据还是远端数据为准")
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
                self._confirm_uploaded(local)
                head = git.head()
                if self.state.get("pending") and head != tip and not git.is_ancestor(head, tip):
                    commit = git.commit(self.object(local), "合并内容一致的离线历史", [tip, head])
                    self.publish(git, local, commit, apply=False)
                else:
                    self.mark_synced(local, tip)
                db.set_ready(True)
            elif remote == baseline:
                if automatic and not self.backups.backed_up(local):
                    # 后台自动上云只发送已保存的本地备份，不把尚未备份的新编辑代为上传。
                    self.status_name = "local"
                    db.set_ready(was_ready)
                else:
                    commit = self.local_commit(git, local)
                    if not git.is_ancestor(tip, commit):
                        commit = git.commit(self.object(local), "保留双方版本历史", [tip, commit])
                    self.publish(git, local, commit, apply=False)
                    self._confirm_uploaded(local)
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
                    self._confirm_uploaded(remote)
                    db.set_ready(True)
                else:
                    db.set_ready(was_ready)
            else:
                commit = self.local_commit(git, local)
                if git.is_ancestor(tip, commit):
                    self.publish(git, local, commit, apply=False)
                    self._confirm_uploaded(local)
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
            return self.status()
        except ApiError as exc:
            self.error = self._error_entry(exc)
            self.status_name = "offline" if exc.code in ("GIT_FAILED", "GIT_UNAVAILABLE") else "blocked"
            if self.state.get("requires_resolution"):
                self.status_name = "conflict"
            db.set_ready(was_ready and not startup)
            self.save()
            return self.status()

    def _save_current(self, reason):
        """把当前运行数据保存为持久本地备份；失败只记录本地错误，不打断流程。"""
        if not self.active:
            return None
        try:
            return self._persist_backup(reason)
        except ApiError as exc:
            self.record_local_error(exc)
            return None

    def _persist_backup(self, reason, *, source_backup=None, idempotency_key=None):
        """捕获当前数据并写入本地历史；capture 只更新捕获缓存，不代表已持久备份。"""
        self.capture(force=True)
        record, created = self.backups.save(self._snapshot_source(self.state["local_hash"]),
                                            self.active["dataset"], reason,
                                            source_backup=source_backup,
                                            idempotency_key=idempotency_key)
        self.local_error = None
        return record, created

    def _automatic_local_backup(self, reason="scheduled"):
        """定时与启动阶段的本地备份：不要求远端、不混用同步工作仓库的 pending 状态。

        活动数据可用、没有冲突会话或待解决冲突时执行；同步任务互斥由 operation() 保证。
        """
        if not self.active or self.state.get("session") or self.state.get("requires_resolution"):
            return None
        try:
            with self.operation():
                return self._persist_backup(reason)
        except ApiError as exc:
            self.record_local_error(exc)
            return None

    def background_local_backup(self):
        """后台定时本地备份入口；开关关闭时不做任何事，失败只记录本地错误。"""
        if not self.settings["local_auto"]:
            return None
        return self._automatic_local_backup("scheduled")

    def _confirm_uploaded(self, digest):
        """上传成功后只确认对应候选；当前数据库可能已有更新编辑，不推进捕获标记。"""
        self.backups.confirm_uploaded(digest)

    def local_backup(self, dry_run=False, idempotency_key=None):
        """手动立即备份；内容没有变化时返回已有版本与 created=false。"""
        if not dry_run and not idempotency_key:
            fail("BACKUP_IDEMPOTENCY_REQUIRED", "正式备份必须提供 idempotency_key",
                 user_message="操作标识缺失",
                 user_hint="请刷新页面后重试")
        with self.operation():
            replay = self.backups.receipt(idempotency_key) if idempotency_key else None
            if replay and replay.get("kind") == "backup":
                record = self.backups.describe(replay["backup"])
                return {"backup": self.backup_row(record), "created": bool(replay.get("created")),
                        "dry_run": False, "idempotent_replay": True}
            if not self.active:
                fail("BACKUP_NO_DATA", "尚无活动数据可备份",
                     user_message="还没有可以备份的数据",
                     user_hint="请先创建店铺并录入数据")
            if dry_run:
                digest = self.current_digest()
                return {"backup": None, "created": not self.backups.backed_up(digest),
                        "dry_run": True, "idempotent_replay": False}
            record, created = self._persist_backup("manual", idempotency_key=idempotency_key)
            return {"backup": self.backup_row(record), "created": created,
                    "dry_run": False, "idempotent_replay": False}

    def backup_row(self, record):
        """单条本地备份描述，附带它与当前活动数据的关系。"""
        dataset = self.active["dataset"] if self.active else None
        return self.backups.row(record, dataset, self.current_digest())

    def backup_page(self, offset=0, limit=20):
        with self.mutex:
            return self.backups.list(offset, limit,
                                     current_dataset=self.active["dataset"] if self.active else None,
                                     current_digest=self.current_digest())

    def restore_backup(self, backup_id, dry_run=False, confirm=False, idempotency_key=None):
        """本地还原：保护当前数据、重建目标代次、切换指针并追加还原结果。"""
        if not dry_run and (confirm is not True or not idempotency_key):
            fail("RESTORE_CONFIRM_REQUIRED", "还原必须明确确认并提供 idempotency_key",
                 user_message="还原前需要确认",
                 user_hint="请确认还原后重试")
        with self.operation():
            return self._restore_locked(backup_id, dry_run, idempotency_key)

    def _restore_locked(self, backup_id, dry_run, idempotency_key):
        record = self.backups.describe(backup_id)
        replay = self.backups.receipt(idempotency_key) if idempotency_key else None
        if replay and replay.get("kind") == "restore":
            if replay.get("stage") != "done" and self.active \
                    and self.active["generation"] == replay.get("candidate_generation"):
                # 指针已切换到该候选，只是结果记录没写成：只补齐记录，不重复覆盖数据。
                return self._finish_restore_record(replay, record)
            return self._restore_receipt_view(replay, idempotent_replay=True)
        if self.state.get("session") or self.state.get("requires_resolution"):
            fail("SYNC_PENDING", "存在未解决的冲突，不能执行本地还原",
                 user_message="还有未处理的冲突",
                 user_hint="请先处理完冲突，再进行本地还原")
        journal = self.state.get("journal")
        if journal and not journal.get("confirmed"):
            fail("SYNC_PENDING", "上一次同步尚未完成，不能执行本地还原",
                 user_message="上一次同步还没有完成",
                 user_hint="请先进入数据同步页面完成上次的操作")
        work = self.temporary("restore")
        generation = uuid.uuid4().hex
        target = self.runtime / generation
        try:
            record, source = self.backups.extract(backup_id, work)
            # 直接重建到运行目录下的新代次：校验失败时只清掉这一份，不动活动指针。
            snapshot.restore_set(source, target)
            if dry_run:
                return {"target": self.backup_row(record), "protect_backup": None,
                        "result_backup": None, "restored": False, "dry_run": True,
                        "idempotent_replay": False}
            return self._apply_restore(record, target, idempotency_key)
        finally:
            remove_tree(work, self.directory)
            if target.exists() and self.active and self.active["generation"] != generation:
                remove_tree(target, self.runtime)

    def _apply_restore(self, record, target, idempotency_key):
        dataset = record["dataset"]
        current_dataset = self.active["dataset"] if self.active else None
        was_ready = db.business_ready
        db.set_ready(False)
        try:
            protect, _ = self._persist_backup("protect")
        except ApiError:
            db.set_ready(was_ready)
            raise
        old = self.active
        receipt = {"kind": "restore", "idempotency_key": idempotency_key, "target": record["id"],
                   "protect_backup": protect["id"],
                   "old_generation": old["generation"] if old else None,
                   "candidate_generation": Path(target).name, "stage": "applying"}
        self.backups.set_receipt(idempotency_key, receipt)
        try:
            self._activate_generation(target, dataset)
        except BaseException:
            # 指针未生效（或已由 _activate_generation 回滚到原代次）：保护备份保留，业务恢复。
            db.set_ready(was_ready)
            raise
        if dataset != current_dataset:
            # 目标属于另一数据集：不把结果自动推向当前远端，交由用户明确选择数据来源。
            self.status_name = "binding"
        self.invalidate_capture_cache()
        self.backups.invalidate_candidate()
        self.save()
        return self._finish_restore_record(receipt, record, current_dataset=current_dataset)

    def _finish_restore_record(self, receipt, record, current_dataset=None):
        """指针已指向目标代次后追加还原结果；失败时保留保护备份并返回可重试状态。"""
        if current_dataset is None:
            current_dataset = record["dataset"]
        try:
            result, _ = self._persist_backup("restore", source_backup=record["id"])
        except ApiError as exc:
            self.record_local_error(exc)
            db.set_ready(True)
            return {"target": self.backup_row(record), "protect_backup": {"id": receipt["protect_backup"]},
                    "result_backup": None, "restored": True, "dry_run": False,
                    "idempotent_replay": receipt.get("stage") == "done",
                    "dataset_changed": record["dataset"] != current_dataset,
                    "retryable": True, "error": self._error_entry(exc)}
        self.backups.set_receipt(receipt["idempotency_key"],
                                 {**receipt, "stage": "done", "result_backup": result["id"]})
        db.set_ready(True)
        return {"target": self.backup_row(record), "protect_backup": {"id": receipt["protect_backup"]},
                "result_backup": self.backup_row(result), "restored": True, "dry_run": False,
                "idempotent_replay": receipt.get("stage") == "done",
                "dataset_changed": record["dataset"] != current_dataset}

    @staticmethod
    def _restore_receipt_view(receipt, idempotent_replay=False):
        return {"target": {"id": receipt["target"]}, "protect_backup": {"id": receipt["protect_backup"]},
                "result_backup": {"id": receipt.get("result_backup")},
                "restored": receipt.get("stage") == "done", "dry_run": False,
                "idempotent_replay": idempotent_replay}

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
            fail("CONFLICT_STALE", "冲突会话已过期，请重新检查同步",
             user_message="这次冲突处理已过期",
             user_hint="请点击「立即同步」重新进入冲突处理")
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
                    "renumber": [dict(r) for r in session.get("renumber", [])]}

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
                fail("CONFLICT_STALE", "冲突草稿已过期，请重新进入同步",
                 user_message="这次冲突处理已过期",
                 user_hint="请点击「立即同步」重新进入冲突处理")
            merger = self.merger()
            merger.build(session["choices"])
            known = {c["id"] for c in merger.conflicts}
            if set(choices) - known:
                fail("RESOLUTION_INVALID", "请求包含未知冲突项",
                 user_message="提交的处理结果中有无法识别的项",
                 user_hint="请刷新页面后重新选择")
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
                fail("CONFLICT_STALE", "冲突草稿已过期",
                 user_message="这次冲突处理已过期",
                 user_hint="请重新进入同步页面处理")
            merger = self.merger()
            result = merger.build(session["choices"])
            if any(not c["resolved"] for c in merger.conflicts):
                fail("CONFLICT_UNRESOLVED", "请先处理全部冲突和库存复核项",
                 user_message="还有冲突项没有处理完",
                 user_hint="请逐项选择处理方式，库存复核项需要确认最终数量")
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
                fail("MERGE_INVALID", "候选结果仍有名称、引用或字段冲突，请调整处理结果",
                 user_message="处理结果之间仍有冲突",
                 user_hint="请检查重名、关联记录和字段定义的处理方式后再试")
            finally:
                remove_tree(candidate, self.directory)
                remove_tree(text, self.directory)

    def apply_resolution(self, identity, revision, confirm):
        with self.operation():
            session = self.session(identity)
            if revision != session["revision"] or not session.get("validated") or not confirm or db.business_ready:
                fail("CONFLICT_STALE", "请先校验并确认当前合并总览",
                 user_message="请先校验当前的合并结果",
                 user_hint="点击「校验」确认无误后再应用")
            git = self.git()
            tip = git.fetch()
            local = self.capture(force=True)
            if tip != session["remote_commit"] or local != session["local"]:
                if not tip:
                    fail("REMOTE_MISSING", "远端分支已消失",
                     user_message="远端的分支不存在了",
                     user_hint="请确认远端仓库和分支是否正确")
                remote = self.remote_snapshot(git, tip)
                self.begin_session(session["base"], local, remote, tip, self.local_commit(git, local))
                self.save()
                fail("CONFLICT_STALE", "本地或远端已变化，已重新生成比较，请重新确认",
                 user_message="本地或远端又有新的改动，比较结果已重新生成",
                 user_hint="请重新核对各项处理结果后再应用")
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
                fail("OFFLINE_NOT_ALLOWED", "当前状态不能跳过同步检查",
             user_message="当前情况下不能直接离线使用",
             user_hint="请先完成同步或处理冲突；只有连接失败时才能选择离线进入")
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
        self._activate_generation(target, manifest["dataset"])
        self.state["local_hash"] = digest
        self._relation = (db.epoch(), digest)
        self.captured_epoch = db.epoch()
        self.save()
        if upgraded:
            # 恢复来源含 v3 店铺库，实际运行库已升到 v4：不能再把它当成与旧快照相同
            self.invalidate_capture_cache()

    def _activate_generation(self, target, dataset):
        """把已完整重建的代次目录切换为活动数据集；失败时回滚到原指针与连接。

        调用前该目录必须已完成快照校验与数据库重建，本方法只负责指针、连接与升级。
        """
        old = self.active
        target = Path(target)
        updated = {"generation": target.name, "dataset": dataset,
                   "previous": old["generation"] if old else None}
        with db.coordinated_access(), db.internal_access():
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
        return updated

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
        # 可变内容一律返回副本，避免调用方经响应对象改写内部状态。
        current = self.current_digest() if self.active else None
        return {"state": self.status_name, "business_ready": db.business_ready, "busy": self.busy,
                "dirty": self.active is not None and db.epoch() != self.captured_epoch,
                "error": dict(self.error) if self.error else None,
                "local_error": dict(self.local_error) if self.local_error else None,
                "local_backup": self.backups.state_view(current),
                "configured": bool(self.settings["remote"]),
                "pending_commits": len(self.state.get("pending", [])),
                "session": self.state.get("session", {}).get("id"),
                "read_verified": bool(self.state.get("read_verified")),
                "write_verified": bool(self.state.get("write_verified")),
                "cleanup_pending": bool(self.state.get("cleanup_pending")), "storage_bytes": self._space[1],
                "snapshots": list(self.state.get("cache_order", [])[-3:]),
                "retention_exception": bool(self.state.get("pending") or self.state.get("session")),
                "legacy_present": (self.root / "index.db").exists()}
