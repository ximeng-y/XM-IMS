"""同步设置的缺省值、旧字段兼容与规范化纯函数。

管理器载入与写入设置、向导草稿都调用这里的同一条规则，避免不同入口各自解释
旧版本的 auto 与 interval 字段。函数只返回新的字典，不修改调用方传入的对象。
"""

# auto 表示「自动将备份同步到云端」，local_auto 表示「本地自动备份」。
# 新配置两者都默认关闭；旧配置缺少 local_auto 时由 normalize 按旧 auto 推导。
# local_interval 与 cloud_interval 分别控制两个后台阶段；interval 只作云端兼容镜像。
DEFAULTS = {"remote": "", "branch": "main", "auto": False, "local_auto": False,
            "interval": 10, "local_interval": 10, "cloud_interval": 10,
            "device": "本机", "github_account": ""}


def normalize(saved) -> dict:
    """载入已保存设置：补缺省值，并按旧配置推导 local_auto 与两个间隔。

    旧版本只有一个自动开关。缺少 local_auto 时若直接取默认值 true，
    曾经关闭自动同步的用户升级后会被静默开启本地自动备份，因此沿用旧 auto 的取值。
    缺少某个新间隔时沿用旧的 interval，避免升级后两个阶段被重置为默认值。
    """
    values = dict(saved or {})
    settings = {key: (values[key] if key in values else value) for key, value in DEFAULTS.items()}
    for key, value in values.items():
        if key not in DEFAULTS:
            settings[key] = value
    if "local_auto" not in values:
        old_auto = values.get("auto")
        settings["local_auto"] = old_auto if type(old_auto) is bool else DEFAULTS["local_auto"]
    legacy_interval = values.get("interval", DEFAULTS["interval"])
    for key in ("local_interval", "cloud_interval"):
        if key not in values:
            settings[key] = legacy_interval
    # 旧字段只作兼容镜像：始终跟随云端间隔，不让保存过的旧值把两项重新联动。
    settings["interval"] = settings["cloud_interval"]
    return settings


def merge(current, values) -> dict:
    """合并一次设置写入。

    只提交 auto（旧客户端）时同时写入 local_auto，保留原「总自动开关」的效果；
    只提交 local_auto 时不改动 auto；两者都提交时分别生效。
    提交旧 interval（旧客户端）时作为两项间隔的共同值，同次提交的新字段分别覆盖，
    兼容镜像始终由最终的云端间隔生成。
    """
    updated = {**current, **values}
    if "auto" in values and "local_auto" not in values:
        updated["local_auto"] = values["auto"]
    if "interval" in values:
        updated["local_interval"] = values["interval"]
        updated["cloud_interval"] = values["interval"]
    for key in ("local_interval", "cloud_interval"):
        if key in values:
            updated[key] = values[key]
    updated["interval"] = updated["cloud_interval"]
    return updated


def valid_minutes(value) -> bool:
    """间隔的合法取值：精确整数且至少为 1；布尔、浮点、字符串与空值都不合法。"""
    return type(value) is int and value >= 1


def interval_error(key) -> dict:
    """间隔字段非法时的双轨错误内容；管理器与向导共用同一套文案。

    旧 interval 保持原有文案；两个新字段分别指明是哪一个间隔填错。
    """
    if key == "interval":
        return {"code": "SETTINGS_INVALID",
                "message": "自动同步间隔必须是至少 1 分钟的整数",
                "user_message": "自动同步间隔至少为 1 分钟",
                "user_hint": "请填写一个不小于 1 的整数"}
    label = {"local_interval": "本地备份间隔", "cloud_interval": "上传云端间隔"}[key]
    return {"code": "SETTINGS_INVALID",
            "message": f"{label}必须是至少 1 分钟的整数",
            "user_message": f"{label}至少为 1 分钟",
            "user_hint": "请填写一个不小于 1 的整数"}
