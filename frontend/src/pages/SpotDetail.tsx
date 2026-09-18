import { useEffect, useState } from 'react'
import { useNavigate, useParams, Link } from 'react-router-dom'
import type { SpotCard } from '../types/contract'
import { getSpot, addFavorite, ApiError } from '../lib/api'
import { getToken } from '../lib/auth'

/** typecode 原始编码如「风景名胜;寺庙道观」→ 取最后一段显示（19.3） */
function lastType(t: string | null): string | null {
  if (!t) return null
  const parts = t.split(/[;；]/).map((x) => x.trim()).filter(Boolean)
  return parts.length ? parts[parts.length - 1] : null
}

/**
 * 景点详情页（19.2）：GET /spots/{poi_id}（🔴 需登录，401 由 request 弹窗重放）。
 * 照片墙用 photos 真图，onError 隐藏该图；无图不占位灰块。
 */
export default function SpotDetail() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const [spot, setSpot] = useState<SpotCard | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [favMsg, setFavMsg] = useState<string | null>(null)
  const [favBusy, setFavBusy] = useState(false)

  useEffect(() => {
    let alive = true
    setSpot(null)
    setError(null)
    getSpot(id)
      .then((s) => {
        if (alive) setSpot(s)
      })
      .catch((e: unknown) => {
        if (!alive) return
        if (e instanceof ApiError && e.code === 'not_found') {
          setError('not_found')
        } else {
          setError(e instanceof Error ? e.message : String(e))
        }
      })
    return () => {
      alive = false
    }
  }, [id])

  const onFavorite = async () => {
    if (!spot) return
    setFavBusy(true)
    setFavMsg(null)
    try {
      // 🔴 收 poi 必须传 name（当前 SpotCard 快照）；封面用 photos[0]（无图 null）
      await addFavorite({ target_type: 'poi', target_id: spot.poi_id, name: spot.name, cover: spot.photos[0] ?? null })
      setFavMsg('已收藏')
    } catch (e: unknown) {
      setFavMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setFavBusy(false)
    }
  }

  // 404：不白屏
  if (error === 'not_found') {
    return (
      <div className="relative px-14 py-10">
        <div className="mt-24 text-center">
          <div className="font-display font-bold text-2xl mb-3">未找到该景点</div>
          <p className="text-muted text-[14px] mb-6">景点不存在或已下架</p>
          <Link to="/spots" className="btn-black inline-block">
            ← 返回搜索页
          </Link>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="relative px-14 py-10">
        <div className="mt-24 text-center">
          <div className="font-display font-bold text-2xl mb-3">加载失败</div>
          <p className="text-muted text-[14px] mb-6">{error}</p>
          <Link to="/spots" className="btn-black inline-block">
            ← 返回搜索页
          </Link>
        </div>
      </div>
    )
  }

  if (!spot) {
    return <div className="px-14 py-10 text-muted">加载中…</div>
  }

  const typeLabel = lastType(spot.typecode)
  const addr = [spot.city, spot.district, spot.address].filter(Boolean).join(' · ')

  return (
    <div className="relative px-14 py-10">
      <div className="deco deco-tri" style={{ width: 32, height: 28, background: 'var(--color-pop-cyan)', top: 20, right: 96, transform: 'rotate(-15deg)' }} />
      <div className="deco deco-ring" style={{ width: 26, height: 26, bottom: 26, left: 72 }} />

      <button className="btn-outline !text-[13px] mb-6" onClick={() => nav(-1)}>
        ← 返回搜索结果
      </button>

      <div className="flex flex-col gap-6 max-w-[880px]">
        {/* 照片墙：真图 onError 隐藏该图；无图不放占位灰块 */}
        {spot.photos.length > 0 && (
          <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))' }}>
            {spot.photos.slice(0, 4).map((p, i) => (
              <img
                key={`${spot.poi_id}-${i}`}
                src={p}
                alt={`${spot.name} ${i + 1}`}
                className="w-full h-44 object-cover border border-ink rounded-[4px]"
                onError={(e) => {
                  e.currentTarget.style.display = 'none'
                }}
              />
            ))}
          </div>
        )}

        <div className="card p-6">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <h1 className="font-display font-bold text-[28px]">{spot.name}</h1>
              {addr && <p className="text-[13px] text-muted mt-1.5">{addr}</p>}
              <div className="flex items-center gap-3 mt-2 text-[12px] text-muted flex-wrap">
                {spot.rating && <span>评分 {spot.rating}</span>}
                {spot.cost_per_person !== null && <span>人均 ¥{spot.cost_per_person}</span>}
                {typeLabel && <span className="px-2 py-0.5 border border-ink rounded-[2px]">{typeLabel}</span>}
              </div>
            </div>
            {getToken() && (
              <button className="btn-outline shrink-0" onClick={() => void onFavorite()} disabled={favBusy}>
                {favBusy ? '收藏中' : favMsg === '已收藏' ? '✓ 已收藏' : '收藏'}
              </button>
            )}
          </div>
          {favMsg && favMsg !== '已收藏' && <div className="mt-2 text-status-fail text-[13px]">{favMsg}</div>}
        </div>
      </div>
    </div>
  )
}
