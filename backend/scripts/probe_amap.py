"""高德 Web 服务实测探针 —— M2 全部事实的来源。

**为什么留这个脚本而不是跑完即弃**（D8 的教训）：
「我实测过」而不给出可重跑路径 = 没实测。这个脚本同时干两件事：

1. 打印一份**事实表**（会被贴进 `技术方案.md`，注明实测日期）
2. 采集**原始响应样本**存进 `tests/fixtures/amap/`
   → 解析逻辑的测试因此**不依赖网络**，也不会因为高德改数据而随机红

⚠️ 有配额消耗：全跑一遍约 30 次请求。高德个人 Key 实测约 3 QPS，
   脚本内已节流（默认 1.3s 间隔），**不要并发跑两份**。

用法：
    python scripts/probe_amap.py              # 全跑（含限流探测 + 采样本）
    python scripts/probe_amap.py --no-limit   # 跳过限流探测（不触发持续失败）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

BASE = "https://restapi.amap.com"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "amap"

# 实测节流间隔。3 QPS ≈ 0.33s，但实测是**滑动窗口**（连续第 4 个必炸），
# 留 4 倍余量：1.3s 对应约 0.77 QPS，稳定不触发。
THROTTLE_S = 1.3

_last_call = 0.0
_call_count = 0


def _get(key: str, path: str, *, throttle: bool = True, **params: Any) -> dict[str, Any]:
    """打一个高德 GET 接口。**只负责拿 JSON，不解释错误码。**"""
    global _last_call, _call_count
    if throttle:
        wait = THROTTLE_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
    _last_call = time.monotonic()
    _call_count += 1

    params["key"] = key
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # HTTP 层错误（与业务错误码是两回事）
        return {"status": "0", "info": f"HTTPError {exc.code}", "infocode": "HTTP"}


def _save(name: str, payload: dict[str, Any]) -> Path:
    """存原始样本。**原样落盘，不做任何裁剪** —— 裁剪过的 fixture 测不出解析坑。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _brief(payload: dict[str, Any]) -> str:
    return f"status={payload.get('status')} info={payload.get('info')!r} infocode={payload.get('infocode')}"


# ══════════════════════════════════════════════════════════════
#  ① 凭证格式（1 秒成本，挡掉最常见的错误）
# ══════════════════════════════════════════════════════════════


def check_credentials() -> None:
    import os
    import re

    print("\n" + "═" * 78)
    print("① 凭证格式")
    print("═" * 78)
    # ⚠️ JS_KEY / SECURITY_CODE **不在 `config.py` 里**，也**不该在里面** ——
    #    它们是前端地图用的，后端永远不读。这里从环境变量直接取只为验格式。
    rows = [
        ("AMAP_WEBSERVICE_KEY", settings.amap_webservice_key, "后端（本项目用这个）"),
        ("AMAP_JS_KEY", os.environ.get("AMAP_JS_KEY", ""), "前端地图（豆包用）"),
        ("AMAP_JS_SECURITY_CODE", os.environ.get("AMAP_JS_SECURITY_CODE", ""), "前端地图配套"),
    ]
    for name, value, note in rows:
        ok = bool(re.fullmatch(r"[0-9a-f]{32}", value or ""))
        print(f"  {name:24} len={len(value or ''):3} 32位纯hex={'✅' if ok else '❌'}  {note}")
    print("\n  ⚠️ 格式只能挡「复制漏字符」。**JS_KEY 真伪服务端验不了**（实测排除过 4 种试法），")
    print("     只能等 M2 前端在浏览器里跑起来看报不报 INVALID_USERKEY。")


# ══════════════════════════════════════════════════════════════
#  ② 三个端点：可用性 + 字段全集（采样本）
# ══════════════════════════════════════════════════════════════


