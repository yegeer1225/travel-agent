"""把**我们自己 agent 真实生成的行程**发布成官方攻略 —— 攻略社区的起始内容。

═══════════════════════════════════════════════════════════════
 为什么起始内容用"自己生成的"，不用 eval/fixtures 那 2 篇
═══════════════════════════════════════════════════════════════

`eval/fixtures/` 是**评测素材**（M8 用），里面那 2 篇是 WebSearch 抓回来的
**别人的文章**（来源 URL 还写着"待补"）。它们的文件头自己写着：

    ⚠️ 版权边界：本文件只作本地评测素材（eval/fixtures/，不进部署、不进攻略库公开内容）

放社区 = 转载别人的文章且没署名 → 所以起始内容改用**本站真实产出**：
`trips` 表里那些真实生成的行程 → `guide_render` 渲染成攻略 → 挂官方号。
零版权风险、零 LLM 成本，而且顺带证明主线「行程 → 攻略」是通的。

⚠️ 与 A38 不冲突（方向容易想反）：A38 禁的是「评测集**回读线上库**」
（防线上内容污染分数）；本脚本方向相反 —— 把**产品自己的产出**放进产品。

═══════════════════════════════════════════════════════════════
 官方号
═══════════════════════════════════════════════════════════════

    username = travel_platform    ← 契约限定 ^[A-Za-z0-9_]+$，**不能用中文**
    nickname = 旅游规划平台         ← 页面显示走 `nickname or username`（api/views.py）

所以社区里显示的作者名就是站名「旅游规划平台」（`界面设计.md` 250 行的站名）。

═══════════════════════════════════════════════════════════════
 用法
═══════════════════════════════════════════════════════════════

    # 发布（默认取最近 3 条真实行程，按目的地去重）
    python scripts/seed_official_guides.py

    # 先看会发什么，不落库
    python scripts/seed_official_guides.py --dry-run

    # 指定行程（可重复传）
    python scripts/seed_official_guides.py --trip 618a9b5a-...

    # 看官方号 + 它名下的攻略
    python scripts/seed_official_guides.py --list

**幂等**：幂等键 = `seed:<trip_id>`，同一条行程重跑**不会**发第二篇。
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.security import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.schemas import Trip  # noqa: E402
from app.services.guide_render import render_guide_from_trip  # noqa: E402
from app.store.db import connect, init_db  # noqa: E402
from app.store.repo import GuideRepo, UserRepo  # noqa: E402

OFFICIAL_USERNAME = "travel_platform"
OFFICIAL_NICKNAME = "旅游规划平台"
SEED_KEY_PREFIX = "seed:"


def ensure_official_account(user_repo: UserRepo) -> int:
    """确保官方号存在 → 返回 user_id。**已存在就不动它**（不重置密码）。"""
    existing = user_repo.get_by_username(OFFICIAL_USERNAME)
    if existing is not None:
        print(f"官方号已存在：{OFFICIAL_USERNAME}（昵称「{existing.nickname}」，id={existing.id}）")
        return existing.id

    password = secrets.token_urlsafe(16)
    rec = user_repo.create(
        OFFICIAL_USERNAME, hash_password(password), nickname=OFFICIAL_NICKNAME
    )
    print(f"✅ 已创建官方号：{OFFICIAL_USERNAME} / 昵称「{OFFICIAL_NICKNAME}」/ id={rec.id}")
    print(f"   初始密码（**只打印这一次**，不用于演示登录）：{password}")
    return rec.id


def pick_trips(limit: int) -> list[dict]:
    """取真实生成的行程，**按目的地去重**（每个目的地只留最新一条）。

    为什么去重：起始内容的目的是"让社区不像个空壳"，两条都是成都的比
    "成都 + 杭州"单薄。要更多条就调 `--limit`。
    """
    with connect(settings) as conn, conn.cursor() as cur:
        # 多取一些再去重：去重后可能不足 limit，所以先拉 limit*5 条候选
        cur.execute(
            "SELECT id, title, destination, trip_json, created_at FROM trips "
            "WHERE source = %s ORDER BY created_at DESC LIMIT %s",
            ("generated", limit * 5),
        )
        rows = cur.fetchall()

    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        dest = row["destination"] or ""
        if dest in seen:
            continue
        seen.add(dest)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def load_trip_row(trip_id: str) -> dict | None:
    """按 id 直读行程。

    ⚠️ 不走 `TripRepo.get(user_id, trip_id)` —— 那个带归属过滤（D31 防线），
    而官方号**不是**这些行程的作者，走它必然查不到。
    """
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, destination, trip_json, created_at FROM trips WHERE id = %s",
            (trip_id,),
        )
        return cur.fetchone()


def cmd_list(user_repo: UserRepo, guide_repo: GuideRepo) -> None:
    u = user_repo.get_by_username(OFFICIAL_USERNAME)
    if u is None:
        print("❌ 官方号还不存在 —— 先跑一次发布（不带 --list）")
        return
    print(f"官方号：{u.username}（昵称「{u.nickname}」，id={u.id}）")
    rows, total = guide_repo.list_mine(u.id, limit=100)
    print(f"名下攻略 {total} 篇：")
    for r in rows:
        dest = r.destination or "?"
        print(
            f"  · {r.visibility:<7} {r.title[:26]:<28} {dest:<6} "
            f"{len(r.content_md):>5} 字  published={r.published_at}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把真实生成的行程发布成官方攻略（攻略社区的起始内容）"
    )
    parser.add_argument("--limit", type=int, default=3, help="发几条，默认 3（按目的地去重后）")
    parser.add_argument("--trip", action="append", default=[], help="指定 trip_id，可重复传")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不落库")
    parser.add_argument("--list", action="store_true", help="只列官方号与它名下的攻略")
    args = parser.parse_args()

    init_db(settings)
    user_repo = UserRepo(conn_factory=lambda: connect(settings))
    guide_repo = GuideRepo(conn_factory=lambda: connect(settings))

    if args.list:
        cmd_list(user_repo, guide_repo)
        return

    if args.trip:
        targets: list[dict] = []
        for tid in args.trip:
            row = load_trip_row(tid)
            if row is None:
                print(f"⚠️ 行程 {tid} 不存在，跳过")
                continue
            targets.append(row)
    else:
        targets = pick_trips(args.limit)

    if not targets:
        print("❌ 没有可用行程（`trips` 表里没有 source='generated' 的记录）")
        return

    if args.dry_run:
        print("【dry-run】不会创建官方号、不会落库\n")
        official_id: int | None = None
    else:
        official_id = ensure_official_account(user_repo)
        print()

    print(f"准备处理 {len(targets)} 篇：")
    published = skipped = 0
    for row in targets:
        trip = Trip.model_validate_json(row["trip_json"])
        rendered = render_guide_from_trip(trip)
        key = f"{SEED_KEY_PREFIX}{row['id']}"
        days = len(trip.days)
        stops = sum(len(d.stops) for d in trip.days)
        print(
            f"  · {rendered.title}（{days} 天 {stops} 站，{len(rendered.content_md)} 字，"
            f"poi {len(rendered.poi_ids)} 个）"
        )

        if args.dry_run:
            print(f"      [dry-run] 幂等键 = {key}，没有落库")
            continue

        if guide_repo.find_by_idempotency(official_id, key) is not None:
            print(f"      已发过（幂等键 {key} 命中），跳过 —— **不会**发第二篇")
            skipped += 1
            continue

        rec = guide_repo.create(
            official_id,
            title=rendered.title,
            content_md=rendered.content_md,
            destination=rendered.destination,
            cover=None,  # 刻意不取高德封面：脚本离线跑；封面属"缺就隐藏"（界面设计 184 行有兜底）
            poi_ids=rendered.poi_ids,
            source_trip_id=row["id"],  # 溯源：这篇攻略来自那条真实行程
            idempotency_key=key,
            publish=True,  # → visibility='public' + published_at
        )
        print(f"      ✅ 已发布 guide_id={rec.id}（public）")
        published += 1

    if args.dry_run:
        return
    print(f"\n完成：新发布 {published} 篇，跳过 {skipped} 篇")
    print("（用 `--list` 复核；社区页现在读的就是这批）")
    print(f"   官方号 id={official_id}，`guides.user_id` 指向它 —— 显示名 = nickname「{OFFICIAL_NICKNAME}」")


if __name__ == "__main__":
    main()
