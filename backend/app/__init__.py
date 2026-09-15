"""`app` 包。

⚠️ 这个文件**故意留空**。它的存在不是为了让 import 能工作（Python 3.3+ 的
命名空间包不需要它），而是为了让**工具链把它当普通包**：

- `pytest` 的 rootdir 推断、`coverage` 的路径归因
- 打包时 `setuptools.find_packages()` 会跳过没有 `__init__.py` 的目录

改动这里之前先确认没有别的地方依赖"它是个空包"。
"""