def check_place_text(key: str) -> None:
    print("\n" + "═" * 78)
    print("② /v3/place/text —— POI 搜索")
    print("═" * 78)

    r = _get(key, "/v3/place/text", keywords="武侯祠", city="成都", offset=3, extensions="all")
    _save("place_text_wuhouci.json", r)
    print(f"  {_brief(r)}  count={r.get('count')}")
    pois = r.get("pois") or []
    if not pois:
        print("  ❌ 没有返回 POI，后面的采集跳过")
        return

    print(f"  顶层字段（{len(r)} 个）: {sorted(r.keys())}")
    print(f"  POI 字段（{len(pois[0])} 个）:")
    print(f"    {sorted(pois[0].keys())}")
    p = pois[0]
    print("  关键字段 repr（`repr` 才能分清 `[]` / `''` / `None`）:")
    for f in ("id", "name", "alias", "location", "type", "typecode", "address", "adname",
              "cityname", "adcode", "tel", "rating", "cost", "biz_ext", "photos", "children"):
        print(f"    {f:14} = {p.get(f)!r}")

    # 餐饮样本：cost / open_time 有值的唯一类别
    r2 = _get(key, "/v3/place/text", keywords="火锅", city="成都", offset=1, extensions="all")
    _save("place_text_food.json", r2)
    b2 = ((r2.get("pois") or [{}])[0].get("biz_ext") or {})
    print(f"\n  [餐饮样本] {_brief(r2)}  biz_ext={b2!r}")

    # 乱码关键词：**高德几乎不会返回真空**，这一条打破「搜不到」的直觉
    r3 = _get(key, "/v3/place/text", keywords="zzz不存在的关键词xyz", city="成都", offset=2)
    _save("place_text_gibberish.json", r3)
    n3 = len(r3.get("pois") or [])
    print(f"  [乱码关键词] {_brief(r3)}  返回 {n3} 条 ← 真实高德几乎不返回空")
    for p3 in (r3.get("pois") or [])[:3]:
        print(f"      {p3.get('name')!r}  type={(p3.get('type') or '')[:22]}")


def check_geocode(key: str) -> str | None:
    print("\n" + "═" * 78)
    print("③ /v3/geocode/geo —— 地理编码（天气接口的前置）")
    print("═" * 78)
    r = _get(key, "/v3/geocode/geo", address="成都市")
    _save("geocode_chengdu.json", r)
    print(f"  {_brief(r)}  count={r.get('count')}")
    geocodes = r.get("geocodes") or []
    if not geocodes:
        print("  ❌ 没拿到 geocode")
        return None
    gc = geocodes[0]
    print(f"  字段全集: {sorted(gc.keys())}")
    for f in ("formatted_address", "province", "city", "adcode", "citycode", "location", "level"):
        print(f"    {f:18} = {gc.get(f)!r}")
    return str(gc.get("adcode") or "")


def check_weather(key: str, adcode: str | None) -> None:
    print("\n" + "═" * 78)
    print("④ /v3/weather/weatherInfo —— 天气预报")
    print("═" * 78)
    if not adcode:
        print("  ⏭ 没有 adcode，跳过")
        return
    r = _get(key, "/v3/weather/weatherInfo", city=adcode, extensions="all")
    _save("weather_chengdu.json", r)
    print(f"  {_brief(r)}")
    forecasts = r.get("forecasts") or []
    if not forecasts:
        print("  ❌ 没有 forecasts")
        return
    f0 = forecasts[0]
    casts = f0.get("casts") or []
    print(f"  forecast 字段: {sorted(f0.keys())}")
    print(f"  reporttime = {f0.get('reporttime')!r}")
    print(f"  ⭐ casts 天数 = {len(casts)}  ← 官方文档写 3 天，实测 4 天")
    if casts:
        print(f"  cast 字段: {sorted(casts[0].keys())}")
        for c in casts:
            print(f"    {c.get('date')} week={c.get('week')!r} {c.get('dayweather')}/{c.get('nightweather')} "
                  f"{c.get('daytemp')}~{c.get('nighttemp')}℃ "
                  f"{c.get('daywind')}风{c.get('daypower')}级")

    r2 = _get(key, "/v3/weather/weatherInfo", city=adcode, extensions="base")
    _save("weather_live_chengdu.json", r2)
    lives = r2.get("lives") or []
    if lives:
        print(f"  [实况 extensions=base] 字段: {sorted(lives[0].keys())}")
        print(f"    {lives[0]!r}")


