import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import type { SpotCard } from '../types/contract'
import { addFavorite, listSpots, searchSpots, ApiError } from '../lib/api'
import { getToken } from '../lib/auth'
import { typecodeLabel } from '../lib/typecode'

/** 默认态快捷搜索词（19.4）：搜索入口不是数据，点 = 填词 + 真实搜索一次 */
const QUICK_WORDS = ['宽窄巷子', '大熊猫繁育研究基地', '武侯祠', '锦里', '金沙遗址', '人民公园', '文殊院', '东郊记忆']

/** 城市 tab（20.8 方案 B 本地过滤）：tab 显示短名，库里 city 是高德原值（带「市」后缀）——
 *  🔴 必须走这张映射表，禁止用 includes 糊匹配 */
const CITY_TABS = [
  { label: '全部', value: '' },
  { label: '北京', value: '北京市' },
  { label: '上海', value: '上海市' },
  { label: '杭州', value: '杭州市' },
  { label: '苏州', value: '苏州市' },
  { label: '南京', value: '南京市' },
  { label: '西安', value: '西安市' },
  { label: '重庆', value: '重庆市' },
  { label: '广州', value: '广州市' },
  { label: '厦门', value: '厦门市' },
]

/** 默认态网格初始显示条数（20.6）与每次「加载更多」追加量 */
const PAGE_SIZE = 20

