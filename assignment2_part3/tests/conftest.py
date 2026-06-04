import os
import sys
from pathlib import Path

import pytest


PART3_ROOT = Path(__file__).resolve().parents[1]
PART2_ROOT = PART3_ROOT.parent / "assignment2_part2"
for path in (PART3_ROOT, PART2_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def _legacy_stall_reply_default(monkeypatch):
    """Default SUPPRESS_STALL_REPLIES to "0" for tests.

    Production defaults to suppressing terminal stall/step-budget fallbacks
    (run_peer_task returns the silence sentinel). Many existing tests assert on
    the returned fallback *string*, so the suite keeps the legacy non-suppressed
    behavior by default. Tests that exercise suppression set the env to "1"
    explicitly, which overrides this fixture.
    """

    if "SUPPRESS_STALL_REPLIES" not in os.environ:
        monkeypatch.setenv("SUPPRESS_STALL_REPLIES", "0")