def check_distance(key: str) -> None:
    print("\n" + "═" * 78)
    print("⑤ /v3/distance —— 测距（**带 duration，不用再调路径规划**）")
    print("═" * 78)
    wuhou = "104.047992,30.646168"
    qingcheng = "103.563817,30.904400"

    for t in ("0", "1"):
        r = _get(key, "/v3/distance", origins=wuhou, destination=qingcheng, type=t)
        res = (r.get("results") or [{}])[0]
        kind = "直线" if t == "0" else "驾车"
        print(f"  [type={t} {kind}] {_brief(r)}  result 字段={sorted(res.keys())}")
        print(f"      distance={res.get('distance')}m  duration={res.get('duration')}s")
        if t == "1":
            _save("distance_type1_wuhou_qingcheng.json", r)

    # 批量：一次请求多个起点 → 同一终点。省 QPS 的关键能力。
    origins = "|".join([wuhou, "104.066301,30.572961", "104.043131,30.658573"])
    r = _get(key, "/v3/distance", origins=origins, destination=qingcheng, type="1")
    _save("distance_batch_type1.json", r)
    print(f"  [批量 origins=3] {_brief(r)}  count={r.get('count')}")
    for res in r.get("results") or []:
        km = int(res.get("distance") or 0) / 1000
        mins = int(res.get("duration") or 0) / 60
        print(f"      origin_id={res.get('origin_id')} → {km:.1f} km / {mins:.0f} min")

    # ── 校准 mock 的距离模型 ──
    # mock 用 `直线 × _ROAD_FACTOR ÷ _AVG_SPEED_KMH` 估车程，那两个常量原本是
    # **没实测依据时的猜测值**。而 `reachable` 判据直接依赖车程 ——
    # 常量偏了，mock 下的判据就会比真实更严或更松，**把真问题盖住**。
    #
    # ⚠️ 样本必须**覆盖不同距离段**：市区内短途在红绿灯里磨，远郊走高速，
    #    平均速度能差一倍。只测一条就定常量，等于拿远郊的速度去算市区。
    print("\n  [校准样本] 市区内 / 近郊 / 远郊 三段：")
    print(f"      {'样本':<26}{'直线 km':>9}{'驾车 km':>10}{'分钟':>7}{'路网系数':>10}{'均速 km/h':>11}")
    from app.providers.amap import _haversine_km  # 复用同一套公式，别在这里再写一遍

    calibrations: list[dict[str, Any]] = []
    cases = [
        ("市区内 武侯祠→宽窄巷子", "104.047992,30.646168", (104.053307, 30.663869)),
        ("近郊   人民公园→熊猫基地", "104.057641,30.656990", (104.138176, 30.740573)),
        ("远郊   武侯祠→都江堰", "104.047992,30.646168", (103.610529, 31.003363)),
        ("远郊   武侯祠→青城山", "104.047992,30.646168", (103.563817, 30.904400)),
    ]
    for label, origin, dest_coord in cases:
        dest = f"{dest_coord[0]},{dest_coord[1]}"
        r = _get(key, "/v3/distance", origins=origin, destination=dest, type="1")
        res = (r.get("results") or [{}])[0]
        meters = int(res.get("distance") or 0)
        seconds = int(res.get("duration") or 0)
        if not meters:
            print(f"      {label:<26}  没拿到结果")
            continue
        o_lng, o_lat = (float(x) for x in origin.split(","))
        straight = _haversine_km((o_lng, o_lat), dest_coord)
        km = meters / 1000
        minutes = seconds / 60
        row = {
            "label": label.strip(),
            "straight_km": round(straight, 2),
            "km": round(km, 1),
            "minutes": round(minutes),
            "road_factor": round(km / straight, 3),
            "speed_kmh": round(km / (minutes / 60), 1),
        }
        calibrations.append(row)
        print(f"      {label:<26}{straight:>9.1f}{km:>10.1f}{minutes:>7.0f}"
              f"{row['road_factor']:>10.3f}{row['speed_kmh']:>11.1f}")
    _save("distance_calibration.json", {"cases": calibrations})
    print("  ⭐ 用法：`_ROAD_FACTOR` 取路网系数的中位数；`_AVG_SPEED_KMH` 按**距离段**加权，")
    print("     不要拿远郊的均速去算市区 —— 两者能差一倍。")


# ══════════════════════════════════════════════════════════════
#  ⑥ 分类别抽样 —— 最容易漏的一步
# ══════════════════════════════════════════════════════════════


