"""高德 Web 服务实现（真实数据源）。

**事实来源**：`scripts/probe_amap.py`（2026-09-15 实测，27 次请求，样本存 `tests/fixtures/amap/`）。
本文件里每条"实测"注释都能被那个脚本复现 —— 如果高德改了行为，先跑探针再改这里。

## 三个接口的分工

| 方法 | 高德接口 | 关键实测事实 |
|---|---|---|
| `search_poi` | `/v3/place/text` | POI 字段 46 个（MCP 层只给 5 个）；`biz_ext` 的键**随类别变**；**几乎不会返回空** |
| `get_weather` | `/v3/geocode/geo` → `/v3/weather/weatherInfo` | 预报**从今天起 4 天**（文档写 3 天）；参数要 `adcode` 不是城市名 |
| `calc_distance` | `/v3/distance` | **带 `duration`**，不用再调路径规划；`type=1` 驾车 / `type=0` 直线（duration 恒为 0） |

## 🔴 本文件要挡住的四个坑（全部实测过）

1. **限流不抛异常** —— 超 QPS 时 HTTP 仍是 200，只是 `status=0` + `info=CUQPS_HAS_EXCEEDED_THE_LIMIT`
   + `infocode=10021`。不识别它就会把"被限流"当成"搜不到" → 误判景点不存在 → 白重排整份行程。
   （实测复现两次：连续请求第 3~4 个开始失败，窗口滑过后自动恢复 → 是滑动窗口不是封禁）
2. **`biz_ext` 的键是变长的** —— 餐饮/咖啡：`cost`/`meal_ordering`/`open_time`/`opentime2`/`rating`；
   酒店：多 `hotel_ordering`/`lowest_price`/`star`；地铁站：只有 `cost`/`open_time`/`opentime2`/`rating`。
   → **所有字段一律 `.get()`**，不能假定存在。
3. **标量的"没有"有多种写法** —— `[]`（空数组）、`''`、`'[]'`（空数组的字符串形式）、`None`。
   → 必须**显式判类型**收敛成 `None`。用 `if not value` 会把 `0` 也一起吞掉。
4. **"搜不到"几乎不存在** —— 实测拿乱码当关键词（`zzz不存在的关键词xyz`）也返回 3 条
   （茶馆/服装店/培训机构）。真实高德是模糊语义检索，**不是名称包含匹配**。
   → 意味着"候选池为空"在真实环境下极罕见；而**候选池混入噪音**才是常态。
   → `search_poi` 返回空列表时，调用方要意识到那几乎一定是别的原因（限流/参数错）。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import date
from typing import Any

import httpx

from app.providers.base import DistanceResult
from app.schemas import AmapPoi, Weather, WeatherStatus

logger = logging.getLogger(__name__)

BASE_URL = "https://restapi.amap.com"

# ══════════════════════════════════════════════════════════════
#  出站限速
# ══════════════════════════════════════════════════════════════

MIN_INTERVAL_S = 0.45
"""两次出站请求的最小间隔。约 2.2 QPS。

**为什么不按文档写 3 QPS**：实测连续打（无间隔）时第 3~4 个就炸 —— 单人 Key 的实际上限
贴着 3 QPS 边界，而窗口边界受网络抖动影响。留一倍余量比"精确贴着上限然后靠重试救"便宜：
一次限流重试要等 1s，而多等 0.1s 什么都不会发生。

⚠️ 这是**进程内**限速（D28）。多进程部署时每个进程各自计数 → 实际 QPS 会翻倍，
到 M5 上多 worker 时要换成 Redis 令牌桶。**现在不引 Redis 是刻意的**（单进程够用）。
⚠️ 状态必须是**模块级**（见下方 `_next_slot_ts`）—— 放实例级会漏，因为 provider 每请求新建。"""

_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)
"""重试退避（D26）。GET 天然幂等，所以可以无脑重试。"""

_RETRYABLE_INFOCODES = {
    "10021",  # CUQPS_HAS_EXCEEDED_THE_LIMIT —— 实测。秒级 QPS 超限，等一会就好
    "10019",  # 文档：CUQPS_HAS_EXCEEDED_THE_LIMIT 的另一种码（未实测，保守一起重试）
}
"""可重试的业务错误码。**只放"等一等就会好"的**。"""

_FATAL_INFOCODES = {
    "10001": "INVALID_USER_KEY —— Key 不对/过期/不是这个平台的",
    "10002": "SERVICE_NOT_AVAILABLE —— 这个 Key 没有该服务的权限",
    "10003": "USER_DAILY_QUERY_OVER_LIMIT —— **日配额**用完（重试无用，等明天）",
    "10004": "USERKEY_PLAT_NOMATCH —— Key 与平台不匹配",
    "10007": "INVALID_PARAMS —— 参数错（本项目自己写错了）",
    "10009": "USERKEY_PLAT_NOMATCH —— Key 与平台不匹配",
    "10012": "INSUFFICIENT_PRIVILEGES —— 权限不足",
}
"""重试无意义、必须立刻让人看见的错误码 → 直接抛 `AmapError`。

