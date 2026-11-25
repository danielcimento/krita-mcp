"""Live tests run against the GUI Krita with this checkout's plugin loaded.

Everything under tests/live is skipped unless KRITA_MCP_LIVE=1.
"""

import os
from pathlib import Path

import pytest

LIVE = os.environ.get("KRITA_MCP_LIVE") == "1"
_HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="live test: set KRITA_MCP_LIVE=1 (needs a running Krita)")
    for item in items:
        if _HERE in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.live)
            if not LIVE:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def krita_url():
    return os.environ.get("KRITA_URL", "http://localhost:5678").rstrip("/")
