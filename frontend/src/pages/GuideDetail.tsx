import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import type { CommentItem, GuideDetail as GuideDetailType } from '../types/contract'
import {
  addFavorite,
  createComment,
  deleteComment,
  getGuide,
  listComments,
  toggleLike,
} from '../lib/api'
import { getToken } from '../lib/auth'
import { streamSSE } from '../lib/sse'

/**
 * 攻略详情（M10 + M11）：
 * - 详情 GET /guides/{id}（public 直通，private 校验归属）
 * - 点赞 POST /likes/toggle 二合一，返回最终状态直接用
 * - 收藏 POST /favorites（guide 别传 name，后端取标题）
 * - 评论列表 / 发评论 / 删评论（is_mine 后端算好）
 */
export default function GuideDetail() {
  const { id = '' } = useParams<{ id: string }>()
  const nav = useNavigate()
  const hasToken = !!getToken()

  const [guide, setGuide] = useState<GuideDetailType | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // 点赞
  const [liked, setLiked] = useState(false)
  const [likeCount, setLikeCount] = useState(0)
  const [likeBusy, setLikeBusy] = useState(false)
  const [favMsg, setFavMsg] = useState<string | null>(null)

  // 评论
  const [comments, setComments] = useState<CommentItem[]>([])
  const [commentText, setCommentText] = useState('')
  const [commentBusy, setCommentBusy] = useState(false)
  const [commentError, setCommentError] = useState<string | null>(null)

  // 「用它做路线」（M12 · 14.4）：content_md → paste SSE → 跳 /overview
  const [pasteBusy, setPasteBusy] = useState(false)
  const [pasteMsg, setPasteMsg] = useState<string | null>(null)
  const acRef = useRef<AbortController | null>(null)

  const load = useCallback(() => {
    let alive = true
    setLoading(true)
    setError(null)
    getGuide(id)
      .then((g) => {
        if (!alive) return
        setGuide(g)
        setLiked(g.liked)
        setLikeCount(g.like_count)
      })
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false))
    listComments(id)
      .then((p) => alive && setComments(p.items))
      .catch(() => alive && setCommentError('评论加载失败'))
    return () => {
      alive = false
    }
  }, [id])

  useEffect(() => load(), [load])

  // 卸载时取消进行中的 paste 流
  useEffect(() => {
    return () => acRef.current?.abort()
  }, [])

  const onUseAsRoute = async () => {
    if (!guide || pasteBusy) return
    // ⚠️ paste 文本上限 20000 字：超长先提示，别让请求白跑（交接文档 14.4）
    if (guide.content_md.length > 20000) {
      setPasteMsg(`这篇攻略约 ${guide.content_md.length} 字，超过粘贴上限 20000 字，无法直接生成路线。`)
      return
    }
    setPasteBusy(true)
    setPasteMsg(null)
    const ac = new AbortController()
    acRef.current = ac
    try {
      // 后端零改动：content_md 直接当 paste 的 text 喂（同一套 SSE 事件流）
      await streamSSE('/api/trips/paste', { text: guide.content_md }, () => {}, ac.signal)
      nav('/overview')
    } catch (e: unknown) {
      setPasteMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setPasteBusy(false)
      acRef.current = null
    }
  }

  const onToggleLike = async () => {
    if (!hasToken) return
    setLikeBusy(true)
    try {
      const st = await toggleLike({ target_type: 'guide', target_id: id })
      setLiked(st.liked)
      setLikeCount(st.count)
    } catch (e: unknown) {
      setFavMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setLikeBusy(false)
    }
  }

  const onFavorite = async () => {
    if (!hasToken) return
    setFavMsg(null)
    try {
      // 🔴 收 guide 别传 name —— 后端取标题（api.md 2.6 / 交接文档十一节）
      await addFavorite({ target_type: 'guide', target_id: id, name: null, cover: guide?.cover ?? null })
      setFavMsg('已收藏')
    } catch (e: unknown) {
      setFavMsg(e instanceof Error ? e.message : String(e))
    }
  }

  const onSubmitComment = async () => {
    const content = commentText.trim()
    if (!content) return
    setCommentBusy(true)
    setCommentError(null)
    try {
      const c = await createComment(id, content)
      setComments((prev) => [...prev, c])
      setCommentText('')
    } catch (e: unknown) {
      setCommentError(e instanceof Error ? e.message : String(e))
    } finally {
      setCommentBusy(false)
    }
  }

  const onDeleteComment = async (commentId: string) => {
    try {
      await deleteComment(commentId)
      setComments((prev) => prev.filter((c) => c.comment_id !== commentId))
    } catch (e: unknown) {
      setCommentError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="relative px-14 py-10">
      <div className="deco deco-square" style={{ width: 18, height: 18, top: 84, right: 96 }} />

      <div className="text-[13px] text-muted mb-3">
        <Link to="/guides" className="hover:underline underline-offset-4">← 返回攻略列表</Link>
      </div>

      {loading && <div className="text-muted text-[13px]">加载中…</div>}
      {error && !loading && (
        <div className="card mt-4 p-10 text-center">
          <div className="font-display font-bold text-lg mb-2">加载失败</div>
          <p className="text-muted text-[14px]">{error}</p>
          <Link to="/guides" className="btn-outline mt-6">返回列表</Link>
        </div>
      )}

      {guide && !loading && (
        <>
          {/* 标题区 */}
          <div className="flex items-start justify-between gap-6">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                {guide.visibility === 'private' && <span className="badge" style={{ background: 'var(--color-pop-red)' }}>私密</span>}
                <span className="text-[12px] text-muted">{guide.author_name}{guide.author_type === 'system' ? ' · 官方' : ''}</span>
                {guide.published_at && <span className="text-[12px] text-muted">发布于 {guide.published_at.slice(0, 10)}</span>}
              </div>
              <h1 className="font-display font-bold text-[32px] mt-1">{guide.title}</h1>
              {guide.destination && <p className="text-[14px] text-muted mt-1">目的地：{guide.destination}</p>}
            </div>
            {/* 互动按钮 */}
            <div className="flex gap-3 shrink-0">
              {hasToken ? (
                <>
                  <button className={liked ? 'btn-black !text-[12px] !px-4 !py-2' : 'btn-outline !text-[12px] !px-4 !py-2'} onClick={() => void onToggleLike()} disabled={likeBusy}>
                    {liked ? `已赞 ${likeCount}` : `赞 ${likeCount}`}
                  </button>
                  <button className="btn-outline !text-[12px] !px-4 !py-2" onClick={() => void onFavorite()}>
                    收藏
                  </button>
                  <button className="btn-black !text-[12px] !px-4 !py-2" onClick={() => void onUseAsRoute()} disabled={pasteBusy}>
                    {pasteBusy ? '生成中…' : '用它做路线'}
                  </button>
                </>
              ) : (
                <Link to="/login" className="btn-outline !text-[12px] !px-4 !py-2">登录后互动</Link>
              )}
            </div>
          </div>
          {favMsg && <div className="text-[12px] text-muted mt-2">{favMsg}</div>}
          {pasteMsg && <div className="text-[12px] text-muted mt-2">{pasteMsg}</div>}

          {/* 正文 */}
          {guide.cover && (
            <img src={guide.cover} alt="" className="mt-6 w-full max-h-72 object-cover border border-ink rounded-[4px]" onError={(e) => { e.currentTarget.style.display = 'none' }} />
          )}
          <div className="card mt-6 p-6">
            {guide.summary && <p className="font-bold text-[15px] mb-3">{guide.summary}</p>}
            {guide.content_md ? (
              <pre className="whitespace-pre-wrap font-sans text-[14px] leading-6 text-ink">{guide.content_md}</pre>
            ) : (
              <p className="text-muted text-[13px]">（正文为空）</p>
            )}
          </div>

          {/* 评论 */}
          <div className="card mt-6 p-6">
            <h2 className="font-display font-bold text-lg mb-3">评论（{comments.length}）</h2>

            {hasToken ? (
              <div className="flex gap-2 mb-4">
                <input
                  className="field"
                  placeholder="写一条评论…"
                  value={commentText}
                  onChange={(e) => setCommentText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') void onSubmitComment()
                  }}
                />
                <button className="btn-black !text-[12px] !px-4 !py-2 shrink-0" onClick={() => void onSubmitComment()} disabled={commentBusy || !commentText.trim()}>
                  {commentBusy ? '发送中' : '发送'}
                </button>
              </div>
            ) : (
              <div className="text-muted text-[13px] mb-4">
                <Link to="/login" className="text-ink font-bold underline-offset-4">登录</Link> 后参与评论
              </div>
            )}
            {commentError && <div className="text-status-fail text-[12px] mb-3">{commentError}</div>}

            {comments.length === 0 ? (
              <div className="text-muted text-[13px]">还没有评论，来抢沙发。</div>
            ) : (
              <ul className="divide-y divide-ink/10">
                {comments.map((c) => (
                  <li key={c.comment_id} className="py-3 flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-[12px] text-muted">
                        {c.author_name}{c.author_type === 'system' ? ' · 官方' : ''} · {c.created_at.slice(0, 16).replace('T', ' ')}
                      </div>
                      <div className="text-[14px] mt-0.5 break-words">{c.content}</div>
                    </div>
                    {c.is_mine && (
                      <button className="btn-outline !text-[11px] !px-2 !py-1 shrink-0" onClick={() => void onDeleteComment(c.comment_id)}>
                        删除
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  )
}
