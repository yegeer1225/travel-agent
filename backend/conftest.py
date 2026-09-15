"""让 pytest 能 `from app.schemas import ...`（backend/ 加进 sys.path）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
