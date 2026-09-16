"""M9 后半 + M10 + M11 真库验收：社区/收藏/评论/点赞/头像 在 MySQL 上走一遍。

═══════════════════════════════════════════════════════════════
 什么时候跑
═══════════════════════════════════════════════════════════════

pytest **不连数据库**（requirements.txt 的纪律）—— 社区四张新表的真 SQL
只有这个脚本能验证。与 verify_mysql_api.py 同款跑法：

    cd backend && ../.venv/Scripts/python.exe scripts/verify_social_api.py

前置：Docker 的 MySQL 起来 + `init_db` 已建新表（脚本会自己调 init_db，幂等）。

覆盖的行为（pytest 的内存替身只验"同签名"，这里验"真 SQL 长一样"）：
- guides：建（默认 private）→ 公共列表不可见 → 发布 → 可见 → 撤回 → 删
- comments：发/删（版主语义不在此验，那是纯路由逻辑，pytest 已覆盖）
- likes：toggle 二合一 + 计数聚合
- favorites：poi 快照 + 幂等 + 删除
- uploads/avatar：真解码重编码 + 落盘 + 静态取回（产物跑完即删）
- /home 与 /spots/search 的真 provider 链路
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app.api.main import create_app  # noqa: E402
from app.api.ratelimit import SlidingWindowLimiter  # noqa: E402
from app.api.security import hash_password  # noqa: E402
from app.config import settings  # noqa: E402
from app.store.db import connect, init_db  # noqa: E402
from app.store.repo import UserRepo  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(f"{'PASS' if cond else 'FAIL'} {name} {extra}")


def main() -> int:
    init_db(settings)

    conn = connect(settings)
    with conn.cursor() as cur:
        # 幂等造账号（重跑无害）；密码每次重设，防上次跑挂了留下半截状态
        cur.execute(
            "INSERT INTO users (username, password_hash, nickname, created_at) "
            "VALUES (%s, %s, %s, NOW(6)) ON DUPLICATE KEY UPDATE password_hash = VALUES(password_hash)",
            ("verify_social", hash_password("Verify123456"), "社验"),
        )
        cur.execute("SELECT id FROM users WHERE username = 'verify_social'")
        uid = int(cur.fetchone()["id"])
    conn.close()

    app = create_app(limiter=SlidingWindowLimiter())
    c = TestClient(app)

    from app.api.security import create_token

    c.headers.update({"Authorization": f"Bearer {create_token(uid)[0]}"})

    # ── guides 全流程 ──
    r = c.post("/api/guides", json={"title": "验收攻略", "content_md": "正文内容用于摘要截取。" * 3, "destination": "成都"})
    check("建攻略 201 + private", r.status_code == 201 and r.json()["visibility"] == "private", r.text[:80])
    gid = r.json()["guide_id"]

    anon = TestClient(app)
    check("private 不进公共列表", anon.get("/api/guides").json()["total"] == 0)
    check("private 详情对匿名 404", anon.get(f"/api/guides/{gid}").status_code == 404)
    check("发布 200", c.post(f"/api/guides/{gid}/publish").status_code == 200)
    lst = anon.get("/api/guides", params={"keywords": "验收"}).json()
    check("发布后关键词可搜到", lst["total"] == 1 and lst["items"][0]["guide_id"] == gid)

    lk = c.post("/api/likes/toggle", json={"target_type": "guide", "target_id": gid}).json()
    check("点赞 liked+count=1", lk["liked"] and lk["count"] == 1)
    cm = c.post(f"/api/guides/{gid}/comments", json={"content": "验收评论"})
    check("发评论 201", cm.status_code == 201 and cm.json()["is_mine"] is True)
    detail = c.get(f"/api/guides/{gid}").json()
    check("详情计数", detail["like_count"] == 1 and detail["comment_count"] == 1)
    check("撤回 200", c.post(f"/api/guides/{gid}/unpublish").status_code == 200)
    check("撤回后公共列表空", anon.get("/api/guides").json()["total"] == 0)
    check("删评论 204", c.delete(f"/api/comments/{cm.json()['comment_id']}").status_code == 204)
    check("删攻略 204", c.delete(f"/api/guides/{gid}").status_code == 204)

    # ── favorites 快照与幂等（真库 INSERT IGNORE 路径）──
    r = c.post("/api/favorites", json={"target_type": "poi", "target_id": "B0FFF6X49V", "name": "成都太古里"})
    check("收藏 poi 200", r.status_code == 200, r.text[:80])
    r2 = c.post("/api/favorites", json={"target_type": "poi", "target_id": "B0FFF6X49V", "name": "成都太古里"})
    check("重复收藏幂等", r2.status_code == 200 and r2.json()["name"] == "成都太古里")
    check("删除 204", c.delete("/api/favorites/poi/B0FFF6X49V").status_code == 204)
    check("再删 404", c.delete("/api/favorites/poi/B0FFF6X49V").status_code == 404)

    # ── 头像上传 + 静态取回（产物跑完即删）──
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), (200, 60, 60)).save(buf, format="PNG")
    r = c.post("/api/uploads/avatar", files={"file": ("a.png", buf.getvalue(), "image/png")})
    check("头像上传 200", r.status_code == 200, r.text[:80])
    if r.status_code == 200:
        url = r.json()["url"]
        got = c.get(url)
        img = Image.open(io.BytesIO(got.content))
        check("取回 + 512x512 WebP", got.status_code == 200 and img.size == (512, 512))
        check("profile 存头像", c.patch("/api/auth/me", json={"avatar": url}).status_code == 200)
        avatar_file = Path(str(BACKEND)) / url.lstrip("/")
        avatar_file.unlink(missing_ok=True)  # 验收产物不留盘
        c.patch("/api/auth/me", json={"avatar": None})  # profile 里的死链也清掉
    else:
        check("取回 + 512x512 WebP", False, "上传就没成，跳过")

    # ── /home + /spots/search 真 provider 链路 ──
    r = anon.get("/api/home")
    d = r.json()
    check("/home 200 + 结构", r.status_code == 200 and "hero" in d and "recommended" in d,
          f"hero={len(d.get('hero', []))} rec={len(d.get('recommended', []))}")
    check("hero 与 recommended 不重复",
          {h["poi_id"] for h in d.get("hero", [])}.isdisjoint({s["poi_id"] for s in d.get("recommended", [])}))
    r = c.get("/api/spots/search", params={"keywords": "熊猫"})
    check("spots/search 200", r.status_code == 200 and len(r.json()["items"]) >= 1)
    pid = r.json()["items"][0]["poi_id"]
    check("/spots/{id} 200", c.get(f"/api/spots/{pid}").status_code == 200)
    check("/spots/{id} 未知 404", c.get("/api/spots/NOPE").status_code == 404)

    print(f"\n{'=' * 46}\n{len(PASSED)} PASS / {len(FAILED)} FAIL")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
