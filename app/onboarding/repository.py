"""候选仓库的 Git 侧检查：只读取远端、导出并校验快照、读取备份摘要。

每次检查使用自己的临时目录；输入是目标与候选，不持有同步管理器。
"""
from ..storage.files import read_json
from ..sync import snapshot
from ..sync.common import fail
from ..sync.gitstore import GitStore


class Repository:
    def __init__(self, workspace, allow_file=False):
        self.workspace = workspace
        self.allow_file = allow_file

    def _store(self, work, remote, branch, account):
        return GitStore(work / "repo", remote, branch,
                        allow_file=self.allow_file, github_account=account or "")

    def probe_tip(self, remote, branch, account=""):
        """只取远端提交，不导出快照。"""
        work = self.workspace.work_dir("probe")
        try:
            return self._store(work, remote, branch, account).fetch()
        finally:
            self.workspace.cleanup(work)

    def check_candidate(self, remote, branch, account=""):
        """能连接 Git 不等于能恢复数据：导出到独立目录后按真实快照校验。"""
        work = self.workspace.work_dir("check")
        try:
            store = self._store(work, remote, branch, account)
            tip = store.fetch()
            info = None
            if tip:
                store.checkout_snapshot(tip, work / "snapshot")
                manifest = snapshot.validate_files(work / "snapshot")
                info = {"dataset": manifest["dataset"]}
            return tip, info
        finally:
            self.workspace.cleanup(work)

    def inspect_snapshot(self, remote, branch, account=""):
        """取远端提交并给出可读的备份摘要；空仓库返回 (None, None)。"""
        work = self.workspace.work_dir("flow-check")
        try:
            store = self._store(work, remote, branch, account)
            tip = store.fetch()
            info = None
            if tip:
                target = work / "snapshot"
                store.checkout_snapshot(tip, target)
                manifest = snapshot.validate_files(target)
                info = self.snapshot_summary(target, manifest, store, tip)
            return tip, info
        except (OSError, ValueError, KeyError, TypeError):
            fail("SNAPSHOT_INVALID", "候选快照摘要无法读取",
                 user_message="这份备份暂时无法读取",
                 user_hint="请确认仓库地址，或在已有数据的电脑上重新同步一次")
        finally:
            self.workspace.cleanup(work)

    @staticmethod
    def snapshot_summary(root, manifest, store, tip):
        shop_keys = {item["key"] for item in manifest["databases"] if item["kind"] == "shop"}
        summary = []
        for row in snapshot.iter_rows(root, "index", "shops"):
            key = row["sync_uid"]
            if key not in shop_keys:
                fail("SNAPSHOT_INVALID", "店铺登记与数据库清单不一致",
                     user_message="备份中的店铺信息需要重新核对",
                     user_hint="请在已有数据的电脑上重新同步一次")
            tables = read_json(root / key / "schema.json")["tables"]
            summary.append({"name": row["name"], "sku_count": tables["items"]["rows"],
                            "movement_count": tables["movements"]["rows"]})
        return {"dataset": manifest["dataset"], "shop_count": len(summary), "shops": summary,
                "sku_count": sum(row["sku_count"] for row in summary),
                "movement_count": sum(row["movement_count"] for row in summary),
                "commit_time": store.text("show", "-s", "--format=%cI", tip)}
