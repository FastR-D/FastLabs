"""Domain errors shared by FastLab application services."""


class RepositoryInitializationRequired(ValueError):
    """Signal that repository setup needs explicit user confirmation."""

    def __init__(self, path, reason):
        self.path = str(path)
        self.reason = reason
        if reason == "not_git":
            message = "该文件夹不是 Git 仓库。是否初始化并创建初始提交？"
        else:
            message = "该 Git 仓库还没有任何提交。是否创建初始提交？"
        super().__init__(message)


class DeliveryReverificationRequired(RuntimeError):
    """Target-side commits were reconciled and must be verified again."""


class AgentRunCancelled(RuntimeError):
    """An Agent run was cancelled before or during execution."""


class AgentRunTimeout(RuntimeError):
    """An Agent run exceeded the administrator configured timeout."""
