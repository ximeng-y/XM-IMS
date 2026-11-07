"""备份配置引导的门面：给 API 使用的薄分派层。

各公开方法交给具体组件：环境检测、GitHub 账号、候选仓库、同步准备接口。
安装、显式登录与最终应用在同步任务锁内执行，其余检查只读。
"""
from ..sync.setup import SyncSetup
from .environment import Environment
from .flow import Flow
from .github import GitHub
from .maintenance import Maintenance
from .repository import Repository
from .workspace import Workspace


class BackupOnboarding:
    """备份配置引导服务；每个后端实例一个，持有已组装的具体组件。"""

    def __init__(self, manager, workspace=None):
        self.manager = manager
        self.setup = SyncSetup(manager)
        self.workspace = workspace or Workspace()
        self.environment_service = Environment(self.workspace)
        self.github_service = GitHub(manager.allow_file)
        self.repository = Repository(self.workspace, manager.allow_file)
        self.maintenance = Maintenance(self.setup, self.github_service, self.repository)
        self.flow = Flow(self.setup, self.github_service, self.repository)

    # ---------- 环境与安装 ----------

    def environment(self):
        return self.environment_service.environment()

    def install(self, component):
        with self.setup.exclusive():
            return self.environment_service.install(component)

    # ---------- GitHub 账号 ----------

    def github_status(self, account=None):
        return self.github_service.status(account)

    def github_login(self, account, force):
        with self.setup.exclusive():
            return self.github_service.login(account, force)

    # ---------- 旧维护流程（显式选择数据来源） ----------

    def check(self, payload):
        return self.maintenance.check(payload)

    def complete(self, payload):
        return self.maintenance.complete(payload)

    # ---------- 新手流程 ----------

    def check_flow(self, payload):
        return self.flow.check(payload)

    def complete_flow(self, payload):
        return self.flow.complete(payload)

    def resume_flow(self, payload):
        return self.flow.resume(payload)