def check_biz_ext_categories(key: str) -> None:
    print("\n" + "═" * 78)
    print("⑥ 分类别抽样：`biz_ext` 的键**本身是变长的**")
    print("═" * 78)
    cases = [
        ("餐饮", {"keywords": "火锅", "city": "成都", "offset": 1}),
        ("咖啡", {"keywords": "咖啡", "city": "成都", "offset": 1}),
        ("酒店", {"keywords": "酒店", "city": "成都", "offset": 1}),
        ("地铁站", {"keywords": "地铁站", "city": "成都", "offset": 1}),
        ("博物馆(types码)", {"types": "140100", "city": "成都", "offset": 1}),
    ]
    print(f"  {'类别':<16}{'cost':<12}{'rating':<10}{'open_time':<20}{'biz_ext 全部键'}")
    print("  " + "-" * 104)
    samples: list[dict[str, Any]] = []
    for label, params in cases:
        r = _get(key, "/v3/place/text", extensions="all", **params)
        pois = r.get("pois") or []
        if not pois:
            print(f"  {label:<16}(空)")
            continue
        p = pois[0]
        samples.append(p)
        b = p.get("biz_ext") or {}
        print(f"  {label:<16}{repr(b.get('cost')):<12}{repr(b.get('rating')):<10}"
              f"{repr(b.get('open_time')):<20}{sorted(b.keys())}")
    _save("poi_by_category_samples.json", {"pois": samples})
    print("\n  ⭐ 结论：`cost` 只在餐饮/咖啡有；`lowest_price`/`star` 是酒店专有；")
    print("     所以 AmapPoi 的字段解析必须全部用 `.get()`，且空数组必须收敛成 None。")


# ══════════════════════════════════════════════════════════════
#  ⑦ 搜索行为边界
# ══════════════════════════════════════════════════════════════


def check_search_behaviour(key: str) -> None:
    print("\n" + "═" * 78)
    print("⑦ 搜索行为：抽象词 / types 类别码 / offset 上限")
    print("═" * 78)

    for kw in ("历史古迹", "小吃"):
        r = _get(key, "/v3/place/text", keywords=kw, city="成都", offset=4, extensions="all")
        print(f"  [keywords={kw!r}] {_brief(r)} count={r.get('count')}")
        for p in (r.get("pois") or [])[:4]:
            print(f"      {p.get('name','')[:24]:<26} typecode={p.get('typecode')} "
                  f"rating={((p.get('biz_ext') or {}).get('rating'))!r}")

    # types 类别码：比关键词**明显更精准**（返回的是知名馆而非小众馆）
    r = _get(key, "/v3/place/text", types="140100", city="成都", offset=6, extensions="all")
    _save("place_text_types_140100.json", r)
    print(f"\n  [types=140100 博物馆] {_brief(r)} count={r.get('count')}")
    for p in (r.get("pois") or [])[:6]:
        print(f"      {p.get('name','')[:24]:<26} typecode={p.get('typecode')} "
              f"rating={((p.get('biz_ext') or {}).get('rating'))!r}")

    for off in (25, 50):
        r = _get(key, "/v3/place/text", keywords="火锅", city="成都", offset=off)
        print(f"  [offset={off}] {_brief(r)} 实际返回={len(r.get('pois') or [])}")


# ══════════════════════════════════════════════════════════════
#  ⑧ 限流边界 —— 必须实测，因为限流**不抛异常**
# ══════════════════════════════════════════════════════════════


def check_rate_limit(key: str) -> None:
    print("\n" + "═" * 78)
    print("⑧ 限流边界：连续请求不加间隔")
    print("═" * 78)
    print("  ⚠️ 这一步会**故意**打出连续失败，用来确认失败长什么样。")
    failures: list[int] = []
    for i in range(1, 9):
        r = _get(key, "/v3/place/text", keywords="火锅", city="成都", offset=1, throttle=False)
        st = r.get("status")
        mark = "" if st == "1" else "   ← 失败"
        print(f"    #{i} status={st} info={r.get('info')!r} infocode={r.get('infocode')}{mark}")
        if st != "1":
            failures.append(i)
    print(f"  失败位置 = {failures or '8 次全过'}")
    print("  ⭐ 关键：失败是 `status=0` + `infocode=10021`，**HTTP 仍是 200、不抛异常**。")
    print("     代码里必须把 10021 识别成「重试退避」类，绝不能当成「搜不到」。")
    print("     恢复：窗口滑过后自动好（第 8 次已恢复）→ 说明是滑动窗口而非封禁。")


