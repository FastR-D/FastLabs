"""FastLab's modular application package.

Public names are resolved lazily so low-level modules such as runners can be
imported without pulling in the HTTP server or creating compatibility cycles.
"""

from importlib import import_module


_EXPORTS = {
    "FastLab": (".application", "FastLab"),
    "Store": (".storage", "Store"),
    "AgentRunGate": (".scheduling", "AgentRunGate"),
    "AgentRunCancelled": (".errors", "AgentRunCancelled"),
    "AgentRunTimeout": (".errors", "AgentRunTimeout"),
    "DeliveryReverificationRequired": (".errors", "DeliveryReverificationRequired"),
    "RepositoryInitializationRequired": (".errors", "RepositoryInitializationRequired"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
