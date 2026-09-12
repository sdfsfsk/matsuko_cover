"""Import the plugin from this checkout with an isolated AstrBot data root."""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

_PREVIOUS_ROOT = os.environ.get("ASTRBOT_ROOT")
_TEST_ROOT = TemporaryDirectory(prefix="matsuko-cover-tests-")
os.environ["ASTRBOT_ROOT"] = _TEST_ROOT.name
package = ModuleType("matsuko_cover_under_test")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package


def pytest_sessionfinish():
    """Restore the caller's runtime root after the tests finish."""
    if _PREVIOUS_ROOT is None:
        os.environ.pop("ASTRBOT_ROOT", None)
    else:
        os.environ["ASTRBOT_ROOT"] = _PREVIOUS_ROOT
    _TEST_ROOT.cleanup()
