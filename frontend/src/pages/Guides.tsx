import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { GuideListItem } from '../types/contract'
import { listGuides } from '../lib/api'
import { getToken } from '../lib/auth'

/** 旅游攻略 · 列表（M10）：默认只回 public；「我的」tab 带 mine=1 */
export default function Guides() {
  const [tab, setTab] = useState<'discover' | 'mine'>('discover')
  const [items, setItems] = useState<GuideListItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const hasToken = !!getToken()

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(null)
    listGuides(tab === 'mine' ? { mine: '1' } : {})
      .then((p) => alive && setItems(p.items))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [tab])

  return (
    <div className="relative px-14 py-10">
      <div className="deco deco-circle" style={{ width: 12, height: 12, background: 'var(--color-pop-red)', top: 72, left: 852 }} />
      <div className="deco deco-dots" style={{ width: 80, height: 44, bottom: 22, right: 140, opacity: 0.6 }} />

      <h1 className="font-display font-bold text-[34px]">旅游攻略</h1>
      <p className="text-[14px] text-muted mt-1">攻略社区 · 与 AI 路线互转</p>

      {/* 发现 / 我的 tab */}
      <div className="flex gap-3 mt-6">
        <button
          className={tab === 'discover' ? 'tab-btn tab-btn-active' : 'tab-btn'}
          onClick={() => setTab('discover')}
        >
          发现
        </button>
        {hasToken && (
          <button
            className={tab === 'mine' ? 'tab-btn tab-btn-active' : 'tab-btn'}
            onClick={() => setTab('mine')}
          >
            我的
          </button>
        )}
      </div>

      {loading && <div className="text-muted text-[13px] mt-6">加载中…</div>}
      {error && <div className="text-status-fail text-[13px] mt-6">加载失败：{error}</div>}
      {!loading && !error && items.length === 0 && (
        <div className="card mt-6 p-10 text-center">
          <div className="font-display font-bold text-lg mb-2">
            {tab === 'mine' ? '你还没有发布攻略' : '还没有攻略'}
          </div>
          <p className="text-muted text-[14px] leading-6 max-w-[520px] mx-auto">
            {tab === 'mine'
              ? '发布的攻略默认仅自己可见，发布后才会出现在「发现」列表。'
              : '攻略社区内容更新中，稍后再来看看。你也可以去生成一份 AI 路线。'}
          </p>
          {tab === 'mine' ? (
            <Link to="/assistant" className="btn-black inline-block mt-6 !text-[12px] !px-5 !py-2.5">
              去生成行程
            </Link>
          ) : (
            <Link to="/assistant" className="btn-outline mt-6">
              去 AI 助手
            </Link>
          )}
        </div>
      )}

      {/* 攻略卡片列表 */}
      {!loading && !error && items.length > 0 && (
        <div className="grid grid-cols-2 gap-5 mt-6">
          {items.map((g) => (
            <Link key={g.guide_id} to={`/guides/${g.guide_id}`} className="card raise raise-yellow block p-0 overflow-hidden group">
              {g.cover ? (
                <img src={g.cover} alt="" className="w-full h-36 object-cover border-b border-ink" onError={(e) => { e.currentTarget.style.display = 'none' }} />
              ) : (
                <div className="w-full h-36 bg-pop-cyan flex items-center justify-center font-display font-bold text-3xl border-b border-ink">
                  {(g.destination ?? g.title).slice(0, 1)}
                </div>
              )}
              <div className="p-4">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="font-display font-bold text-[16px] truncate">{g.title}</h3>
                  {g.destination && <span className="badge shrink-0">{(g.destination ?? '').slice(0, 8)}</span>}
                </div>
                <p className="text-[13px] text-muted mt-1.5 line-clamp-2 leading-5 min-h-[40px]">{g.summary || '暂无摘要'}</p>
                <div className="flex items-center justify-between mt-3 text-[12px] text-muted">
                  <span className="truncate">{g.author_name}{g.author_type === 'system' ? ' · 官方' : ''}</span>
                  <span className="shrink-0">
                    ♡ {g.like_count} · 评论 {g.comment_count}
                    {g.published_at && ` · ${g.published_at.slice(0, 10)}`}
                  </span>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
