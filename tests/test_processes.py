"""多个 Git 调用入口共用的输入、输出与超时边界。"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import workspace


class ProcessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=workspace(), prefix="process-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        from app.platform_tools import processes

        self.processes = processes

    def run_python(self, code, **kwargs):
        return self.processes.run_hidden_memory(
            [sys.executable, "-B", "-c", code], timeout=5, **kwargs,
        )

    def check_input(self, data):
        result = self.run_python(
            "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())", data=data,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"" if data is None else data)

    def test_stdin_eof(self):
        self.check_input(None)

    def test_empty_input(self):
        self.check_input(b"")

    def test_large_binary_input(self):
        self.check_input(bytes(range(256)) * 640)

    def test_cwd_and_env(self):
        env = os.environ.copy()
        env["XM_TEST_VALUE"] = "process-test"
        result = self.run_python(
            "import os;print(os.getcwd());print(os.environ['XM_TEST_VALUE'])",
            cwd=self.directory, env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.decode().splitlines(), [str(self.directory), "process-test"],
        )

    def test_return_code_and_dual_output(self):
        result = self.run_python(
            "import os;os.write(1,b'out');os.write(2,b'err');raise SystemExit(17)",
            capture_stderr=True,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (17, b"out", b"err"))

    def test_large_dual_output(self):
        result = self.run_python(
            "import os\n"
            "for _ in range(64):\n"
            " os.write(1,b'o'*32768)\n"
            " os.write(2,b'e'*32768)",
            capture_stderr=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"o" * (64 * 32768))
        self.assertEqual(result.stderr, b"e" * (64 * 32768))

    def test_credential_output_contract(self):
        real_file = self.processes.tempfile.TemporaryFile
        writes = []

        class RecordedFile:
            def __init__(self, *args, **kwargs):
                self.file = real_file(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def __getattr__(self, name):
                return getattr(self.file, name)

            def write(self, data):
                writes.append(data)
                return self.file.write(data)

        # 只使用虚构输出，检查默认丢弃 stderr，且捕获输出没有写入临时文件。
        with patch.object(self.processes.tempfile, "TemporaryFile", RecordedFile):
            result = self.run_python("import os;os.write(1,b'fake-output');os.write(2,b'err')")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"fake-output")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(writes, [])

    def check_timeout(self, code):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.processes.run_hidden_memory(
                [sys.executable, "-B", "-c", code], timeout=0.5, capture_stderr=True,
            )
        # 给 CI 调度留余量，但不能等到子进程或后代的 8 秒休眠结束才返回。
        self.assertLess(time.monotonic() - started, 3)

    def test_timeout_running_process(self):
        self.check_timeout("import time;time.sleep(8)")

    def test_timeout_after_pipe_eof(self):
        self.check_timeout("import os,time;os.close(1);os.close(2);time.sleep(8)")

    def check_descendant(self, stdout, stderr):
        marker = self.directory / "descendant-started"
        code = (
            "import subprocess,sys\n"
            "subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(8)'],"
            f"stdin=subprocess.DEVNULL,stdout={stdout},stderr={stderr},"
            "creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))\n"
            f"open({str(marker)!r},'wb').close()"
        )
        self.check_timeout(code)
        self.assertTrue(marker.is_file(), "子进程尚未创建后代，未验证到后代持管道的场景")

    def test_timeout_descendant_stdout(self):
        self.check_descendant("None", "subprocess.DEVNULL")

    def test_timeout_descendant_stderr(self):
        self.check_descendant("subprocess.DEVNULL", "None")

    def test_timeout_descendant_both(self):
        self.check_descendant("None", "None")

    def test_real_git_result_contract(self):
        from app.errors import ApiError
        from app.sync.gitstore import GitStore

        store = GitStore.local(self.directory / "repo", device="process-test")
        result = store.run("invalid-test-command", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsInstance(result.stdout, bytes)
        self.assertIsInstance(result.stderr, bytes)
        with self.assertRaises(ApiError) as error:
            store.run("invalid-test-command")
        self.assertEqual(error.exception.code, "GIT_FAILED")
