"""同步设置的缺省值、旧字段兼容与规范化纯函数。

管理器载入与写入设置、向导草稿都调用这里的同一条规则，避免不同入口各自解释
旧版本的 auto 字段。函数只返回新的字典，不修改调用方传入的对象。
"""

# auto 表示「自动将备份同步到云端」，local_auto 表示「本地自动备份」。
# 新配置两者都默认关闭；旧配置缺少 local_auto 时由 normalize 按旧 auto 推导。
DEFAULTS = {"remote": "", "branch": "main", "auto": False, "local_auto": False,
            "interval": 10, "device": "本机", "github_account": ""}


def normalize(saved) -> dict:
    """载入已保存设置：补缺省值，并按旧配置的 auto 推导 local_auto。

    旧版本只有一个自动开关。缺少 local_auto 时若直接取默认值 true，
    曾经关闭自动同步的用户升级后会被静默开启本地自动备份，因此沿用旧 auto 的取值。
    """
    values = dict(saved or {})
    settings = {key: (values[key] if key in values else value) for key, value in DEFAULTS.items()}
    for key, value in values.items():
        if key not in DEFAULTS:
            settings[key] = value
    if "local_auto" not in values:
        old_auto = values.get("auto")
        settings["local_auto"] = old_auto if type(old_auto) is bool else DEFAULTS["local_auto"]
    return settings


def merge(current, values) -> dict:
    """合并一次设置写入。

    只提交 auto（旧客户端）时同时写入 local_auto，保留原「总自动开关」的效果；
    只提交 local_auto 时不改动 auto；两者都提交时分别生效。
    """
    updated = {**current, **values}
    if "auto" in values and "local_auto" not in values:
        updated["local_auto"] = values["auto"]
    return updated
