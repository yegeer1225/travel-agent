"""M5 真库验收：MySQL 上把会话/行程走一遍，并验证「重启服务会话还在」。

═══════════════════════════════════════════════════════════════
 什么时候跑
═══════════════════════════════════════════════════════════════

pytest **不连数据库**（requirements.txt 的纪律）—— 所以存储层的真 SQL
只有这个脚本能验证。Docker 的 MySQL 起来后跑一次：

    cd backend && ../.venv/Scripts/python.exe scripts/verify_mysql_api.py

先跑 `docker-compose up -d`（项目根，MySQL 8；排序规则见 db.py 的注释）。

**"重启服务还在"怎么验**：起两个**独立的 app 实例**（各自建自己的连接池
等价于两次进程启动），在 A 里创建、在 B 里读 —— 存储在 MySQL 不在进程里，
B 读得到 = 重启不丢。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.api.main import create_app  # noqa: E402
from app.config import settings  # noqa: E402
from app.store.db import connect, init_db  # noqa: E402
from app.store.repo import TripRepo  # noqa: E402


def _make_trip(trip_id: str):
    """最小合法 Trip（走真实 schemas 校验，不是手拼 dict）。"""
    from app.schemas import Trip, TripSummary

    return Trip(
        trip_id=trip_id,
        title="验收行程",
        destination="成都",
        source="generated",
        created_at="2026-09-16T12:00:00+00:00",
        updated_at="2026-09-16T12:00:00+00:00",
        summary=TripSummary(total_distance_km=10.5, stop_count=6, hard_errors=0, soft_warnings=0),
        user_id=1,
    )


def main() -> int:
    print("① 建库建表（幂等）...")
    init_db(settings)
    print(f"   ✓ `{settings.mysql_db}` 就绪（{settings.mysql_host}:{settings.mysql_port}）")

    # 实例 A —— 相当于第一次启动的服务
    app_a = create_app()
    client = TestClient(app_a)

    print("①.5 注册验收用户并拿 token（M9：所有路由走真 JWT）...")
    from app.api.security import hash_password
    from app.store.repo import UserRepo

    user_repo = UserRepo(conn_factory=lambda: connect(settings))
    if user_repo.get_by_username("verify_user") is None:
        user_repo.create("verify_user", hash_password("verify-pass-123"), nickname="验收员")
    # 验收脚本的 app 用的是自己的 UserRepo 实例，但读同一个库 —— 直接签 token
    from app.api.security import create_token

    verify_uid = user_repo.get_by_username("verify_user").id
    token, _ = create_token(verify_uid)
    client.headers.update({"Authorization": f"Bearer {token}"})

    print("② 健康检查...")
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"], r.text
    print(f"   ✓ mock_mode={r.json()['mock_mode']}")

    print("③ 会话 CRUD...")
    sid = client.post("/api/sessions", json={"title": "验收会话"}).json()["session_id"]
    assert client.get(f"/api/sessions/{sid}").json()["session"]["title"] == "验收会话"
    assert client.get("/api/sessions").json()["total"] >= 1
    print("   ✓ 创建 / 读取 / 列表")

    print("④ 行程落库与读回（Pydantic 往返）...")
    trip = _make_trip("verify-trip-1")
    TripRepo(conn_factory=lambda: connect(settings)).save(verify_uid, trip)
    got = client.get("/api/trips/verify-trip-1")
    assert got.status_code == 200 and got.json()["title"] == "验收行程", got.text
    items = client.get("/api/trips").json()
    assert any(i["trip_id"] == "verify-trip-1" for i in items["items"])
    print(f"   ✓ 详情 + 列表（total={items['total']}）")

    print("⑤ 重启模拟：新 app 实例（= 新进程）里读回来 ...")
    app_b = create_app()
    client_b = TestClient(app_b)
    client_b.headers.update({"Authorization": f"Bearer {token}"})
    assert client_b.get(f"/api/sessions/{sid}").json()["session"]["title"] == "验收会话", (
        "新实例读不到 = 会话存在进程里 = 重启会丢 —— 存储层有 bug"
    )
    assert client_b.get("/api/trips/verify-trip-1").status_code == 200
    print("   ✓ 会话与行程都在（重启不丢）")

    print("⑥ 清理验收数据...")
    assert client_b.delete(f"/api/sessions/{sid}").status_code == 204
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM trips WHERE id = %s", ("verify-trip-1",))
        cur.execute("DELETE FROM users WHERE username = %s", ("verify_user",))
    print("   ✓ 已删")

    print("\n✅ M5 真库验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
