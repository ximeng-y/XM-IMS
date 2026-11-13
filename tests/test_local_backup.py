"""无远端的新店铺：首次备份、超时重试、幂等与 Git 提取。"""
import sys
import unittest
from unittest.mock import patch

from tests import workspace


class LocalBackupRegressionTests(unittest.TestCase):
    def test_first_local_backup_timeout_retry_and_extract(self):
        directory = workspace()
        from app.errors import ApiError
        from app.onboarding.first_use import FirstUse
        from app.schemas import FirstShopCreate
        from app.sync import gitstore, snapshot
        from app.sync.manager import SyncManager
        from app.sync.setup import SyncSetup

        manager = SyncManager()
        self.addCleanup(manager.stop)
        manager.start()
        shop_name = "backup-regression"
        created = FirstUse(SyncSetup(manager)).create_shop(
            FirstShopCreate(name=shop_name, confirm=True, idempotency_key="first-shop"),
        )
        self.assertTrue(created["created"])
        self.assertFalse(manager.status()["configured"])
        real_run = gitstore.run_hidden_memory

        def force_timeout(argv, **kwargs):
            kwargs["timeout"] = 0.5
            return real_run(
                [sys.executable, "-B", "-c", "import time;time.sleep(8)"], **kwargs,
            )

        # 只替换失败的这一次进程调用；后面的重试、提交和提取全部使用真实 Git。
        with patch.object(gitstore, "run_hidden_memory", side_effect=force_timeout):
            with self.assertRaises(ApiError) as error:
                manager.local_backup(idempotency_key="first-backup")
        self.assertEqual(error.exception.code, "GIT_UNAVAILABLE")
        status = manager.status()
        self.assertFalse(status["busy"])
        self.assertEqual(status["local_backup"]["total"], 0)
        self.assertIsNone(status["error"])

        saved = manager.local_backup(idempotency_key="first-backup")
        self.assertTrue(saved["created"])
        status = manager.status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["busy"])
        self.assertIsNone(status["local_error"])
        self.assertEqual(status["local_backup"]["total"], 1)
        self.assertTrue(status["local_backup"]["current_backed_up"])

        replay = manager.local_backup(idempotency_key="first-backup")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["backup"]["id"], saved["backup"]["id"])
        unchanged = manager.local_backup(idempotency_key="unchanged-backup")
        self.assertFalse(unchanged["created"])
        self.assertEqual(manager.backup_page()["total"], 1)

        record, source = manager.backups.extract(saved["backup"]["id"], directory / "extract")
        self.assertEqual(snapshot.sha(source / "manifest.json"), record["digest"])
        snapshot.validate_files(source)
        self.assertEqual(
            [row["name"] for row in snapshot.iter_rows(source, "index", "shops")], [shop_name],
        )
        self.assertFalse((source / ".git").exists())
