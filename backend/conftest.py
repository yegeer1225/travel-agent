"""让 pytest 能 `from app.schemas import ...`（backend/ 加进 sys.path）。

同时把 `tests/` 也加进去 —— 这样 `from fakes import ScriptedChatModel`
在"直接跑单个测试文件"和"整套跑"两种情况下行为一致。
只依赖 pytest 的自动插桩会让前者时灵时不灵。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))
