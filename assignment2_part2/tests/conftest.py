import sys
from pathlib import Path


PART2_ROOT = Path(__file__).resolve().parents[1]
if str(PART2_ROOT) not in sys.path:
    sys.path.insert(0, str(PART2_ROOT))
