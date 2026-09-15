"""全局配置。**唯一读 `os.environ` 的地方** —— 其余模块一律 `from app.config import settings`。

三条纪律：

1. **代码里不出现默认密钥。** 缺了直接 `raise`，不静默兜底。
   静默默认值是"本地能跑、换台机器就炸"的经典来源，而它偏偏只在部署时才暴露。
2. `.env` 在**项目根**（不是 `backend/`）—— 它是全项目共用的，
   包括 `docker-compose.yml`（compose 直接读同一个文件，见 `docker-compose.yml` 顶部注释）。
3. 模型名 / 思考开关**只从这里出**，调用处不许写死字符串。
   否则"换模型"就变成全局搜索替换，那是 D9 明确要避免的事。

关于「思考开关」为什么是字符串而不是 bool：它的取值要**原样**塞进
`ChatOpenAI(extra_body={"thinking": {"type": ...}})`，DeepSeek 只认
`"enabled"` / `"disabled"` 这两个字面量。中间转一道 bool 只会多一层翻译。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# backend/app/config.py -> backend/app -> backend -> 项目根
ROOT = Path(__file__).resolve().parents[2]

load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    """配置缺失或非法。

    **启动即失败，不要拖到运行时** —— 一个请求跑到一半才发现没 key，
    用户已经等了 30 秒，而且日志里看到的是业务报错，不是配置报错。
    """


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"缺少配置项 {name}。检查 {ROOT / '.env'}（模板见 .env.example）"
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ══════════════════════════════════════════════════════════════
#  模型能力表 —— D9「切换模型做三个粒度」的实现依据
#
#    · 换模型名            → 改 .env 的 LLM_MODEL_TOOL / LLM_MODEL_PLAN
#    · 换某节点的思考开关   → 改 .env 的 LLM_THINKING_TOOL / LLM_THINKING_PLAN
#    · 换整个供应商        → 改 LLM_BASE_URL + LLM_API_KEY（+ 上面 4 行）
#
#  ⚠️ 官方**没有** `deepseek-v4.1-flash` 这个名字（2026-09-15 实测确认）
# ══════════════════════════════════════════════════════════════

MODEL_REGISTRY: dict[str, dict[str, object]] = {
    "deepseek-flash": {"provider": "deepseek", "supports_thinking": True},
    "deepseek-v4-pro": {"provider": "deepseek", "supports_thinking": True},
    "qwen-plus": {"provider": "bailian", "supports_thinking": False},
}


@dataclass(frozen=True)
class Settings:
    # ---- LLM ----
    llm_api_key: str
    llm_base_url: str
    llm_model_tool: str
    """**带 tools 的节点**用这个模型。"""
    llm_model_plan: str
    """**不挂 tools 的节点**（规划 / 生成）用这个模型。"""
    llm_thinking_tool: str
    """带 tools 的节点必须 `disabled`。理由 = **成本 + 确定性**，**不是**"会 400"
    （那条 2026-09-15 晚复测未复现，见 D8）：
    `reasoning_tokens` 计入 `completion_tokens`（实测推理占 160/163），
    且思考模式下 `temperature` 失效、关掉后 `temperature=0` 才生效。"""
    llm_thinking_plan: str
    """不挂 tools 的节点可以 `enabled`，思考真实生效且能保住 CoT。"""

    # ---- 高德 ----
    amap_provider: str
    """`mock` 或 `real`。**前端无感知**（D23）：接口路径与响应结构完全不变。"""
    amap_webservice_key: str
    """后端「Web服务」Key。前端 JS Key 是**另一个**，不在这里 —— 它由 Vite 注入前端。"""

    # ---- MySQL ----
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str = field(repr=False)
    """`repr=False`：防止密码出现在日志 / 异常堆栈 / notebook 输出里。"""
    mysql_db: str = "travel_agent"

    # ---- 派生量 ----
    @property
    def mock_mode(self) -> bool:
        """是不是在跑 mock。`api.md` 1.4 承诺前端**不需要**判断这个，
        它只用于 `GET /health` 的 `mock_mode` 字段和启动日志。"""
        return self.amap_provider != "real"

    @property
    def tool_thinking_enabled(self) -> bool:
        return self.llm_thinking_tool.lower() == "enabled"

    @property
    def plan_thinking_enabled(self) -> bool:
        return self.llm_thinking_plan.lower() == "enabled"

    def thinking_body(self, *, with_tools: bool) -> dict[str, dict[str, str]]:
        """构造 `extra_body={"thinking": {"type": ...}}`。

        不让调用方自己拼字符串，因为拼错一个字面量（`"disable"` vs `"disabled"`）
        服务端**不会报错** —— 它会静默按默认值跑，而默认是**开**的。
        传 `with_tools=True` 就自动取 `LLM_THINKING_TOOL`。

        ⚠️ DeepSeek 思考模式下 `temperature` 失效（官方原话 "will also have no effect"），
        所以**不要**在开思考的节点上调 temperature —— 调了也不报错，只是没效果。
        """
        value = self.llm_thinking_tool if with_tools else self.llm_thinking_plan
        return {"thinking": {"type": value}}

    # ---- DSN（同步 / 异步两套，checkpoint 用得到）----
    @property
    def mysql_dsn(self) -> str:
        """同步 DSN —— `langgraph-checkpoint-mysql` 的 `PyMySQLSaver` 用。"""
        return (
            f"mysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
        )

    @property
    def mysql_dsn_async(self) -> str:
        """异步 DSN —— `AIOMySQLSaver` 用。驱动段必须写 `mysql+aiomysql`（实测）。"""
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
        )


def _build() -> Settings:
    provider = _optional("AMAP_PROVIDER", "mock").lower()
    if provider not in {"mock", "real"}:
        raise ConfigError(f"AMAP_PROVIDER 只能是 mock 或 real，现在是 {provider!r}")

    model_tool = _optional("LLM_MODEL_TOOL", "deepseek-flash")
    model_plan = _optional("LLM_MODEL_PLAN", "deepseek-flash")

    # 模型名打错不会在启动时报错（网络请求时才 404），所以这里先拦一道粗筛：
    # 不在能力表里**不阻止启动**（可能人家刚出了新模型），但必须能看见。
    unknown = [m for m in {model_tool, model_plan} if m not in MODEL_REGISTRY]
    if unknown:
        print(
            f"[config] ⚠️ 模型 {'、'.join(unknown)} 不在 MODEL_REGISTRY 里，"
            f"无法判断是否支持思考模式 —— 请确认 .env 的模型名拼写"
        )

    thinking_tool = _optional("LLM_THINKING_TOOL", "disabled").lower()
    if thinking_tool == "enabled":
        # 守卫保留，理由已更新（2026-09-15 晚复测）：
        # **不是**为了躲 400（那条未复现），而是 ——
        #   · 成本：reasoning_tokens 计入 completion_tokens，工具循环 ≤8 轮会多烧数倍
        #   · 确定性：思考模式下 temperature 失效；关掉后 temperature=0 才生效
        raise ConfigError(
            "LLM_THINKING_TOOL 不能是 enabled —— 工具循环节点必须关思考"
            "（成本 + 确定性，详见 DECISIONS.md D8）"
        )

    return Settings(
        llm_api_key=_require("LLM_API_KEY"),
        llm_base_url=_optional("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        llm_model_tool=model_tool,
        llm_model_plan=model_plan,
        llm_thinking_tool=thinking_tool,
        llm_thinking_plan=_optional("LLM_THINKING_PLAN", "enabled").lower(),
        amap_provider=provider,
        # ⚠️ mock 模式下不校验高德 Key：M1 阶段（以及豆包并行开工时）不需要真 Key
        amap_webservice_key=(
            _require("AMAP_WEBSERVICE_KEY")
            if provider == "real"
            else _optional("AMAP_WEBSERVICE_KEY")
        ),
        mysql_host=_optional("MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(_optional("MYSQL_PORT", "3306")),
        mysql_user=_optional("MYSQL_USER", "travel"),
        mysql_password=_optional("MYSQL_PASSWORD"),
        mysql_db=_optional("MYSQL_DB", "travel_agent"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """带缓存 —— `.env` 在进程生命周期内不会变，重复读盘没意义。
    测试里要换配置就 `get_settings.cache_clear()`。"""
    return _build()


settings = get_settings()


__all__ = [
    "ROOT",
    "ConfigError",
    "MODEL_REGISTRY",
    "Settings",
    "get_settings",
    "settings",
]
