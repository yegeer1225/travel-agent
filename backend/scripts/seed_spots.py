"""把**真实高德 POI** 收录进 `spots` 表（D70）—— 景点页的数据来源。

═══════════════════════════════════════════════════════════════
 这个脚本存在的理由是"禁止编数据"
═══════════════════════════════════════════════════════════════

景点页要搜"本站收录的景点"。**收录必须来自高德真实返回**，不能手写：
手写 5 条 INSERT 就是编数据，跟 D20 删掉参考设计那句"共收录 21006 个景点"是同一个错
（面试被问"这数据哪来的"直接答不上）。所以：

- 写入路径**只有这一个脚本**，且它**强制走 real provider**（不看 `AMAP_PROVIDER` 档位）
  —— 否则 mock 档下会把那 9 条演示数据当成"收录"存进去，数据来源就说不清了
- 每条的 `source` 列记来源、`collected_at` 记采集时刻（数据会过期，得知道什么时候采的）
- 幂等：同一个 `poi_id` 已存在就 UPDATE（重跑无害）

═══════════════════════════════════════════════════════════════
 用法
═══════════════════════════════════════════════════════════════

    # 收录（默认每个关键词取 1 条，--top 可调）
    python scripts/seed_spots.py --city 成都 \
        --keywords "宽窄巷子,武侯祠,杜甫草堂,锦里,大熊猫繁育研究基地"

    # 先看看会收什么，不落库
    python scripts/seed_spots.py --city 成都 --keywords "锦里" --dry-run

    # 看当前收录了多少
    python scripts/seed_spots.py --list

⚠️ 有配额消耗：每个关键词 = 1 次高德请求（provider 内已按 D35 串行 + 0.45s 节流）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.providers.amap import AmapHttpProvider  # noqa: E402
from app.schemas import AmapPoi  # noqa: E402
from app.store.db import connect, init_db  # noqa: E402
from app.store.repo import SpotRepo  # noqa: E402

DEFAULT_KEYWORDS = "宽窄巷子,武侯祠,杜甫草堂,锦里,大熊猫繁育研究基地"
DEFAULT_CITY = "成都"


def _print_poi(poi: AmapPoi, *, index: int | None = None) -> None:
    prefix = f"{index:>2}. " if index is not None else "    "
    place = f"{poi.cityname or '?'}·{poi.adname or '?'}"
    bits = [f"{poi.poi_id:<14}", f"{poi.name[:26]:<28}", f"{place:<12}"]
    bits.append(f"评分{poi.rating}" if poi.rating else "评分未知")
    if poi.cost_per_person is not None:
        bits.append(f"人均{poi.cost_per_person:.0f}")
    print(prefix + "  ".join(bits))
    if poi.type:
        print(f"      type={poi.type}")


async def _collect(city: str, keywords: list[str], top: int) -> list[AmapPoi]:
    """按关键词逐个搜，每个取前 `top` 条。**串行**（provider 内部再叠一层 0.45s 节流）。"""
    if not settings.amap_webservice_key:
        raise SystemExit(
            "❌ 缺 AMAP_WEBSERVICE_KEY。收录必须用真实高德数据 —— "
            "这个脚本故意不看 AMAP_PROVIDER 档位，避免把 mock 的演示数据当成收录。"
        )
    provider = AmapHttpProvider(settings.amap_webservice_key)

    picked: dict[str, AmapPoi] = {}
    for kw in keywords:
        print(f"\n── 搜「{kw}」（city={city}）")
        hits = await provider.search_poi(kw, city=city, limit=50)
        if not hits:
            print("   ❌ 没有结果 —— 检查关键词与城市名")
            continue
        for poi in hits[:top]:
            first = poi.poi_id not in picked
            picked[poi.poi_id] = poi
            _print_poi(poi, index=len(picked))
            if not first:
                print("      （同 id 去重：前面已收过）")
    return list(picked.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="把真实高德 POI 收录进 spots 表（D70）")
    parser.add_argument("--city", default=DEFAULT_CITY, help=f"城市名，默认 {DEFAULT_CITY}")
    parser.add_argument("--keywords", default=DEFAULT_KEYWORDS, help="逗号分隔的关键词")
    parser.add_argument("--top", type=int, default=1, help="每个关键词取前几条，默认 1")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不落库")
    parser.add_argument("--list", action="store_true", help="只列当前收录，不搜高德")
    args = parser.parse_args()

    init_db(settings)  # 幂等；收录前保证表在
    repo = SpotRepo(conn_factory=lambda: connect(settings))

    if args.list:
        print(f"当前收录：{repo.count()} 条")
        # 接口层的空关键词是 400，所以这里直接列全量，不绕 `search()`
        with connect(settings) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT poi_id, name, city, district, rating, source, collected_at "
                "FROM spots ORDER BY city, name"
            )
            rows = cur.fetchall()
        for i, row in enumerate(rows, 1):
            print(
                f"{i:>3}. {row['poi_id']:<14} {row['name'][:26]:<28} "
                f"{row['city'] or '?'}·{row['district'] or '?'}  "
                f"评分{row['rating'] or '未知'}  source={row['source']}  {row['collected_at']}"
            )
        return

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keywords:
        raise SystemExit("❌ --keywords 是空的")
    if args.top < 1:
        raise SystemExit("❌ --top 至少为 1")

    print("═" * 78)
    print(f"收录（真实高德数据） city={args.city}  关键词 {len(keywords)} 个  每个取前 {args.top} 条")
    print("═" * 78)
    pois = asyncio.run(_collect(args.city, keywords, args.top))

    print("\n" + "═" * 78)
    if args.dry_run:
        print(f"--dry-run：搜到 {len(pois)} 条，**没有落库**")
        return
    if not pois:
        print("❌ 一条都没搜到，未落库")
        return
    inserted, updated = repo.upsert_many(pois, source="seed")
    print(f"✅ 收录完成：新增 {inserted} 条，更新 {updated} 条 —— 当前共 {repo.count()} 条")
    print("（用 `--list` 复核；景点页现在搜的就是这批）")


if __name__ == "__main__":
    main()
