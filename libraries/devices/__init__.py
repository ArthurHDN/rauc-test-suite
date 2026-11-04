"""Device plugin registry.

Every module in this package (except ``base`` and modules starting with ``_``) is imported on first
use; each :class:`~devices.base.DevicePlugin` subclass that lists names in ``devices`` is registered
under those names.  Adding a device therefore only requires dropping a module into this directory.
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import DevicePlugin

_loaded = False


def _load_modules() -> None:
    global _loaded
    if _loaded:
        return
    for info in pkgutil.iter_modules(__path__):
        if info.name == "base" or info.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{info.name}")
    _loaded = True


def _subclasses(cls: type) -> list[type]:
    found = []
    for sub in cls.__subclasses__():
        found.append(sub)
        found.extend(_subclasses(sub))
    return found


def registry() -> dict[str, type[DevicePlugin]]:
    """``{device name: plugin class}`` of all installed plugins."""
    _load_modules()
    result: dict[str, type[DevicePlugin]] = {}
    for cls in _subclasses(DevicePlugin):
        for name in cls.devices:
            if name in result and result[name] is not cls:
                raise RuntimeError(
                    f"Device '{name}' is provided by both {result[name].__module__} and {cls.__module__}"
                )
            result[name] = cls
    return result


def list_devices() -> list[tuple[str, str]]:
    """Sorted ``(name, description)`` pairs."""
    return sorted((name, cls.description) for name, cls in registry().items())


def get_plugin_class(device: str) -> type[DevicePlugin]:
    plugins = registry()
    if device not in plugins:
        raise AssertionError(
            f"Unknown device '{device}'. Available devices: {', '.join(sorted(plugins)) or 'none'} "
            "(see DEVICES.md to add one)"
        )
    return plugins[device]