# ══════════════════════════════════════════════════════════════
#  ⑨ 补齐 mock 池的字段真值
# ══════════════════════════════════════════════════════════════


def collect_mock_pool_fields(key: str) -> None:
    """采集 `mock.py` 里那 9 个 POI 的 `type` / `typecode` **实测真值**。

    **为什么需要单独采一次**：D34 当时不敢给 mock 的 POI 填 `type`，理由是
    "只知道格式、不知道这 9 个 POI 的真实值，填了就是编造"。现在有探针了 ——
    真值能拿到，就该补上。不补的后果很具体：`weather_conflict` 判据要读 `type`
    判"户外还是室内"，而 mock 下 `type` 恒为 `None` → 那条判据**永远走不到**，
    连测试都写不出来。

    **池子直接从 `mock.py` 导入**，不在这里复制一份名单 —— 复制就会不同步，
    而不同步**不报错**（坑 21）。
    """
    from app.providers.mock import MOCK_POI_POOL

    print("\n" + "═" * 78)
    print("⑨ 采集 mock 池 9 个 POI 的 type / typecode（实测真值）")
    print("═" * 78)
    print("  ⚠️ 按 **poi_id 精确匹配**，不靠名字 —— 「人民公园」全国有很多个。\n")

    results: list[dict[str, Any]] = []
    for poi in MOCK_POI_POOL:
        r = _get(key, "/v3/place/text", keywords=poi.name, city="成都", offset=10, extensions="all")
        hit = next((p for p in (r.get("pois") or []) if p.get("id") == poi.poi_id), None)
        if hit is None:
            print(f"  ❌ {poi.name[:24]:<26} [{poi.poi_id}] 没搜到同一个 id")
            print(f"      （返回的前 3 个 id：{[p.get('id') for p in (r.get('pois') or [])[:3]]}）")
            continue
        b = hit.get("biz_ext") or {}
        results.append({
            "poi_id": poi.poi_id,
            "name": poi.name,
            "type": (hit.get("type") or None),
            "typecode": (hit.get("typecode") or None),
            "open_time": (b.get("open_time") or None),
            "rating": (b.get("rating") or None),
        })
        print(f"  ✅ {poi.name[:24]:<26} type={hit.get('type')!r}")
        print(f"     {'':24} typecode={hit.get('typecode')!r}")

    _save("mock_pool_types.json", {"collected_at": "probe", "items": results})

    print("\n  ↓↓↓ 直接贴进 `app/providers/mock.py`（每条的 `type` / `typecode`）↓↓↓")
    for row in results:
        print(f'        type={row["type"]!r},')
        print(f'        typecode={row["typecode"]!r},   # {row["name"]}')


# ══════════════════════════════════════════════════════════════


def main() -> int:
    parser = argparse.ArgumentParser(description="高德 Web 服务实测探针")
    parser.add_argument("--no-limit", action="store_true", help="跳过限流探测（不触发连续失败）")
    parser.add_argument(
        "--collect-mock",
        action="store_true",
        help="额外采集 mock 池 9 个 POI 的 type/typecode（+9 次请求，平时用不到）",
    )
    args = parser.parse_args()

    print("高德 Web 服务实测探针")
    print(f"AMAP_PROVIDER={settings.amap_provider}  （探针**无视**这个开关，永远直连真接口）")

    check_credentials()
    if not settings.amap_webservice_key:
        print("\n❌ AMAP_WEBSERVICE_KEY 为空，无法继续")
        return 1

    key = settings.amap_webservice_key
    check_place_text(key)
    adcode = check_geocode(key)
    check_weather(key, adcode)
    check_distance(key)
    check_biz_ext_categories(key)
    check_search_behaviour(key)
    if not args.no_limit:
        check_rate_limit(key)
    if args.collect_mock:
        collect_mock_pool_fields(key)

    print("\n" + "═" * 78)
    print(f"完成：共 {_call_count} 次请求｜样本存到 {FIXTURE_DIR}")
    print("═" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
