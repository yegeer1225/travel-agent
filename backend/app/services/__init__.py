"""服务层：与 HTTP、与存储都无关的纯逻辑。放这里的判据 —— **纯函数、可单测、零 IO**。

目前只有攻略渲染器（D61）。它值得单独一层，是因为"行程 → 攻略正文"这件事
既不属于 http（不碰 Request），也不属于 store（不碰 SQL）——
硬塞进任一边都会让那一层多背一个职责。
"""

from app.services.guide_render import RenderedGuide, render_guide_from_trip

__all__ = ["RenderedGuide", "render_guide_from_trip"]
