"""同步包的准备接口：首次数据准备、仓库绑定与首次上传续接。

SyncManager 继续承担正常同步、冲突、快照切换与保留策略；本模块只把备份引导
需要的同步侧操作收拢成一组窄接口。只有同步包内部允许理解 baseline / pending /
journal / committed_head / committed_hash 的组合规则，因此这些原始字典不出现在
返回值里：对外是只读语义状态或受控描述，调用方无法借返回值改写同步状态。

不做 GitHub HTTP、不下载程序、不决定页面按钮、不开浏览器，也不拼装 HTTP 响应。
"""
import contextlib

from .. import config, db
from ..storage.files import read_json, remove_tree
from . import snapshot
from .common import fail


class SyncSetup:
    """首次准备与绑定的同步侧协作实现；与 SyncManager 同属同步包内部。"""

    def __init__(self, manager):
        self.manager = manager

    # ---------- 只读视图 ----------

    def settings_snapshot(self):
        """配置副本；allow_file 只供同步包内部使用，HTTP 输出另取 settings_view()."""
        return self.manager.settings_snapshot()

    def status(self):
        """同步状态的原结构；引导流程把它原样带进页面结果。"""
        return self.manager.status()

    @property
    def business_ready(self):
        return db.business_ready

    def _shop_count(self) -> int:
        """活动数据集内的店铺数量；没有活动数据或索引库时为 0。

        只读查询，不导入店铺业务服务。开库前先确认文件存在，避免为查询创建空索引库。
        """
        if not self.manager.active or not config.INDEX_DB.is_file():
            return 0
        with db.internal_access():
            return db.index_db().conn.execute("SELECT COUNT(*) FROM shops").fetchone()[0]

    def inspect_setup(self, target=None) -> dict:
        """同步侧的准备状态：数据集、业务就绪、待处理事项与当前目标。

        只做只读判断，不开数据库、不返回 journal 或任何锁对象；首店存在性由
        require_backup_source/require_restore_target 各自判断。
        target 为本次向导选择的 (remote, branch)，给出时可判断它是否与已保存目标一致。
        """
        manager = self.manager
        state = manager.state
        settings = manager.settings_snapshot()
        same_target = target is not None and tuple(target) == (settings["remote"], settings["branch"])
        return {
            "active": bool(manager.active),
            "dataset": manager.active["dataset"] if manager.active else None,
            "business_ready": db.business_ready,
            "started": manager.started,
            "busy": manager.busy,
            "status": manager.status_name,
            "has_error": bool(manager.error),
            "error_code": (manager.error or {}).get("code"),
            "remote": settings["remote"],
            "branch": settings["branch"],
            "index_db_present": config.INDEX_DB.is_file(),
            "same_target": same_target,
            "has_baseline": bool(state.get("baseline")),
            "has_journal": bool(state.get("journal")),
            "pending_work": any(state.get(k) for k in
                                ("pending", "session", "journal", "requires_resolution")),
            "blocked_pending": any(state.get(k) for k in
                                   ("pending", "session", "requires_resolution")),
        }

    def pending_blocks_backup(self, target) -> bool:
        """备份是否存在不属于可续接首次上传的待处理状态（备份流程的维护门槛）。"""
        manager = self.manager
        state = manager.state
        if state.get("journal"):
            return False
        if not any(state.get(k) for k in ("pending", "session", "requires_resolution")):
            return False
        same_target = tuple(target) == (manager.settings["remote"], manager.settings["branch"])
        return not (same_target and state.get("baseline"))

    @contextlib.contextmanager
    def first_use_view(self):
        """首启只读视图：非阻塞取锁，同步任务进行中时产出 None（页面保持 waiting）。

        未启动时直接给出准备状态；产出值只在关键区有效，调用方在同一关键区内完成
        自己的只读查询与阶段判断（含数据库协调锁）。
        """
        manager = self.manager
        if not manager.started or not manager.mutex.acquire(blocking=False):
            yield self.inspect_setup()
            return
        try:
            if manager.busy:
                yield None
            else:
                with db.coordinated_access():
                    yield self.inspect_setup()
        finally:
            manager.mutex.release()

    # ---------- 关键区与门禁 ----------

    @contextlib.contextmanager
    def exclusive(self):
        """一个引导动作的完整关键区；配置与初始化等子操作不再重复进入。"""
        with self.manager.operation():
            yield

    def pause_business(self):
        """暂停业务写入：绑定、恢复与冲突切换期间使用。"""
        db.set_ready(False)

    def resume_business(self, previous_ready):
        """恢复到调用前的业务门禁状态。"""
        db.set_ready(previous_ready)

    def require_rebind_allowed(self):
        """换库前的硬性门槛：待上传提交、冲突草稿与未捕获成提交的本地修改都不允许绕过。"""
        manager = self.manager
        if any(manager.state.get(k) for k in
               ("pending", "session", "journal", "requires_resolution")):
            fail("SYNC_PENDING", "请先同步未上传提交或处理冲突，再更换数据仓库",
                 user_message="还有没有上传的改动或待处理的冲突",
                 user_hint="请先完成同步或处理完冲突，再更换仓库")
        # 不能只看 pending 数量：新编辑可能尚未生成 Git 提交，用快照内容对比。
        # 与最近一次已确认上传/恢复的基线比较；committed_hash 只在本机提交时更新，
        # 从远端恢复后的干净数据用它判断会误报未上传。
        if manager.state.get("baseline") and manager.active:
            digest = manager.capture()
            if digest and digest != manager.state.get("baseline"):
                fail("SYNC_UNPUSHED", "本地快照与最近一次同步的基线不一致，请先完成同步再更换数据仓库",
                     user_message="本机还有没有上传的修改",
                     user_hint="请先点击「立即同步」上传本地修改，再更换仓库")

    def require_backup_source(self):
        """备份前必须有活动数据与首店。"""
        with db.coordinated_access():
            if not self.manager.active or not config.INDEX_DB.is_file() or not self._shop_count():
                fail("FIRST_USE_SHOP_REQUIRED", "备份前需建立活动数据与首店",
                     user_message="请先创建第一间店铺")

    def require_restore_target(self):
        """用远端数据替换本机前：不能有未处理任务，也不能覆盖已有店铺。"""
        manager = self.manager
        if any(manager.state.get(k) for k in
               ("pending", "session", "journal", "requires_resolution")):
            fail("ONBOARDING_MAINTENANCE_REQUIRED", "新手恢复拒绝未上传提交或冲突状态",
                 user_message="请先处理现有改动",
                 user_hint="进入数据同步页面完成同步或处理冲突")
        with db.coordinated_access():
            if self._shop_count():
                fail("ONBOARDING_MAINTENANCE_REQUIRED", "新手恢复不能替换已有店铺",
                     user_message="本机已有可用店铺",
                     user_hint="如需替换，请进入数据同步页面的「高级维护」")

    # ---------- 设置 ----------

    def configure(self, values):
        """在已有 exclusive() 关键区内保存设置：校验与仓库重建行为不变。"""
        return self.manager._settings_update_locked(values)

    def configure_auxiliary(self, draft, ensure_persisted=False):
        """只应用有变化的辅助设置。

        旧流程只写有变化项；新流程额外确认磁盘落盘，前次写设置失败时内存可能已
        更新，所以按读回结果决定是否再保存一次。
        """
        settings = self.manager.settings
        values = {key: draft[key] for key in ("device", "interval", "auto")
                  if draft[key] != settings[key]}
        if draft["github_account"] != settings.get("github_account", ""):
            values["github_account"] = draft["github_account"]
        if values:
            self.manager._settings_update_locked(values)
        if ensure_persisted:
            keys = ("device", "interval", "auto", "github_account")
            if not self.persisted_settings_match({key: draft[key] for key in keys}):
                self.manager._settings_update_locked({key: draft[key] for key in keys})

    def persisted_settings_match(self, values) -> bool:
        """同步包自己读设置文件判断是否已落盘，调用方不必自行拼接同步目录。"""
        try:
            saved = read_json(self.manager.directory / "settings.json", {})
        except (OSError, ValueError):
            return False
        return all(saved.get(key) == value for key, value in values.items())

    # ---------- 初始化与首次上传 ----------

    def initialize(self, source, checked_remote=None):
        """在已有 exclusive() 关键区内调用初始化主体：来源、目标复核与失败语义不变。"""
        return self.manager._initialize_body(source, checked_remote=checked_remote)

    def record_failure(self, error, previous_ready):
        """记录初始化失败并恢复业务门禁；错误码分类不变。"""
        self.manager._record_initialize_failure(error, previous_ready)

    def initial_upload_info(self, target):
        """识别可续接的首次上传，返回受控描述 {commit, hash} 或 None。

        target 为本次选择的目标 (remote, branch)。全部证明条件留在同步包内：
        目标未变、无基线/会话/待处理冲突、journal 只指向一个已提交的首个根提交、
        工作树与该提交一致且快照校验通过。不返回内部 journal 对象。
        """
        manager = self.manager
        if tuple(target) != (manager.settings["remote"], manager.settings["branch"]):
            return None
        state = manager.state
        if state.get("baseline") or state.get("session") or state.get("requires_resolution"):
            return None
        journal = state.get("journal")
        if not journal:
            return None
        commit, digest = journal.get("commit"), journal.get("hash")
        if journal.get("apply") is not False or type(journal.get("confirmed")) is not bool \
                or not commit or not digest or state.get("committed_head") != commit \
                or state.get("committed_hash") != digest or state.get("pending") != [commit]:
            return None
        store = manager.git()
        if store.head() != commit:
            return None
        if store.run("cat-file", "-e", commit + "^{commit}", check=False).returncode:
            return None
        # 首次空库上传只有一个根提交，拒绝把别的历史或额外 pending 当成它。
        if len(store.text("rev-list", "--parents", "-n", "1", commit).split()) != 1:
            return None
        work = manager.temporary("resume-verify")
        work.mkdir(parents=True)
        try:
            store.checkout_snapshot(commit, work / "snapshot")
            snapshot.validate_files(work / "snapshot")
            if snapshot.sha(work / "snapshot" / "manifest.json") != digest:
                return None
            manager._snapshot_source(digest)
            return {"commit": commit, "hash": digest}
        finally:
            remove_tree(work, manager.directory)

    def require_resumable_upload(self, info, remote_tip):
        """续接前的远端一致性检查：远端必须仍是空库或本次已确认的初始提交。"""
        if remote_tip not in (None, info["commit"]):
            fail("ONBOARDING_REMOTE_CHANGED", "远端不是空库或本次初始提交",
                 user_message="仓库里出现了新的变化",
                 user_hint="请进入数据同步页面重新核对")

    def finish_initial_upload(self, info, remote_tip):
        """实际续接首次上传：只推送已证明关联的原提交，不创建第二个根提交。

        调用前应已应用本次辅助设置：这里按新设置重建 Git 命令。推送期间新增的
        业务数据留在本机，成功后由后续正常同步上传。
        """
        manager = self.manager
        store = manager.git()
        current_digest = manager.capture(force=True)
        if remote_tip is None:
            store.push(info["commit"])
        manager.state["write_verified"] = True
        manager.state["journal"]["confirmed"] = True
        manager.save()
        manager.mark_synced(info["hash"], info["commit"])
        changed = current_digest != info["hash"]
        if changed:
            manager.invalidate_capture_cache()
        manager.error = None
        db.set_ready(True)
        return {"completed": True, "local_changes_pending": changed}

    def completed_setup_state(self, target, remote_summary):
        """已有基线但回执丢失时，核对能否读回同一结果（不再次恢复数据库）。

        返回 None 表示当前没有基线、不属于本分支；条件不符时抛出维护错误。
        """
        manager = self.manager
        state = manager.state
        if not state.get("baseline"):
            return None
        remote, branch, tip = target
        if not (tip == state.get("base_commit") and remote_summary and manager.active
                and remote_summary["dataset"] == manager.active["dataset"]
                and not any(state.get(k) for k in
                            ("pending", "session", "journal", "requires_resolution"))):
            fail("ONBOARDING_MAINTENANCE_REQUIRED", "已有基线或待处理状态与续接目标不一致",
                 user_message="备份已有新的变化",
                 user_hint="请进入数据同步页面继续处理")
        if (manager.error or {}).get("code") not in (
                None, "SYNC_STORAGE_FAILED", "GIT_FAILED", "GIT_UNAVAILABLE") \
                or not config.INDEX_DB.is_file():
            fail("ONBOARDING_MAINTENANCE_REQUIRED", "已有同步结果的数据可用性仍需处理",
                 user_message="请继续处理本机数据",
                 user_hint="进入数据同步页面查看当前状态")
        return {"tip": tip}

    def finish_completed_setup(self, tip):
        """读回已完成的同步结果并开放业务；本机后续修改照常保留。"""
        manager = self.manager
        state = manager.state
        current_digest = manager.capture(force=True)
        changed = current_digest != state["baseline"]
        manager.mark_synced(state["baseline"], tip, compact=False)
        if changed:
            manager.invalidate_capture_cache()
        manager.error = None
        db.set_ready(True)
        return {"completed": True, "local_changes_pending": changed}
