"""新手引导流程的编排：检查、最终应用与续接。

只理解 intent、候选检查结果、用户确认与受控同步结果；不读原 journal、
不写同步状态文件——这些都在同步包的 SyncSetup 内。
"""
from .. import db
from ..errors import ApiError, error_payload
from ..sync.common import fail
from ..sync.gitstore import GitStore
from . import drafts
from .drafts import flow_draft


class Flow:
    def __init__(self, setup, github, repository):
        self.setup = setup
        self.github = github
        self.repository = repository

    # ---------- 检查 ----------

    def _draft(self, payload):
        settings = self.setup.settings_snapshot()
        return flow_draft(payload, settings, settings["allow_file"])

    def _check_draft(self, draft):
        setup = self.setup
        if draft["intent"] == "restore":
            setup.require_restore_target()
        else:
            setup.require_backup_source()
            if setup.pending_blocks_backup((draft["remote"], draft["branch"])):
                fail("ONBOARDING_MAINTENANCE_REQUIRED", "存在非首次上传的待处理状态",
                     user_message="请先处理现有同步任务",
                     user_hint="进入数据同步页面继续处理，现有改动已保留")
        if draft["mode"] == "github-new":
            owner = self.github.verify(draft["github_account"])
            draft = {**draft, "remote": f"https://github.com/{owner}/{draft['repository']}.git",
                     "owner": owner}
            state = setup.inspect_setup()
            # 已绑定这个新目标且存在 journal 时先识别续接，不能误报为陌生数据仓库。
            if (draft["remote"], draft["branch"]) != (state["remote"], state["branch"]) \
                    or not state["has_journal"]:
                if self.github.repository_info(draft["github_account"], draft["repository"]):
                    fail("ONBOARDING_REPO_EXISTS", "创建目标已存在，请明确采用已有仓库",
                         user_message="这个仓库名已经被使用",
                         user_hint="请换一个名称，或选择「连接已有仓库」")
                return {**draft, "decision": "ready", "remote_tip": None, "snapshot": None}
        if draft["intent"] == "backup" and setup.inspect_setup()["has_journal"]:
            journal = setup.initial_upload_info((draft["remote"], draft["branch"]))
            if journal:
                tip, info = self.repository.inspect_snapshot(
                    draft["remote"], draft["branch"], draft["github_account"])
                if tip not in (None, journal["commit"]):
                    fail("ONBOARDING_REMOTE_CHANGED", "首次上传后远端已改为其他提交",
                         user_message="仓库里出现了新的变化",
                         user_hint="请进入数据同步页面重新核对，现有改动已保留")
                return {**draft, "decision": "resume_required", "remote_tip": tip, "snapshot": info}
            fail("ONBOARDING_MAINTENANCE_REQUIRED", "journal 不属于可续接的首次上传",
                 user_message="请先处理现有同步任务",
                 user_hint="进入数据同步页面继续处理")
        tip, info = self.repository.inspect_snapshot(
            draft["remote"], draft["branch"], draft["github_account"])
        state = setup.inspect_setup()
        same = (draft["remote"], draft["branch"]) == (state["remote"], state["branch"])
        if draft["intent"] == "restore":
            decision = "ready" if info else "remote_empty"
        elif same and state["has_baseline"]:
            decision = "settings_only"
        else:
            decision = "remote_has_data" if info else "ready"
        return {**draft, "decision": decision, "remote_tip": tip, "snapshot": info}

    def check(self, payload):
        return self._check_draft(self._draft(payload))

    # ---------- 结果与决策 ----------

    def _result(self, draft, *, completed=False, applied=False, created=None, error=None,
                action="none", **extra):
        return {"completed": completed, "settings_applied": applied,
                "created_repository_url": created, "error": error_payload(error) if error else None,
                "status": self.setup.status(), "resume_action": action,
                "target_remote": draft.get("remote") or "", "target_branch": draft["branch"], **extra}

    @staticmethod
    def _require_decision(check):
        if check["decision"] == "remote_has_data":
            fail("ONBOARDING_REMOTE_HAS_DATA", "新手备份拒绝覆盖有效远端数据",
                 user_message="这个仓库已有一份备份",
                 user_hint="请选择其他仓库；如需采用这份备份，请明确选择恢复")
        if check["decision"] == "remote_empty":
            fail("REMOTE_EMPTY", "恢复来源没有有效快照",
                 user_message="这个仓库尚无可恢复备份",
                 user_hint="请确认仓库地址，或先在已有数据的电脑上完成备份")

    @staticmethod
    def _storage_error():
        return ApiError("SYNC_STORAGE_FAILED", "引导保存或初始化数据失败", status=409,
                        user_message="数据保存暂未完成",
                        user_hint="请确认磁盘空间和目录访问正常后重试")

    # ---------- 最终应用 ----------

    def complete(self, payload):
        drafts.write_identity(payload)
        draft = self._draft(payload)
        settings = self.setup.settings_snapshot()
        setup = self.setup
        if payload.get("dry_run") is True:
            if (settings["remote"], settings["branch"]) != (
                    payload["expected_remote"], payload["expected_branch"]):
                fail("ONBOARDING_STALE", "dry_run 时已保存目标发生变化",
                     user_message="备份设置已更新，请刷新页面")
            check = self.check(payload)
            self._require_decision(check)
            if check["remote_tip"] != payload["remote_tip"]:
                fail("ONBOARDING_REMOTE_CHANGED", "dry_run 候选提交发生变化",
                     user_message="备份内容有新的变化，请重新检查")
            if check["decision"] not in ("settings_only", "resume_required"):
                state = setup.inspect_setup()
                if state["pending_work"] or (state["has_baseline"] and setup.status()["dirty"]):
                    fail("SYNC_PENDING", "dry_run 时存在待处理的本机改动",
                         user_message="请先完成现有数据的同步")
            return self._result(check, check=check, dry_run=True)
        previously_ready = db.business_ready
        with setup.exclusive():
            setup.pause_business()
            created, applied = None, False
            try:
                if (settings["remote"], settings["branch"]) != (
                        payload["expected_remote"], payload["expected_branch"]):
                    fail("ONBOARDING_STALE", "当前配置与打开流程时不一致",
                         user_message="备份设置已更新",
                         user_hint="请刷新页面后继续配置")
                check = self._check_draft(draft)
                draft = check
                self._require_decision(check)
                if check["remote_tip"] != payload["remote_tip"]:
                    fail("ONBOARDING_REMOTE_CHANGED", "远端提交与检查结果不一致",
                         user_message="备份内容有新的变化",
                         user_hint="请重新检查后确认")
                if check["decision"] == "resume_required":
                    setup.resume_business(previously_ready)
                    return self._result(draft, applied=True, action="retry_upload")
                if check["decision"] == "settings_only":
                    setup.configure_auxiliary(draft, ensure_persisted=True)
                    setup.resume_business(previously_ready)
                    return self._result(draft, completed=True, applied=True)
                setup.require_rebind_allowed()
                if draft["mode"] == "github-new":
                    try:
                        created, draft["remote"] = self.github.create_repository(
                            draft["github_account"], draft["repository"])
                    except ApiError as exc:
                        setup.resume_business(previously_ready)
                        return self._result(draft, error=exc, action="recheck_remote")
                values = {key: draft[key] for key in (
                    "remote", "branch", "device", "interval", "auto", "github_account")}
                setup.configure(values)
                applied = True
                setup.initialize(
                    draft["source"],
                    checked_remote=(draft["remote"], draft["branch"], payload["remote_tip"]))
                return self._result(draft, completed=True, applied=True, created=created)
            except (ApiError, OSError) as exc:
                error = exc if isinstance(exc, ApiError) else self._storage_error()
                # 设置文件可能已写成功、后续状态保存才失败；以磁盘读回而非返回码判断。
                applied = applied or setup.persisted_settings_match({
                    key: draft[key] for key in
                    ("remote", "branch", "device", "interval", "auto", "github_account")})
                setup.record_failure(error, previously_ready)
                setup.resume_business(previously_ready)
                action = "retry_check" if error.code == "ONBOARDING_REMOTE_CHANGED" else \
                    "retry_upload" if applied and setup.inspect_setup()["has_journal"] else \
                    "retry_initialize" if applied else "reuse_created" if created else "retry_check"
                return self._result(draft, applied=applied, created=created, error=error, action=action)

    # ---------- 续接 ----------

    def resume(self, payload):
        drafts.write_identity(payload)
        draft = self._draft(payload)
        settings = self.setup.settings_snapshot()
        setup = self.setup
        target = payload.get("target_remote")
        if not isinstance(target, str) or not target:
            fail("ONBOARDING_INVALID", "续接缺少 target_remote",
                 user_message="请重新检查要使用的仓库")
        GitStore.validate_remote(target, settings["allow_file"])
        # 不确定的创建结果只读核实，返回摘要后由用户再次确认采用，绝不自动再次 POST。
        if draft["mode"] == "github-new":
            info = self.github.repository_info(draft["github_account"], draft["repository"])
            owner = self.github.verify(draft["github_account"])
            expected = f"https://github.com/{owner}/{draft['repository']}.git"
            if target.lower() != expected.lower():
                fail("ONBOARDING_STALE", "创建续接目标与所选账号、仓库名不一致",
                     user_message="请核对本次选择的账号和仓库")
            if not info:
                return self._result({**draft, "remote": expected}, action="retry_check",
                                    repository_exists=False)
            if info["remote"].lower() != target.lower():
                fail("ONBOARDING_STALE", "创建结果的 clone_url 与续接目标不一致",
                     user_message="请重新检查仓库地址")
            draft = {**draft, "mode": "existing", "remote": info["remote"]}
            created = info["url"]
        else:
            created = None
            if target != draft["remote"]:
                fail("ONBOARDING_STALE", "续接目标与草稿地址不一致",
                     user_message="仓库地址已更改，请重新检查")
        current = (settings["remote"], settings["branch"])
        identity = (draft["remote"], draft["branch"])
        opened = (payload["expected_remote"], payload["expected_branch"])
        if current != identity:
            if current != opened:
                fail("ONBOARDING_STALE", "续接期间配置已更换",
                     user_message="备份设置已更新，请刷新页面")
            check = self._check_draft(draft)
            return self._result(draft, created=created,
                                action="reuse_created" if created else "retry_check",
                                check=check, dry_run=payload.get("dry_run") is True)
        previously_ready = db.business_ready
        with setup.exclusive():
            if (setup.settings_snapshot()["remote"], setup.settings_snapshot()["branch"]) != current:
                fail("ONBOARDING_STALE", "取得锁后目标发生变化",
                     user_message="备份设置已更新，请刷新页面")
            dry_run = payload.get("dry_run") is True
            if not dry_run:
                setup.pause_business()
            try:
                tip, summary = self.repository.inspect_snapshot(
                    draft["remote"], draft["branch"], draft["github_account"])
                completed = setup.completed_setup_state(
                    (draft["remote"], draft["branch"], tip), summary)
                if completed is not None:
                    if dry_run:
                        pending = setup.status()["dirty"]
                    else:
                        setup.configure_auxiliary(draft, ensure_persisted=True)
                        pending = setup.finish_completed_setup(completed["tip"])["local_changes_pending"]
                    return self._result(draft, completed=True, applied=True, created=created,
                                        dry_run=dry_run, local_changes_pending=pending)
                if draft["intent"] == "backup" and setup.inspect_setup()["has_journal"]:
                    info = setup.initial_upload_info((draft["remote"], draft["branch"]))
                    if not info:
                        fail("ONBOARDING_MAINTENANCE_REQUIRED", "首次上传 journal 关联校验失败",
                             user_message="请先处理现有同步任务",
                             user_hint="进入数据同步页面继续处理，现有改动已保留")
                    setup.require_resumable_upload(info, tip)
                    if dry_run:
                        return self._result(draft, applied=True, action="retry_upload", dry_run=True)
                    # 同目标只更新辅助设置；实际续传使用本次选中的账号，原提交与 journal 保留。
                    setup.configure_auxiliary(draft, ensure_persisted=True)
                    outcome = setup.finish_initial_upload(info, tip)
                    return self._result(draft, completed=True, applied=True, created=created,
                                        local_changes_pending=outcome["local_changes_pending"])
                setup.require_restore_target() if draft["intent"] == "restore" \
                    else setup.require_backup_source()
                if setup.inspect_setup()["blocked_pending"]:
                    fail("ONBOARDING_MAINTENANCE_REQUIRED", "存在非首次上传的 pending/journal",
                         user_message="请先处理现有同步任务",
                         user_hint="进入数据同步页面继续处理")
                check = {**draft, "remote_tip": tip, "snapshot": summary,
                         "decision": ("ready" if summary else "remote_empty")
                         if draft["intent"] == "restore"
                         else ("remote_has_data" if summary else "ready")}
                self._require_decision(check)
                if tip != payload["remote_tip"]:
                    if not dry_run:
                        setup.resume_business(previously_ready)
                    return self._result(draft, applied=True, created=created,
                                        action="retry_check", check=check)
                if dry_run:
                    return self._result(draft, applied=True, check=check, dry_run=True)
                # 同目标保留绑定与工作仓，只应用本次账号等辅助设置，再核对实际获取的提交。
                setup.configure_auxiliary(draft, ensure_persisted=True)
                setup.initialize(
                    draft["source"],
                    checked_remote=(draft["remote"], draft["branch"], payload["remote_tip"]))
                return self._result(draft, completed=True, applied=True, created=created)
            except (ApiError, OSError) as exc:
                error = exc if isinstance(exc, ApiError) else self._storage_error()
                if not dry_run:
                    setup.record_failure(error, previously_ready)
                    setup.resume_business(previously_ready)
                action = "recheck_remote" if error.code in (
                    "ONBOARDING_REMOTE_CHANGED", "ONBOARDING_MAINTENANCE_REQUIRED",
                    "SNAPSHOT_INVALID", "SCHEMA_UNSUPPORTED", "GIT_BRANCH_MISSING", "BASE_MISSING") else \
                    "retry_upload" if setup.inspect_setup()["has_journal"] else "retry_initialize"
                return self._result(draft, applied=True, created=created, error=error, action=action)
