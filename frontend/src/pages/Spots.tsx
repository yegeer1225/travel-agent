import { useEffect, useRef, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import type { SpotCard } from '../types/contract'
import { addFavorite, searchSpots, ApiError } from '../lib/api'
import { getToken } from '../lib/auth'

function ResultCard({ s, hasToken }: { s: SpotCard; hasToken: boolean }) {
  const color = ['pop-yellow', 'pop-cyan', 'pop-blue', 'pop-green'][s.poi_id.length % 4]
  const [favMsg, setFavMsg] = useState<string | null>(null)
  const [favBusy, setFavBusy] = useState(false)

  const onFavorite = async () => {
    setFavBusy(true)
    setFavMsg(null)
    try {
      // 🔴 收 poi 必须传 name —— 当前 SpotCard 快照（api.md 2.6 / 交接文档十一节）
      await addFavorite({ target_type: 'poi', target_id: s.poi_id, name: s.name, cover: s.photos[0] ?? null })
      setFavMsg('已收藏')
    } catch (e: unknown) {
      setFavMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setFavBusy(false)
    }
  }

  return (
    <div className="card p-4 flex gap-4 items-start">
      {/* 左侧：photos[0] 真图（onError 隐藏，回退色块+站名，不用灰底占位图） */}
      <div
        className="w-24 h-20 shrink-0 flex items-center justify-center rounded-[4px] border border-ink relative overflow-hidden"
        style={{ background: `var(--color-${color})` }}
      >
        {s.photos[0] ? (
          <img
            src={s.photos[0]}
            alt={s.name}
            className="absolute inset-0 w-full h-full object-cover"
            onError={(e) => {
              e.currentTarget.style.display = 'none'
            }}
          />
        ) : null}
        <span className="font-display font-bold text-lg tracking-wide uppercase">{s.name.slice(0, 2)}</span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <h3 className="font-bold text-[15px] truncate">{s.name}</h3>
          {s.rating && <span className="text-[12px] text-muted shrink-0">评分 {s.rating}</span>}
        </div>
        <div className="text-[12px] text-muted mt-0.5">
          {[s.city, s.district, s.address].filter(Boolean).join(' · ')}
        </div>
        <div className="flex items-center gap-3 mt-2 text-[12px]">
          {s.cost_per_person !== null && <span>人均 ¥{s.cost_per_person}</span>}
          {s.typecode && <span className="px-2 py-0.5 border border-ink rounded-[2px] text-muted">{s.typecode}</span>}
          {hasToken && (
            <>
              <button
                className="btn-outline !text-[11px] !px-2.5 !py-1"
                onClick={() => void onFavorite()}
                disabled={favBusy}
              >
                {favBusy ? '收藏中' : favMsg === '已收藏' ? '✓ 已收藏' : '收藏'}
              </button>
              {favMsg && favMsg !== '已收藏' && <span className="text-status-fail">{favMsg}</span>}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/** 景点搜索页：搜索页不是列表页 —— 进来没有数据，默认态显示引导文案 */
export default function Spots() {
  // 登录后跳回本页（路由变化）→ 重渲染 → hasToken 重新求值（P3：不再只算一次）
  useLocation()
  const hasToken = !!getToken()
  const [searchParams] = useSearchParams()
  const urlKw = searchParams.get('kw') ?? '' // 导航栏搜索框带过来的词（15.3）
  const [keyword, setKeyword] = useState(urlKw)
  const [city, setCity] = useState('')
  const [items, setItems] = useState<SpotCard[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [meta, setMeta] = useState<{ source: string; cached: boolean } | null>(null)
  // 请求序列保护：自动搜索（URL 参数）与手动搜索并发时，只认最后一次发出的（过期响应丢弃）
  const seqRef = useRef(0)

  const runSearch = async (kw: string, cityName: string) => {
    const seq = ++seqRef.current
    const k = kw.trim()
    // 🔴 关键词为空时不要发请求（后端会返回 400 keyword_required）
    if (k === '') {
      setItems(null)
      setError(null)
      return
    }
    setLoading(true)
    setError(null)
    try {
      const res = await searchSpots(k, cityName.trim() === '' ? undefined : cityName.trim())
      if (seq !== seqRef.current) return // 已被更新的搜索取代，丢弃过期响应
      setItems(res.items)
      setTotal(res.total)
      setMeta({ source: res.source, cached: res.cached })
    } catch (err) {
      if (seq !== seqRef.current) return
      if (err instanceof ApiError && err.code === 'rate_limited') {
        setError(`操作太频繁，请 ${err.retryAfter ?? 60} 秒后重试`)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
      setItems(null)
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
  }

  const doSearch = (e?: React.FormEvent) => {
    e?.preventDefault()
    void runSearch(keyword, city)
  }

  // 导航栏带词跳入：灌进输入框 + 自动搜一次（依赖 urlKw，用户手动搜索不触发，互不打架）
  useEffect(() => {
    if (urlKw) {
      setKeyword(urlKw)
      void runSearch(urlKw, '')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlKw])

  return (
    <div className="relative px-14 py-10">
      <div className="deco deco-tri" style={{ width: 32, height: 28, background: 'var(--color-pop-green)', top: 20, right: 96, transform: 'rotate(20deg)' }} />
      <div className="deco deco-ring" style={{ width: 26, height: 26, bottom: 26, left: 72 }} />

      <h1 className="font-display font-bold text-[34px]">旅游景点</h1>
      <p className="text-[14px] text-muted mt-1">本站收录景点库 · 搜到即可收藏</p>

      {/* 搜索表单 */}
      <form onSubmit={doSearch} className="mt-6 flex items-center gap-3">
        <input
          className="flex-1 max-w-[480px] border border-ink rounded-[4px] px-4 py-3 text-[14px] outline-none focus:border-2"
          placeholder="输入景点名称或关键词开始搜索，例如「成都 博物馆」"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <input
          className="w-40 border border-ink rounded-[4px] px-3 py-3 text-[14px] outline-none"
          placeholder="城市（可选）"
          value={city}
          onChange={(e) => setCity(e.target.value)}
        />
        <button type="submit" className="btn-black" disabled={loading}>
          {loading ? '搜索中…' : '搜索'}
        </button>
      </form>

      {/* 错误提示（显示在表单下方） */}
      {error && <div className="mt-3 text-status-fail text-[13px]">{error}</div>}

      {/* 默认态：没有搜索关键词 → 引导文案，不硬凑卡片 */}
      {!error && items === null && !loading && (
        <div className="mt-16 text-center">
          <div className="font-display font-bold text-lg mb-2">输入关键词开始搜索</div>
          <p className="text-muted text-[14px]">
            例如「成都 博物馆」「广州 长隆」「西湖」… 搜索本站已收录的景点
          </p>
        </div>
      )}

      {loading && <div className="mt-8 text-muted">搜索中…</div>}

      {!loading && items !== null && items.length === 0 && (
        <div className="mt-16 text-center text-muted">本站暂未收录相关景点，换个关键词试试</div>
      )}

      {!loading && items !== null && items.length > 0 && (
        <div className="mt-8">
          <div className="flex items-center gap-3 mb-4 text-[12px] text-muted">
            <span>共 {total} 条结果</span>
            {meta && (
              <span>
                数据源 {meta.source === 'local' ? '本站收录' : meta.source === 'amap' ? '高德' : '模拟数据'}
              </span>
            )}
          </div>
          <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(2, 1fr)' }}>
            {items.map((s) => (
              <ResultCard key={s.poi_id} s={s} hasToken={hasToken} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
