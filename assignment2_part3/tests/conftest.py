import sys
from pathlib import Path


PART3_ROOT = Path(__file__).resolve().parents[1]
PART2_ROOT = PART3_ROOT.parent / "assignment2_part2"
for path in (PART3_ROOT, PART2_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
