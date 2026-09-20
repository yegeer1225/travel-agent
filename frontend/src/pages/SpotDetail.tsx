import { useEffect, useState } from 'react'
import { useNavigate, useParams, Link } from 'react-router-dom'
import type { SpotCard } from '../types/contract'
import { getSpot, addFavorite, ApiError } from '../lib/api'
import { getToken } from '../lib/auth'
import { typecodeLabel } from '../lib/typecode'

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
  // 20.10 lightbox：当前大图索引（null = 关闭）；遍历「仍可见」的图（onError 的从列表移除，索引不漂移）
  const [lightboxIdx, setLightboxIdx] = useState<number | null>(null)
  const [visiblePhotos, setVisiblePhotos] = useState<string[]>([])
  const [lbFailed, setLbFailed] = useState(false)

  useEffect(() => {
    if (spot) setVisiblePhotos(spot.photos)
  }, [spot])

  // 20.10：lightbox 打开时锁 body 滚动，关闭恢复
  useEffect(() => {
    if (lightboxIdx === null) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = prev
    }
  }, [lightboxIdx])

  // 20.10：键盘 ← → 切换、ESC 关闭（到头循环）
  useEffect(() => {
    if (lightboxIdx === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setLightboxIdx(null)
      else if (e.key === 'ArrowRight')
        setLightboxIdx((i) => (i === null ? i : (i + 1) % visiblePhotos.length))
      else if (e.key === 'ArrowLeft')
        setLightboxIdx((i) => (i === null ? i : (i + visiblePhotos.length - 1) % visiblePhotos.length))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [lightboxIdx, visiblePhotos.length])

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

  const typeLabel = typecodeLabel(spot.typecode)
  const addr = [spot.city, spot.district, spot.address].filter(Boolean).join(' · ')

  return (
    <div className="relative px-14 py-10">
      <div className="deco deco-tri" style={{ width: 32, height: 28, background: 'var(--color-pop-cyan)', top: 20, right: 96, transform: 'rotate(-15deg)' }} />
      <div className="deco deco-ring" style={{ width: 26, height: 26, bottom: 26, left: 72 }} />

      <button className="btn-outline !text-[13px] mb-6" onClick={() => nav(-1)}>
        ← 返回
      </button>

      <div className="flex flex-col gap-6 max-w-[880px]">
        {/* 照片墙（20.10）：可见图 onError 移除；点击任一张 → lightbox（遍历全部可见图，解锁第 5 张起） */}
        {visiblePhotos.length > 0 && (
          <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))' }}>
            {visiblePhotos.slice(0, 4).map((p, i) => (
              <img
                key={`${spot.poi_id}-${i}`}
                src={p}
                alt={`${spot.name} ${i + 1}`}
                className="w-full h-44 object-cover border border-ink rounded-[4px] cursor-zoom-in"
                onClick={() => {
                  setLbFailed(false)
                  setLightboxIdx(i)
                }}
                onError={(e) => {
                  e.currentTarget.style.display = 'none'
                  setVisiblePhotos((prev) => prev.filter((x) => x !== p))
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

      {/* 20.10 lightbox：全屏遮罩 + 居中大图 object-contain；× / 点空白 / ESC 关闭；← → 切换（循环）；计数 */}
      {lightboxIdx !== null && visiblePhotos.length > 0 && (
        <div
          className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center"
          onClick={(e) => {
            if (e.target === e.currentTarget) setLightboxIdx(null) // 点遮罩空白关闭，点图不关
          }}
        >
          <button
            className="absolute top-5 right-5 w-11 h-11 flex items-center justify-center text-white text-[26px] leading-none hover:bg-white/10 rounded-[4px]"
            onClick={() => setLightboxIdx(null)}
            aria-label="关闭"
          >
            ×
          </button>
          <div className="absolute top-5 left-1/2 -translate-x-1/2 text-white/80 text-[13px]">
            {(lightboxIdx % visiblePhotos.length) + 1} / {visiblePhotos.length}
          </div>
          <button
            className="absolute left-4 top-1/2 -translate-y-1/2 w-11 h-11 flex items-center justify-center text-white text-[30px] leading-none hover:bg-white/10 rounded-[4px]"
            onClick={() => {
              setLbFailed(false)
              setLightboxIdx((i) => (i === null ? i : (i + visiblePhotos.length - 1) % visiblePhotos.length))
            }}
            aria-label="上一张"
          >
            ←
          </button>
          <div className="relative w-[88vw] h-[82vh]">
            <img
              key={lightboxIdx}
              src={visiblePhotos[lightboxIdx % visiblePhotos.length]}
              alt={`${spot.name} 大图`}
              className={`w-full h-full object-contain ${lbFailed ? 'opacity-0' : ''}`}
              onLoad={() => setLbFailed(false)}
              onError={() => setLbFailed(true)}
            />
            {lbFailed && (
              <div className="absolute inset-0 flex items-center justify-center text-white/70 text-[14px]">
                图片加载失败
              </div>
            )}
          </div>
          <button
            className="absolute right-4 top-1/2 -translate-y-1/2 w-11 h-11 flex items-center justify-center text-white text-[30px] leading-none hover:bg-white/10 rounded-[4px]"
            onClick={() => {
              setLbFailed(false)
              setLightboxIdx((i) => (i === null ? i : (i + 1) % visiblePhotos.length))
            }}
            aria-label="下一张"
          >
            →
          </button>
        </div>
      )}
    </div>
  )
}
