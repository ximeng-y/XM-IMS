"""旧维护入口的编排：显式选择数据来源的检查与最终应用。

保留原「只改设置」「先关闭业务再守卫检查」与阶段性结果结构；同步侧操作
全部经 SyncSetup，不直接读 journal 或写同步状态文件。
"""
from .. import db
from ..errors import ApiError, error_payload
from ..sync.common import fail
from . import drafts
from .drafts import build_draft


class Maintenance:
    def __init__(self, setup, github, repository):
        self.setup = setup
        self.github = github
        self.repository = repository

    def _draft(self, payload):
        settings = self.setup.settings_snapshot()
        return build_draft(payload, settings, settings["allow_file"])

    # ---------- 只读候选检查 ----------

    def check(self, payload):
        draft = self._draft(payload)
        if draft["mode"] == "github-new":
            owner = self.github.verify(draft["github_account"])
            self.github.assert_repo_free(owner, draft["repository"])
            draft["remote"] = f"https://github.com/{owner}/{draft['repository']}.git"
            return {**draft, "owner": owner, "remote_tip": None, "snapshot": None,
                    "sources": ["local"]}
        tip, info = self.repository.check_candidate(
            draft["remote"], draft["branch"], draft["github_account"])
        sources = ["local"]
        if tip:
            sources = ["remote", "local"]
        return {**draft, "remote_tip": tip, "snapshot": info, "sources": sources}

    # ---------- 最终确认 ----------

    def complete(self, payload):
        if payload.get("confirm") is not True:
            raise ApiError("CONFIRM_REQUIRED", "缺少最终确认", status=409,
                           user_message="请先确认后再执行",
                           user_hint="只有明确确认后，最后一步才会应用配置")
        draft = self._draft(payload)
        setup = self.setup
        expected_remote, expected_branch = payload.get("expected_remote"), payload.get("expected_branch")
        if not isinstance(expected_remote, str) or not isinstance(expected_branch, str):
            fail("ONBOARDING_INVALID", "缺少打开向导时的 expected_remote / expected_branch",
                 user_message="向导状态不完整",
                 user_hint="请关闭向导，刷新页面后重新打开")
        previously_ready = db.business_ready
        with setup.exclusive():
            settings = setup.settings_snapshot()
            current = (settings["remote"], settings["branch"])
            if current != (expected_remote, expected_branch):
                fail("ONBOARDING_STALE", "向导打开后同步配置已发生变化，请关闭向导重新打开",
                     user_message="同步配置在向导打开后发生了变化",
                     user_hint="请关闭本向导，刷新页面后重新打开")
            # 只有连接已有仓库、地址分支都未变且已完成过首次备份时才走「仅改设置」；
            # 首次备份从未完成时，即使地址未变也必须完整走一遍绑定流程，不能报告完成。
            same_target = draft["mode"] == "existing" and (draft["remote"], draft["branch"]) == current \
                and setup.inspect_setup()["has_baseline"]
            if same_target:
                setup.configure_auxiliary(draft)
                return {"completed": True, "settings_applied": True, "created_repository_url": None,
                        "error": None, "status": setup.status()}
            # 实际绑定阶段：先关闭业务写入，再做守卫检查与联网探测，避免检查窗口内的新写入
            # 被随后的恢复或初始化覆盖；中途失败时恢复原门禁。同步任务锁不能代替业务门禁。
            setup.pause_business()
            try:
                setup.require_rebind_allowed()
                if draft["mode"] == "github-new":
                    self.github.verify(draft["github_account"])
                    self.github.assert_repo_free(draft["github_account"], draft["repository"])
                else:
                    tip = self._probe_tip(draft, payload)
                    self._require_source(draft, payload, tip)
            except ApiError:
                setup.resume_business(previously_ready)
                raise
            created_url = None
            if draft["mode"] == "github-new":
                # 副作用从这里开始：之后不再以异常形式报错，改用阶段性结果返回。
                try:
                    created_url, clone_url = self.github.create_repository(
                        draft["github_account"], draft["repository"])
                except ApiError as exc:
                    setup.resume_business(previously_ready)
                    return {"completed": False, "settings_applied": False,
                            "created_repository_url": None,
                            "error": error_payload(exc), "status": setup.status()}
                # 使用 API 返回的标准 HTTPS clone 地址，不自行拼接。
                draft["remote"] = clone_url
            values = {"remote": draft["remote"], "branch": draft["branch"], "device": draft["device"],
                      "interval": draft["interval"], "auto": draft["auto"],
                      "github_account": draft["github_account"]}
            try:
                setup.configure(values)
            except ApiError as exc:
                setup.resume_business(previously_ready)
                return {"completed": False, "settings_applied": False,
                        "created_repository_url": created_url,
                        "error": error_payload(exc), "status": setup.status()}
            try:
                source = "local" if draft["mode"] == "github-new" else payload.get("source")
                setup.initialize(source)
            except ApiError as exc:
                setup.record_failure(exc, previously_ready)
                return {"completed": False, "settings_applied": True,
                        "created_repository_url": created_url,
                        "error": error_payload(exc), "status": setup.status()}
            return {"completed": True, "settings_applied": True, "created_repository_url": created_url,
                    "error": None, "status": setup.status()}

    def _probe_tip(self, draft, payload):
        """最终应用前重新探测远端：与用户检查时看到的提交一致才允许继续。"""
        if "remote_tip" not in payload:
            fail("ONBOARDING_INVALID", "缺少候选检查结果（remote_tip）",
                 user_message="向导状态不完整",
                 user_hint="请回到「检查并确认」重新检查")
        tip = self.repository.probe_tip(draft["remote"], draft["branch"], draft["github_account"])
        if tip != payload.get("remote_tip"):
            fail("ONBOARDING_REMOTE_CHANGED", "远端内容在检查后发生了变化，请重新检查",
                 user_message="远端又有新的变化",
                 user_hint="请回到「检查并确认」重新检查后再继续")
        return tip

    @staticmethod
    def _require_source(draft, payload, tip):
        source = payload.get("source")
        if tip is None:
            # 空仓库只允许用本机数据开始；远端出现内容的变化已被提交一致性检查拦下。
            if source != "local":
                fail("ONBOARDING_SOURCE_REQUIRED", "该仓库为空，只能使用本机数据开始备份",
                     user_message="这个仓库还是空的",
                     user_hint="请选择「使用本机数据开始备份」")
            return
        if source not in ("remote", "local"):
            fail("ONBOARDING_SOURCE_REQUIRED", "远端已有数据，必须明确选择从远端恢复或使用本机数据",
                 user_message="请选择使用哪一侧的数据",
                 user_hint="请在检查结果中选择「从远端恢复」或「使用本机数据」")
