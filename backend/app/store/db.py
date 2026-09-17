"""MySQL 连接与建表（M5）。

═══════════════════════════════════════════════════════════════
 为什么是薄 SQL 而不是 ORM
═══════════════════════════════════════════════════════════════

这个项目的存储就三张表 + 两个 repo，查询全是"按 user_id 拿列表 / 按 id 拿一条"。
引 SQLAlchemy 换来的是：多一个依赖、多一层会话管理、多一套方言文档要读 ——
而它防的"手写 SQL 拼错字段"问题，用**查询结果直接喂 Pydantic 模型**这招就挡住了
（字段名错了 `Session.model_validate` 当场报错，不会静默）。

═══════════════════════════════════════════════════════════════
 两个实测定死的规矩（别改）
═══════════════════════════════════════════════════════════════

**① 排序规则必须 `utf8mb4_0900_ai_ci`。**
   驱动 `charset='utf8mb4'` 会发 `SET NAMES utf8mb4`，服务端把
   `collation_connection` 设成该字符集的**默认**排序规则 —— 就是 0900_ai_ci。
   compose 里若设成 unicode_ci，表列（unicode_ci）和连接字面量（0900_ai_ci）
   混用 → **1267 Illegal mix of collations**，代价一个下午的那种坑。
   所以 DDL 里每一张表都显式写 `COLLATE=utf8mb4_0900_ai_ci`。

**② checkpoint 四张表（checkpoints 等）不在这里建。**
   那是 `langgraph-checkpoint-mysql` 的地盘，M6 接 chat 时由 `saver.setup()`
   自动建（表名/迁移记录都归它管）。手写会跟它的迁移机制打架。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pymysql
import pymysql.cursors

from app.config import Settings

# ══════════════════════════════════════════════════════════════
#  DDL —— 幂等（IF NOT EXISTS），重复跑无害
# ══════════════════════════════════════════════════════════════

_DDL_TABLES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        username      VARCHAR(32)  NOT NULL,
        password_hash VARCHAR(100) NOT NULL,
        nickname      VARCHAR(32)  NULL,
        email         VARCHAR(128) NULL,
        avatar        VARCHAR(255) NULL,
        created_at    DATETIME(6)  NOT NULL,
        UNIQUE KEY uq_users_username (username)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id         CHAR(36)     NOT NULL,
        user_id    INT          NOT NULL,
        title      VARCHAR(100) NOT NULL,
        model      VARCHAR(40)  NULL,
        created_at DATETIME(6)  NOT NULL,
        updated_at DATETIME(6)  NOT NULL,
        PRIMARY KEY (id),
        KEY idx_sessions_user (user_id, updated_at DESC)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id         CHAR(36)    NOT NULL,
        session_id CHAR(36)    NOT NULL,
        role       VARCHAR(16) NOT NULL,
        content    MEDIUMTEXT  NOT NULL,
        meta_json  JSON        NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        KEY idx_messages_session (session_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS trips (
        id          CHAR(36)      NOT NULL,
        session_id  CHAR(36)      NULL,
        user_id     INT           NOT NULL,
        title       VARCHAR(200)  NOT NULL,
        destination VARCHAR(100)  NOT NULL,
        source      VARCHAR(16)   NOT NULL,
        trip_json   JSON          NOT NULL,
        created_at  DATETIME(6)   NOT NULL,
        updated_at  DATETIME(6)   NOT NULL,
        PRIMARY KEY (id),
        KEY idx_trips_user (user_id, created_at DESC),
        KEY idx_trips_session (session_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS guides (
        id           CHAR(36)      NOT NULL,
        user_id      INT           NOT NULL,
        title        VARCHAR(100)  NOT NULL,
        content_md   MEDIUMTEXT    NOT NULL,
        destination  VARCHAR(100)  NULL,
        cover        VARCHAR(500)  NULL,
        poi_ids      JSON          NULL,
        visibility   VARCHAR(16)   NOT NULL DEFAULT 'private',
        published_at DATETIME(6)   NULL,
        created_at   DATETIME(6)   NOT NULL,
        updated_at   DATETIME(6)   NOT NULL,
        source_trip_id  CHAR(36)    NULL,
        idempotency_key VARCHAR(64) NULL,
        PRIMARY KEY (id),
        KEY idx_guides_user (user_id, created_at DESC),
        KEY idx_guides_pub (visibility, published_at DESC),
        KEY idx_guides_src (user_id, source_trip_id),
        UNIQUE KEY uq_guides_idem (user_id, idempotency_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS comments (
        id          CHAR(36)    NOT NULL,
        user_id     INT         NOT NULL,
        target_type VARCHAR(16) NOT NULL,
        target_id   CHAR(36)    NOT NULL,
        content     VARCHAR(500) NOT NULL,
        created_at  DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        KEY idx_comments_target (target_type, target_id, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS likes (
        user_id     INT         NOT NULL,
        target_type VARCHAR(16) NOT NULL,
        target_id   CHAR(36)    NOT NULL,
        created_at  DATETIME(6) NOT NULL,
        PRIMARY KEY (user_id, target_type, target_id),
        KEY idx_likes_target (target_type, target_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS favorites (
        user_id     INT          NOT NULL,
        target_type VARCHAR(16)  NOT NULL,
        target_id   VARCHAR(64)  NOT NULL,
        name        VARCHAR(200) NOT NULL,
        cover       VARCHAR(500) NULL,
        created_at  DATETIME(6)  NOT NULL,
        PRIMARY KEY (user_id, target_type, target_id),
        KEY idx_favorites_user (user_id, created_at DESC)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
)


def connect(settings: Settings) -> pymysql.connections.Connection:
    """开一条同步连接（repo 用）。

    ⚠️ `autocommit=True`：repo 每个方法就是一条自洽的语句，
    没有多语句事务；开着它省掉"忘了 commit"这类静默丢写。
    M6 chat 落库若需要"消息 + 行程"原子写，再在那个方法里显式开事务。
    """
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_db,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


_DDL_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (表名, 判据列名, 迁移语句)。⚠️ CREATE TABLE IF NOT EXISTS **不会**给已存在的
    # 表加列 —— 存量库靠这里补。判据查 information_schema，天然幂等。
    #
    # ⚠️ 表名必须放在元组里（2026-09-17 修）：此前判据 SQL 把 'sessions' 写死，
    # 于是从第二条迁移起判据永远查错表 → 每次都判"列不存在" → 重复执行 ALTER →
    # 第二次 `Duplicate column name` 直接把 init_db 打崩。**加迁移前先读这条**。
    (
        "sessions",
        "model",
        "ALTER TABLE sessions ADD COLUMN model VARCHAR(40) NULL AFTER title",
    ),
    (
        "guides",
        "source_trip_id",
        "ALTER TABLE guides ADD COLUMN source_trip_id CHAR(36) NULL AFTER updated_at, "
        "ADD KEY idx_guides_src (user_id, source_trip_id)",
    ),
    (
        "guides",
        "idempotency_key",
        "ALTER TABLE guides ADD COLUMN idempotency_key VARCHAR(64) NULL AFTER source_trip_id, "
        "ADD UNIQUE KEY uq_guides_idem (user_id, idempotency_key)",
    ),
)


def init_db(settings: Settings) -> None:
    """建库建表（幂等）。部署/验收脚本跑一次即可。"""
    bare = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with bare.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_db}` "
                "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
            )
        bare.select_db(settings.mysql_db)
        with bare.cursor() as cur:
            for ddl in _DDL_TABLES:
                cur.execute(ddl)
            for table, column, ddl in _DDL_MIGRATIONS:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
                    (settings.mysql_db, table, column),
                )
                # 裸连接不是 DictCursor，fetchone() 是元组 —— 用下标别用键名
                if int(cur.fetchone()[0]) == 0:
                    cur.execute(ddl)
    finally:
        bare.close()


def utc_now() -> datetime:
    """统一用 UTC 存（列是 DATETIME 无时区）——读出来时统一补回 UTC 时区。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def aware(dt: datetime | None) -> datetime | None:
    """DB 读出的 naive datetime → 补 UTC 时区（契约要求 ISO 8601 带时区）。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc)


__all__ = ["connect", "init_db", "utc_now", "aware"]
