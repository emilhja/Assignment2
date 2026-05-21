"""Single import-path shim. Part 2 is imported by name everywhere else."""

import sys
from pathlib import Path

_PART2_DIR = Path(__file__).resolve().parent.parent / "assignment2_part2"
if str(_PART2_DIR) not in sys.path:
    sys.path.insert(0, str(_PART2_DIR))