function ResultCard({ s, hasToken }: { s: SpotCard; hasToken: boolean }) {
  const nav = useNavigate()
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

  const typeLabel = typecodeLabel(s.typecode)

  return (
    <div
      className="card p-4 flex gap-4 items-start cursor-pointer transition-colors hover:bg-pop-yellow/20"
      onClick={() => nav(`/spots/${s.poi_id}`)} // 🔴 整卡可点进详情（19.3）
    >
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
          {typeLabel && <span className="px-2 py-0.5 border border-ink rounded-[2px] text-muted">{typeLabel}</span>}
          {hasToken && (
            <>
              <button
                className="btn-outline !text-[11px] !px-2.5 !py-1"
                onClick={(e) => {
                  e.stopPropagation() // 🔴 点收藏不触发卡片跳转（19.3）
                  void onFavorite()
                }}
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

/** 默认态封面卡（与首页「猜你喜欢」同款视觉）：整卡可点进详情（GET /spots，2026-09-18） */
function CoverCard({ s }: { s: SpotCard }) {
  const nav = useNavigate()
  const color = ['pop-yellow', 'pop-cyan', 'pop-blue', 'pop-green'][s.poi_id.length % 4]
  return (
    <div
      className="card overflow-hidden cursor-pointer transition-transform hover:raise"
      onClick={() => nav(`/spots/${s.poi_id}`)}
      title={s.name}
    >
      {/* 封面：photos[0] 真图（onError 隐藏，回退色块+站名，不用灰底占位图） */}
      <div
        className="h-[150px] flex items-center justify-center relative overflow-hidden"
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
        <span className="font-display font-bold text-[26px] tracking-wide uppercase">{s.name.slice(0, 4)}</span>
      </div>
      <div className="p-4 pb-[18px]">
        <div className="flex justify-between items-center gap-2">
          <h3 className="font-bold text-[14px] truncate">{s.name}</h3>
          {s.rating && <b className="text-[13px] text-muted shrink-0">评分 {s.rating}</b>}
        </div>
        <div className="mt-1 text-[12px] text-muted truncate">{s.district ?? s.city}</div>
        {s.cost_per_person !== null && <div className="mt-0.5 text-[12px] text-muted">人均 ¥{s.cost_per_person}</div>}
      </div>
    </div>
  )
}

/** 景点页：默认态平铺收录库（GET /spots），搜索后切结果列表（D70：搜的是本站收录） */
export default function Spots() {
  // 登录后跳回本页（路由变化）→ 重渲染 → hasToken 重新求值（P3：不再只算一次）
  useLocation()
  const hasToken = !!getToken()
  const [searchParams, setSearchParams] = useSearchParams()
  const urlKw = searchParams.get('kw') ?? '' // 导航栏搜索框带过来的词（15.3）
  /** 选中城市（20.8）：存高德原值「北京市」，由 URL 驱动 —— 后退/刷新/分享链接都不丢；「全部」= 空 */
  const cityValue = searchParams.get('city') ?? ''
  const [keyword, setKeyword] = useState(urlKw)
  const [city, setCity] = useState('')
  const [items, setItems] = useState<SpotCard[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [meta, setMeta] = useState<{ source: string; cached: boolean } | null>(null)
  // 请求序列保护：自动搜索（URL 参数）与手动搜索并发时，只认最后一次发出的（过期响应丢弃）
  const seqRef = useRef(0)
  // 默认态数据（20.8 方案 B）：一次拉全量到本地，tab 秒切零请求；显示层本地截取（20.6）
  const [list, setList] = useState<SpotCard[] | null>(null)
  const [listTotal, setListTotal] = useState(0)
  const [listError, setListError] = useState<string | null>(null)
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE)

  useEffect(() => {
    let alive = true
    listSpots(100) // 🔴 一次拉全量（20.8）；后端 limit 上限需 ≥100，否则 400 → 见内部后端交接清单 R2
      .then((res) => {
        if (!alive) return
        setList(res.items)
        setListTotal(res.total)
      })
      .catch((e: unknown) => {
        if (alive) setListError(e instanceof Error ? e.message : String(e))
      })
    return () => {
      alive = false
    }
  }, [])

  /** 当前 tab 下的过滤结果（本地过滤，零请求）；「全部」= 全量 */
  const filtered = list ? (cityValue === '' ? list : list.filter((s) => s.city === cityValue)) : []
  /** 切 tab：offset 归零（重新从 20 条起）+ 城市写进 URL（全部 = 清参数） */
  const switchCity = (value: string) => {
    setVisibleCount(PAGE_SIZE)
    if (value === '') {
      setSearchParams((p) => {
        const q = new URLSearchParams(p)
        q.delete('city')
        return q
      })
    } else {
      setSearchParams((p) => {
        const q = new URLSearchParams(p)
        q.set('city', value)
        return q
      })
    }
  }
  /** 加载更多：本地截取加 20（20.6 语义，切 tab 后归零） */
  const loadMore = () => setVisibleCount((c) => c + PAGE_SIZE)

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

      {/* 默认态：平铺收录库封面卡（与首页猜你喜欢同款视觉），整卡可点进详情。
          数据来自 GET /spots（真实收录库），不是硬凑 —— total 也是后端给的。
          城市 tabs（20.8）：本地过滤、写 URL；快捷词保留：点 = 填词 + 真实搜索一次 */}
      {!error && items === null && !loading && (
        <div className="mt-8">
          {listError && <div className="text-status-fail text-[13px]">收录列表加载失败：{listError}</div>}
          {!listError && list === null && <div className="text-muted">加载中…</div>}
          {list !== null && (
            <>
              {/* 城市 tabs（20.8）：选中写 URL /spots?city=原值；只作用于默认态网格 */}
              <div className="flex flex-wrap items-center gap-2 mb-4">
                {CITY_TABS.map((t) => (
                  <button
                    key={t.label}
                    className={
                      cityValue === t.value
                        ? 'btn-outline !text-[13px] !px-3 !py-1.5 bg-pop-yellow !border-ink'
                        : 'btn-outline !text-[13px] !px-3 !py-1.5'
                    }
                    onClick={() => switchCity(t.value)}
                  >
                    {t.label}
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-3 mb-4 text-[12px] text-muted">
                <span>共收录 {cityValue === '' ? listTotal : filtered.length} 个景点</span>
                <span>点击卡片查看详情</span>
              </div>
              {filtered.length === 0 ? (
                <div className="mt-12 text-center text-muted">
                  {list.length === 0 ? '收录库暂无景点，试试上方搜索' : '该城市暂无收录景点，试试其他城市'}
                </div>
              ) : (
                <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
                  {filtered.slice(0, visibleCount).map((s) => (
                    <CoverCard key={s.poi_id} s={s} />
                  ))}
                </div>
              )}
              {/* 加载更多（20.6，本地截取）：已显示数 >= 过滤后 total 时按钮消失；切 tab 归零 */}
              {filtered.length > visibleCount && (
                <div className="mt-6 text-center">
                  <button className="btn-outline !text-[13px]" onClick={loadMore}>
                    加载更多
                  </button>
                </div>
              )}
              <div className="mt-8">
                <div className="font-bold text-[14px] mb-3">没找到想去的？搜一下</div>
                <div className="flex flex-wrap gap-2">
                  {QUICK_WORDS.map((w) => (
                    <button
                      key={w}
                      className="btn-outline !text-[13px] !px-3 !py-1.5"
                      onClick={() => {
                        setKeyword(w) // 🔴 点 = 填入输入框 + 真实搜索一次（19.4）
                        void runSearch(w, '')
                      }}
                    >
                      {w}
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}
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