⚠️ 上面这些码是**文档值**，本项目的实测只复现过 `10021`。
   把它们列出来是为了让报错信息**可读**（而不是显示"infocode=10001"让人去查文档），
   但不要声称"实测过"。"""


class AmapError(RuntimeError):
    """高德返回了重试无用的业务错误。

    **不要捕获它然后返回空结果** —— 那会把"Key 过期"变成"这个景点搜不到"，
    是本文件开头列的第 1 号坑的另一种形态。
    """

    def __init__(self, infocode: str, info: str, path: str) -> None:
        hint = _FATAL_INFOCODES.get(infocode, "")
        detail = f"高德接口 {path} 返回错误：infocode={infocode} info={info!r}"
        if hint:
            detail += f"\n  → {hint}"
        super().__init__(detail)
        self.infocode = infocode
        self.info = info
        self.path = path


# ══════════════════════════════════════════════════════════════
#  字段清洗 —— 高德的类型不稳定全在这里收敛掉
# ══════════════════════════════════════════════════════════════


def _text(value: Any) -> str | None:
    """把标量字段收敛成 `str | None`。

    高德表示"这个字段没有"有四种写法：`None` / `''` / `[]` / **`'[]'`**（空数组的字符串形式）。
    最后一种最阴 —— 它 truthy、非空、长度 2，一路流到界面上就会显示成 `[]`。
    """
    if value is None or isinstance(value, (list, dict)):
        return None
    text = str(value).strip()
    if not text or text in {"[]", "{}", "null", "None"}:
        return None
    return text


def _num(value: Any) -> float | None:
    """`'73.00'` → `73.0`；`[]` → `None`。

    ⚠️ 返回 `None` 和返回 `0.0` 是**两件事**：`cost=None` 是"没数据"（界面上该隐藏），
    `cost=0.0` 是"免费"。所以不能把空值兜底成 0。
    """
    text = _text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _as_list(value: Any) -> list[str]:
    """`alias` 实测有两种形态：`'武侯祠|武侯祠景区'`（`|` 分隔的字符串）和 `[]`。

    契约（`schemas.py`）要求 `list[str]`，所以这里必须把两种都吃下来。
    """
    if value is None or isinstance(value, dict):
        return []
    if isinstance(value, list):
        return [t for t in (_text(v) for v in value) if t]
    return [t for t in (p.strip() for p in str(value).split("|")) if t]


def _parse_location(value: Any) -> tuple[float, float] | None:
    """`'104.047992,30.646168'` → `(104.047992, 30.646168)`（**lng 在前**）。

    高德全站都是 `lng,lat` 顺序，和 GeoJSON 的 `lat,lng` 相反 —— 这是最容易搞反的地方，
    所以本项目的所有坐标一律显式声明顺序，且用 `(lng, lat)` 元组而不是裸 dict。
    """
    text = _text(value)
    if text is None:
        return None
    parts = text.split(",")
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def _parse_photos(value: Any) -> list[str]:
    """`[{'title': [], 'url': 'http://...'}, ...]` → `['http://...', ...]`。

    实测每个 POI 都有 3 张；`title` 恒为 `[]`，所以只取 `url`。
    """
    if not isinstance(value, list):
        return []
    urls: list[str] = []
    for item in value:
        if isinstance(item, dict):
            url = _text(item.get("url"))
            if url:
                urls.append(url)
    return urls


def parse_poi(raw: dict[str, Any]) -> AmapPoi | None:
    """把一条高德原始 POI 转成契约模型。**拿不到必要字段就返回 `None`**（跳过这条，不是整批失败）。

    必要条件只有三个：`id` / `name` / `location`。
    缺 id → 封闭世界校验没法用它；缺坐标 → 算不了距离。缺哪个都等于这条数据没用。

    ⚠️ 其余字段**全部可选**，且一律走 `_text` / `_num` 收敛 —— 高德的 `biz_ext` 是变长键的，
       餐饮有 `cost`、酒店有 `lowest_price`、地铁站什么都没有。
    """
    poi_id = _text(raw.get("id"))
    name = _text(raw.get("name"))
    coord = _parse_location(raw.get("location"))
    if not poi_id or not name or coord is None:
        return None
    lng, lat = coord

    biz = raw.get("biz_ext")
    biz = biz if isinstance(biz, dict) else {}

    return AmapPoi(
        poi_id=poi_id,
        name=name,
        alias=_as_list(raw.get("alias")),
        type=_text(raw.get("type")),
        typecode=_text(raw.get("typecode")),
        lng=lng,
        lat=lat,
        address=_text(raw.get("address")),
        adname=_text(raw.get("adname")),
        cityname=_text(raw.get("cityname")),
        adcode=_text(raw.get("adcode")),
        tel=_text(raw.get("tel")),
        photos=_parse_photos(raw.get("photos")),
        # ⚠️ rating 契约里是 `str | None`（实测高德就返回字符串 `'4.8'`），别顺手转 float
        rating=_text(biz.get("rating")),
        cost_per_person=_num(biz.get("cost")),
        open_time=_text(biz.get("open_time")),
        open_time_detail=_text(biz.get("opentime2")),
    )


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """球面直线距离。**参数是 `(lng, lat)`。**

    为什么本地算而不用 `type=0`：本地算**不花配额**，而且省一次请求就少一次撞限流的机会。
    实测 `type=0` 给的距离与本地 haversine 一致（武侯祠→青城山：54504m vs 本地算 ≈54.5km）。
    """
    lng1, lat1 = a
    lng2, lat2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


# ══════════════════════════════════════════════════════════════


# ── 限速状态（**模块级**，进程内共享）─────────────────────────
# 限速约束的是「这把 Key 的出站速率」，与 provider 实例无关。
# 放实例级是错的：provider 是**每请求新建**的（`api/chat_stream.py` 的 factory
# 每请求建图 → 每请求建 provider），实例级状态等于"每个请求各限各的"
# → 并发 N 个请求就是 2.2N QPS，必然撞高德限流（实测第 3~4 个就炸）。
#
# 取号式排队：`_throttle` 先**同步地取走**自己那一格的发车时刻，再异步等。
# 取号的两行之间没有 await，在单 event loop 里天然原子 —— 既不需要锁，也不碰 loop，
# 所以跨 event loop（pytest 每个用例一个 loop）也不会出问题。
_next_slot_ts = 0.0


class AmapHttpProvider:
    """`AmapProvider` 协议的真实实现。**签名与 `MockAmapProvider` 完全一致**（D23）。"""

    name = "real"

    def __init__(
        self,
        key: str,
        *,
        timeout: float = 15.0,
        min_interval_s: float = MIN_INTERVAL_S,
        retry_backoff_s: tuple[float, ...] | None = None,
    ) -> None:
        if not key:
            raise ValueError("AmapHttpProvider 需要 AMAP_WEBSERVICE_KEY")
        self._key = key
        self._timeout = timeout
        self._min_interval_s = min_interval_s
        # 退避时长可注入，否则测"重试"要真等 1+2+4 秒（测试会慢到没人愿意跑）
        self._retry_backoff = retry_backoff_s if retry_backoff_s is not None else _RETRY_BACKOFF_S
        self._client: httpx.AsyncClient | None = None
        self._adcode_cache: dict[str, str] = {}
        """城市名 → adcode。**这个缓存值得有**：一次行程里同一目的地会被查很多次天气，
        而地理编码是纯浪费（地名不会在几分钟内变）。"""

    # ── HTTP 基座 ──────────────────────────────────────────────

    @property
    def client(self) -> httpx.AsyncClient:
        """懒创建。原因：`build_provider()` 是在**同步上下文**里被调的，
        那时还没有 event loop —— 在里面 `httpx.AsyncClient()` 会挂在错误的 loop 上。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _throttle(self) -> None:
        """**进程内**共享的最小间隔限速。

        取号式：先原子地取走"我这格的发车时刻"，把它推进到 `_next_slot_ts`，
        再异步等到那一刻。并发调用会自然排成一队，而不是各等各的。

        ⚠️ 状态在模块级（`_next_slot_ts`），**不能用实例变量** —— 见那一处的说明。
        """
        global _next_slot_ts

        now = time.monotonic()
        my_slot = max(now, _next_slot_ts)  # 到点就走，没到就排队
        _next_slot_ts = my_slot + self._min_interval_s

        wait = my_slot - now
        if wait > 0:
            await asyncio.sleep(wait)

    async def _request(self, path: str, **params: Any) -> dict[str, Any]:
        """打一发 GET，把**业务错误码**翻译成异常或重试。

        ⚠️ 高德的错误不体现在 HTTP 状态码上：超限时仍是 200 + `status=0`。
           所以 `raise_for_status()` 挡不住限流，必须读 `infocode`。
        """
        params["key"] = self._key
        url = f"{BASE_URL}{path}"
        attempt = 0
        backoffs = self._retry_backoff

        while True:
            await self._throttle()
            try:
                resp = await self.client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                # 网络层 / 非 JSON：GET 幂等，可以重试
                if attempt < len(backoffs):
                    backoff = backoffs[attempt]
                    logger.warning("高德 %s 网络失败（%s），%.1fs 后重试", path, exc, backoff)
                    await asyncio.sleep(backoff)
                    attempt += 1
                    continue
                raise AmapError("TRANSPORT", str(exc), path) from exc

            if str(payload.get("status")) == "1":
                return payload

            infocode = str(payload.get("infocode") or "")
            info = str(payload.get("info") or "")

            if infocode in _RETRYABLE_INFOCODES and attempt < len(backoffs):
                backoff = backoffs[attempt]
                logger.warning(
                    "高德 %s 触发限流（%s），%.1fs 后重试（第 %d/%d 次）",
                    path, info, backoff, attempt + 1, len(backoffs),
                )
                await asyncio.sleep(backoff)
                attempt += 1
                continue

            # 🔴 到这里就是**重试无用**的错误。抛出去，绝不返回空结果 ——
            #    返回空会被上层当成"这个关键词搜不到"，而真相是 Key 过期 / 日配额用完。
            raise AmapError(infocode, info, path)

    # ── 三个接口 ───────────────────────────────────────────────

    async def search_poi(
        self,
        keyword: str,
        city: str | None = None,
        limit: int = 10,
    ) -> list[AmapPoi]:
        """`/v3/place/text`。

        `extensions=all` **必加** —— 不加就没有 `biz_ext`（评分/营业时间/人均全靠它）。
        `offset` 走 `limit`，上限 25（实测 50 也能返回，但不依赖未文档化的行为）。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        params: dict[str, Any] = {
            "keywords": keyword,
            "offset": max(1, min(int(limit), 25)),
            "extensions": "all",
        }
        if city:
            params["city"] = city

        payload = await self._request("/v3/place/text", **params)
        raws = payload.get("pois")
        raws = raws if isinstance(raws, list) else []

        parsed: list[AmapPoi] = []
        skipped = 0
        for raw in raws:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            poi = parse_poi(raw)
            if poi is None:
                skipped += 1
                continue
            parsed.append(poi)

        if skipped:
            logger.debug("search_poi(%r) 跳过 %d 条无法解析的 POI", keyword, skipped)

        if raws and not parsed:
            # 🔴 有条目但一条都解析不出来 = **高德改了字段格式**，不是"搜不到"。
            #    这种情况必须炸出来，否则会静默变成"所有景点都不存在"。
            raise AmapError(
                "PARSE",
                f"{len(raws)} 条 POI 全部解析失败（第一条的字段：{sorted(raws[0])[:12]}…）",
                "/v3/place/text",
            )

        return parsed[:limit]

    async def get_poi(self, poi_id: str) -> AmapPoi | None:
        """`/v3/place/detail` 按 id 精确查。查不到（`pois: []`）返回 None。"""
        payload = await self._request("/v3/place/detail", id=poi_id)
        raws = payload.get("pois")
        raws = raws if isinstance(raws, list) else []
        for raw in raws:
            if isinstance(raw, dict):
                poi = parse_poi(raw)
                if poi is not None:
                    return poi
        return None

    async def get_weather(self, city: str, day: date) -> Weather:
        """地理编码 → 天气。**两步合成一个方法**，不额外增加工具数量（A7 只有 3 个工具）。"""
        city = (city or "").strip()
        if not city:
            return Weather(status=WeatherStatus.UNAVAILABLE, note="没有指定城市")

        adcode = await self._resolve_adcode(city)
        if adcode is None:
            return Weather(
                status=WeatherStatus.UNAVAILABLE,
                note=f"查不到「{city}」的城市编码，可能是地名写法不对（试试「成都市」或「都江堰市」）",
            )

        payload = await self._request("/v3/weather/weatherInfo", city=adcode, extensions="all")
        forecasts = payload.get("forecasts")
        forecasts = forecasts if isinstance(forecasts, list) else []
        casts: list[dict[str, Any]] = []
        if forecasts and isinstance(forecasts[0], dict):
            raw_casts = forecasts[0].get("casts")
            casts = [c for c in (raw_casts or []) if isinstance(c, dict)]

        if not casts:
            return Weather(
                status=WeatherStatus.UNAVAILABLE,
                note=f"高德没有返回「{city}」的预报（可能这座城市不开放天气服务）",
            )

        report_time = None
        if forecasts and isinstance(forecasts[0], dict):
            report_time = _text(forecasts[0].get("reporttime"))

        target = day.isoformat()
        for cast in casts:
            if _text(cast.get("date")) == target:
                return Weather(
                    status=WeatherStatus.OK,
                    day_weather=_text(cast.get("dayweather")),
                    night_weather=_text(cast.get("nightweather")),
                    day_temp=_int(cast.get("daytemp")),
                    night_temp=_int(cast.get("nighttemp")),
                    # ⚠️ 实测高德返回的是 `daywind='北'` / `daypower='1-3'`，
                    #    **不带"风"字也不带"级"字**。契约里 `Weather.day_wind` 的注释写的是
                    #    "如东北风"—— 那是**展示时的拼接结果**，不是原始值。照实存原值，
                    #    展示层要拼自己拼（别在数据层预先化妆，否则想改文案就得改数据）。
                    day_wind=_text(cast.get("daywind")),
                    day_power=_text(cast.get("daypower")),
                    report_time=report_time,
                )

        return Weather(
            status=WeatherStatus.UNAVAILABLE,
            note=(
                f"高德只预报从今天起 {len(casts)} 天（到 {_text(casts[-1].get('date'))}），"
                f"{target} 超出窗口，查不到"
            ),
            report_time=report_time,
        )

    async def calc_distance(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
    ) -> DistanceResult:
        """`/v3/distance`，`type=1`（驾车）。

        ⭐ 实测这个接口**同时给 `distance` 和 `duration`** —— 所以不用再调 `/v3/direction/driving`
        （那个接口配额更紧、返回结构复杂得多，我们只想要两个数）。

        `straight_km` 本地用 haversine 算，省一次请求。
        """
        o = f"{origin[0]},{origin[1]}"
        d = f"{dest[0]},{dest[1]}"
        payload = await self._request("/v3/distance", origins=o, destination=d, type="1")

        results = payload.get("results")
        results = results if isinstance(results, list) else []
        if not results or not isinstance(results[0], dict):
            raise AmapError("NO_RESULT", "测距接口没有返回 results", "/v3/distance")

        row = results[0]
        distance_m = _num(row.get("distance"))
        if distance_m is None:
            raise AmapError("NO_RESULT", f"测距结果里没有 distance：{row!r}", "/v3/distance")

        duration_s = _num(row.get("duration")) or 0.0
        if duration_s > 0:
            drive_min = max(1, round(duration_s / 60))
        else:
            # 实测 `type=1` 恒有 duration；这个分支是防高德改行为的兜底。
            # **为什么不用 0 兜底**：`drive_min=0` 会被"当天车程是否过长"的判据读成
            # "坐车不用时间"，比一个量级正确的估算值危险得多。
            # 30 km/h 是市区含红绿灯的经验速度，只求量级对。
            drive_min = max(1, round(distance_m / 1000 / 30 * 60))
            logger.warning("测距没有 duration，用 30km/h 估算：%s → %s", o, d)

        return DistanceResult(
            km=round(distance_m / 1000, 2),
            drive_min=drive_min,
            straight_km=round(_haversine_km(origin, dest), 2),
        )

    # ── 内部 ───────────────────────────────────────────────────

    async def _resolve_adcode(self, city: str) -> str | None:
        """城市名 → adcode。天气接口只吃 adcode（成都 = 510100）。

        只缓存**成功**的结果：失败的缓存下来会让"先打错字、再改对"永远拿不到数据。
        """
        cached = self._adcode_cache.get(city)
        if cached:
            return cached

        payload = await self._request("/v3/geocode/geo", address=city)
        geocodes = payload.get("geocodes")
        geocodes = geocodes if isinstance(geocodes, list) else []
        if not geocodes or not isinstance(geocodes[0], dict):
            return None

        adcode = _text(geocodes[0].get("adcode"))
        if adcode:
            self._adcode_cache[city] = adcode
        return adcode


__all__ = [
    "AmapError",
    "AmapHttpProvider",
    "BASE_URL",
    "MIN_INTERVAL_S",
    "parse_poi",
]
