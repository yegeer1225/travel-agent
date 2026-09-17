import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { HomeResponse } from '../types/contract'
import { fetchHome } from '../lib/api'
import HeroCarousel from '../components/HeroCarousel'

function SpotCardView({
  name,
  city,
  district,
  rating,
  cost,
  color,
  raised,
  photos,
}: {
  name: string
  city: string | null
  district: string | null
  rating: string | null
  cost: number | null
  color: string
  raised: boolean
  photos: string[]
}) {
  return (
    <div className={`card overflow-hidden ${raised ? 'raise' : ''}`}>
      {/* 封面：photos[0] 真图（onError 隐藏，回退色块+站名，不用灰底占位图） */}
      <div
        className="h-[150px] flex items-center justify-center relative overflow-hidden"
        style={{ background: `var(--color-${color})` }}
      >
        {photos[0] ? (
          <img
            src={photos[0]}
            alt={name}
            className="absolute inset-0 w-full h-full object-cover"
            onError={(e) => {
              e.currentTarget.style.display = 'none'
            }}
          />
        ) : null}
        <span className="font-display font-bold text-[26px] tracking-wide uppercase">{name}</span>
      </div>
      <div className="p-4 pb-[18px]">
        <div className="flex justify-between items-center text-[13px] text-muted">
          <span>{district ?? city}</span>
          {rating && (
            <b className="text-ink font-bold">
              评分 {rating}
            </b>
          )}
        </div>
        {cost !== null && <div className="mt-1 text-[13px] text-muted">人均 ¥{cost}</div>}
        <div className="mt-3 h-[1px] bg-ink/15" />
        <div className="text-[12px] text-muted mt-2">高德真实景点数据</div>
      </div>
    </div>
  )
}

export default function Home() {
  const [data, setData] = useState<HomeResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    fetchHome()
      .then((d) => alive && setData(d))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  return (
    <div className="relative">
      {/* ── 装饰层（首页可略多，全部落留白处，不贴文字/按钮/卡片）── */}
      <div className="deco deco-ring" style={{ width: 48, height: 48, top: 120, left: 36 }} />
      <div className="deco deco-tri" style={{ width: 38, height: 34, background: 'var(--color-pop-blue)', top: 236, left: 600, transform: 'rotate(14deg)' }} />
      <div className="deco deco-circle" style={{ width: 140, height: 140, background: 'var(--color-pop-red)', opacity: 0.85, top: 16, right: -44 }} />
      <div className="deco deco-ring" style={{ width: 34, height: 34, top: 128, right: 252 }} />
      <div className="deco deco-half" style={{ width: 44, height: 88, background: 'var(--color-pop-green)', top: 316, right: 8 }} />
      <div className="deco deco-tri" style={{ width: 56, height: 48, background: 'var(--color-pop-cyan)', bottom: 14, right: 168, transform: 'rotate(200deg)' }} />
      <div className="deco deco-dots" style={{ width: 72, height: 40, bottom: 10, left: 26, opacity: 0.75 }} />

      {/* ── Hero ── */}
      <section className="relative px-14 pt-[72px] pb-[88px]">
        <div className="grid items-center gap-10 relative z-[2]" style={{ gridTemplateColumns: 'minmax(480px, 620px) minmax(0, 1fr)' }}>
          <div>
            <h1 className="font-display font-bold text-[62px] leading-[1.08] tracking-wide">
              AI 把行程
              <br />
              安排得明明白白
            </h1>
            <p className="mt-5 text-[17px] text-muted leading-[1.8] max-w-[540px]">
              说一句「成都 3 天，带爸妈」，自动排好每天去哪、几点到、路上多久、天气合不合适——还能拖拽改顺序，实时重算。
            </p>
            <div className="mt-9 flex items-center gap-[18px]">
              <Link to="/assistant" className="btn-black">
                开始规划
              </Link>
              <Link to="/overview" className="btn-outline">
                粘贴行程，秒出总览
              </Link>
            </div>
            <p className="mt-4 text-[13px] text-muted">高德真实数据 · 结构化行程 · 支持手动改</p>
          </div>

          <div className="relative h-[480px]">
            {loading && (
              <div className="absolute inset-0 flex items-center justify-center text-muted">加载中…</div>
            )}
            {error && (
              <div className="absolute inset-0 flex items-center justify-center text-status-fail text-sm">
                首页数据加载失败：{error}
              </div>
            )}
            {data && data.hero.length === 0 && (
              <div className="absolute inset-0 flex items-center justify-center text-muted text-sm">
                暂无推荐景点
              </div>
            )}
            {data && data.hero.length > 0 && (
              <div className="absolute inset-0">
                <HeroCarousel slides={data.hero} />
              </div>
            )}
          </div>
        </div>
      </section>

      {/* ── 猜你喜欢（静态 4 张卡，不做轮播）── */}
      <section className="relative px-14 pb-[72px]">
        <div className="relative z-[2] flex items-baseline gap-4 mb-8">
          <h2 className="font-display font-bold text-[34px]">猜你喜欢</h2>
          <span className="text-[14px] text-muted">高德真实景点数据</span>
        </div>

        {loading && <div className="text-muted">加载中…</div>}
        {error && <div className="text-status-fail text-sm">加载失败：{error}</div>}
        {data && data.recommended.length === 0 && (
          <div className="text-muted text-sm">暂无推荐</div>
        )}
        {data && data.recommended.length > 0 && (
          <div className="grid gap-6 relative z-[2]" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
            {data.recommended.slice(0, 4).map((r, i) => (
              <SpotCardView
                key={r.poi_id}
                name={r.name}
                city={r.city}
                district={r.district}
                rating={r.rating}
                cost={r.cost_per_person}
                color={['pop-yellow', 'pop-cyan', 'pop-blue', 'pop-green'][i % 4]}
                raised={i % 2 === 0}
                photos={r.photos}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
