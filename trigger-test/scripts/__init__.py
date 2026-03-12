"""
Test script registry for the trigger longevity test tool.

Each test script is an async function decorated with @test_script("name", "description").
Scripts are auto-discovered from all modules in this package.
"""

import importlib
import pkgutil
from pathlib import Path

# Registry: name → (function, description)
_scripts: dict[str, tuple] = {}


def test_script(name: str, description: str):
    """Decorator to register a test script."""
    def decorator(func):
        _scripts[name] = (func, description)
        return func
    return decorator


def get_script(name: str):
    """Return (function, description) for a named script, or None."""
    return _scripts.get(name)


def list_scripts() -> list[tuple[str, str]]:
    """Return sorted list of (name, description) for all registered scripts."""
    return sorted((name, desc) for name, (_, desc) in _scripts.items())


def discover():
    """Import all modules in this package to trigger @test_script registrations."""
    pkg_path = Path(__file__).parent
    for info in pkgutil.iter_modules([str(pkg_path)]):
        importlib.import_module(f".{info.name}", __package__)
