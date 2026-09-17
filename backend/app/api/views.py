"""契约响应的拼装层：repo 记录 + 计数 → `GuideListItem` / `GuideDetail`。

**为什么单独一个模块**：拼 `GuideDetail` 的地方不止一处 —— 攻略 CRUD 在
`routes/guides.py`，而 `POST /trips/{trip_id}/publish-as-guide`（D61）在
`routes/trips.py` 也要拼同一个模型。

两边各写一遍的代价不是"重复代码难看"，而是**契约加字段时总有一处忘改，
而且不报错** —— 只是那个响应悄悄少一个字段（坑 15 的同类：不一致不会报错，
只会在前端表现为 `undefined`）。所以只留这一份实现，两处都调它。
"""

from __future__ import annotations

from app.schemas import GuideDetail, GuideListItem, TargetType


def summary_of(content_md: str) -> str:
    """正文前 80 字，**后端截**（前端截会出现半句话 + 一堆换行）。"""
    return " ".join(content_md.split())[:80]


def guide_list_item(rec, *, author_name: str, like_count: int, comment_count: int) -> GuideListItem:
    """记录 → 列表卡。`rec` 既可能是 `GuideRecord`，也可能是内存替身的 dict。"""
    g = (lambda k: rec[k]) if isinstance(rec, dict) else (lambda k: getattr(rec, k))
    return GuideListItem(
        guide_id=g("id"),
        title=g("title"),
        summary=summary_of(g("content_md")),
        destination=g("destination"),
        cover=g("cover"),
        author_name=author_name,
        author_type="user",
        visibility=g("visibility"),
        like_count=like_count,
        comment_count=comment_count,
        published_at=g("published_at"),
        created_at=g("created_at"),
        source_trip_id=g("source_trip_id"),
    )


def attach_counts(
    recs: list,
    user_id: int | None,
    like_repo,
    comment_repo,
    user_repo,
) -> list[GuideListItem]:
    """列表页批量拼装：作者名 + 点赞/评论数（批量 COUNT，不 N+1 到没法看）。"""
    ids = [r["id"] if isinstance(r, dict) else r.id for r in recs]
    like_map = like_repo.counts(TargetType.GUIDE.value, ids)
    comment_map = comment_repo.counts(TargetType.GUIDE.value, ids)
    author_cache: dict[int, str] = {}

    def _author(uid: int) -> str:
        if uid not in author_cache:
            u = user_repo.get_by_id(uid)
            author_cache[uid] = (u.nickname or u.username) if u else f"用户{uid}"
        return author_cache[uid]

    items: list[GuideListItem] = []
    for r in recs:
        is_dict = isinstance(r, dict)
        gid = r["id"] if is_dict else r.id
        uid = r["user_id"] if is_dict else r.user_id
        items.append(
            guide_list_item(
                r,
                author_name=_author(uid),
                like_count=like_map.get(gid, 0),
                comment_count=comment_map.get(gid, 0),
            )
        )
    return items


def guide_detail_view(rec, user_id: int | None, like_repo, comment_repo, user_repo) -> GuideDetail:
    """记录 → 详情。作者名、点赞态、评论数都在这里一次拼好。"""
    gid = rec["id"] if isinstance(rec, dict) else rec.id
    owner = rec["user_id"] if isinstance(rec, dict) else rec.user_id
    u = user_repo.get_by_id(owner)
    liked, like_count = like_repo.state(user_id or 0, TargetType.GUIDE.value, gid)
    comment_count = comment_repo.counts(TargetType.GUIDE.value, [gid]).get(gid, 0)
    base = guide_list_item(
        rec,
        author_name=(u.nickname or u.username) if u else f"用户{owner}",
        like_count=like_count,
        comment_count=comment_count,
    )
    return GuideDetail(
        **base.model_dump(),
        content_md=rec["content_md"] if isinstance(rec, dict) else rec.content_md,
        poi_ids=rec["poi_ids"] if isinstance(rec, dict) else rec.poi_ids,
        liked=liked,
    )


__all__ = ["attach_counts", "guide_detail_view", "guide_list_item", "summary_of"]
