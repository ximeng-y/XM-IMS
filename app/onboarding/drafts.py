"""向导草稿的纯校验：显式接收已保存配置副本与允许的仓库能力。

不联网、不开库、不读取环境；错误码与提示保持原样。
"""
from ..sync import settings as sync_settings
from ..sync.common import fail
from ..sync.gitstore import GitStore
from .github import REPO_NAME_PATTERN, GitHub


def _settings_values(payload, settings):
    """自动开关与三个间隔字段：与同步管理器共用同一条旧客户端兼容规则。

    只纳入请求里实际存在的字段，未提交项沿用已保存设置。
    """
    submitted = {key: payload[key] for key in
                 ("auto", "local_auto", "interval", "local_interval", "cloud_interval")
                 if key in payload}
    return sync_settings.merge(settings, submitted)


def build_draft(payload, settings, allow_file) -> dict:
    """校验向导输入，返回规范化草稿（含目标地址与分支）。"""
    mode = payload.get("mode")
    if mode not in ("existing", "github-new"):
        fail("ONBOARDING_INVALID", "mode 必须是 existing 或 github-new",
             user_message="备份方式选择有误",
             user_hint="请回到「选择备份方式」重新选择")
    device = payload.get("device", settings["device"])
    merged = _settings_values(payload, settings)
    auto, local_auto = merged["auto"], merged["local_auto"]
    # 先校验本次实际提交的值，非法旧字段不能被有效的新字段遮掉。
    for key in ("interval", "local_interval", "cloud_interval"):
        if key in payload and not sync_settings.valid_minutes(payload[key]):
            fail(**sync_settings.interval_error(key))
    for key in ("local_interval", "cloud_interval"):
        if not sync_settings.valid_minutes(merged[key]):
            fail(**sync_settings.interval_error(key))
    if type(auto) is not bool or type(local_auto) is not bool:
        fail("SETTINGS_INVALID", "自动同步间隔必须是至少 1 分钟的整数",
             user_message="自动同步间隔至少为 1 分钟",
             user_hint="请填写一个不小于 1 的整数")
    if not isinstance(device, str) or not 1 <= len(device.strip()) <= 80:
        fail("SETTINGS_INVALID", "设备名称长度应为 1–80 个字符",
             user_message="设备名称需为 1–80 个字符",
             user_hint="请缩短或补全设备名称")
    github_account = payload.get("github_account") or ""
    if not isinstance(github_account, str) or len(github_account) > 100:
        fail("ONBOARDING_INVALID", "github_account 必须是不超过 100 字符的文本",
             user_message="GitHub 账号填写有误",
             user_hint="请回到「登录 GitHub」步骤重新选择账号")
    draft = {"mode": mode, "device": device.strip(),
             "local_interval": merged["local_interval"], "cloud_interval": merged["cloud_interval"],
             "interval": merged["cloud_interval"],
             "auto": auto, "local_auto": local_auto,
             "github_account": github_account, "remote": "", "branch": "main", "repository": None}
    if mode == "existing":
        remote = payload.get("remote", "")
        if not isinstance(remote, str) or not remote.strip():
            fail("SETTINGS_INVALID", "仓库地址必填",
                 user_message="请填写仓库地址",
                 user_hint="请填写以 https:// 或 git@ 开头的仓库地址")
        remote = remote.strip()
        GitStore.validate_remote(remote, allow_file)
        branch = payload.get("branch", settings["branch"])
        if not isinstance(branch, str) or not branch.strip() or branch.strip().startswith("-"):
            fail("SETTINGS_INVALID", "分支名无效",
                 user_message="分支名无效",
                 user_hint="请填写一个合法的分支名，例如 main")
        draft["remote"], draft["branch"] = remote, branch.strip()
        if not GitHub.remote_owner(remote):
            draft["github_account"] = ""
    else:
        repository = payload.get("repository")
        if not isinstance(repository, str) or not REPO_NAME_PATTERN.fullmatch(repository):
            fail("ONBOARDING_REPO_NAME", "仓库名称只能包含字母、数字、点、下划线和连字符",
                 user_message="仓库名称格式不正确",
                 user_hint="请使用字母或数字开头，只含字母、数字、点、下划线或连字符")
        draft["repository"] = repository
        if not draft["github_account"]:
            fail("ONBOARDING_GITHUB_ACCOUNT", "创建 GitHub 仓库前需要先选择已验证的账号",
                 user_message="还没有选择 GitHub 账号",
                 user_hint="请回到「登录 GitHub」步骤选择已验证的账号")
    return draft


def flow_draft(payload, settings, allow_file) -> dict:
    """新手流程的草稿：intent 决定来源，restore 只允许连接已有仓库。"""
    intent = payload.get("intent")
    if intent not in ("backup", "restore"):
        fail("ONBOARDING_INTENT", "intent 必须为 backup 或 restore",
             user_message="请选择配置备份或使用已有备份")
    draft = build_draft(payload, settings, allow_file)
    source = "local" if intent == "backup" else "remote"
    if payload.get("source", source) != source or (intent == "restore" and draft["mode"] != "existing"):
        fail("ONBOARDING_SOURCE_INVALID", "新手流程的 source/mode 与 intent 不一致",
             user_message="请重新选择要使用的备份仓库")
    return {**draft, "intent": intent, "source": source}


def write_identity(payload) -> None:
    """写操作的确认、幂等键与向导快照身份的公共校验。"""
    if type(payload.get("dry_run", False)) is not bool:
        fail("ONBOARDING_INVALID", "dry_run 必须为布尔值",
             user_message="请刷新页面后重试")
    if payload.get("confirm") is not True and payload.get("dry_run") is not True:
        fail("CONFIRM_REQUIRED", "缺少最终确认",
             user_message="请确认后继续")
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not 1 <= len(key) <= 100:
        fail("ONBOARDING_INVALID", "idempotency_key 需为 1–100 字符",
             user_message="请刷新页面后重新确认")
    if not isinstance(payload.get("expected_remote"), str) or not isinstance(payload.get("expected_branch"), str):
        fail("ONBOARDING_INVALID", "缺少 expected_remote/expected_branch",
             user_message="请刷新页面后重新打开配置")
    if "remote_tip" not in payload:
        fail("ONBOARDING_INVALID", "缺少 remote_tip",
             user_message="请先检查备份仓库")
