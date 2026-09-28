"""Domain errors shared by FastLab application services."""


class DeliveryReverificationRequired(RuntimeError):
    """Target-side commits were reconciled and must be verified again."""


class AgentRunCancelled(RuntimeError):
    """An Agent run was cancelled before or during execution."""


class AgentRunTimeout(RuntimeError):
    """An Agent run exceeded the administrator configured timeout."""
