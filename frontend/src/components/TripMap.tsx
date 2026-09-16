import { useEffect, useRef } from 'react'
import AMapLoader from '@amap/amap-jsapi-loader'
import type { Trip } from '../types/contract'

declare global {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  interface Window { _AMapSecurityConfig: { securityJsCode: string } }
}

interface MapHandle {
  destroy: () => void
}

/**
 * 高德地图：按天画线 + 站点编号。
 * - 坐标直接用（后端返回 GCJ-02，前端 JS API 也是 GCJ-02，天然一致，不做转换）
 * - 切 Day 页签要 destroy() 再重建，否则旧线还在
 * - 未配置 VITE_AMAP_JS_KEY 时显示占位（纯色块 + 提示），不发起加载
 */
export default function TripMap({ trip, activeDay }: { trip: Trip; activeDay: number | null }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const handleRef = useRef<MapHandle | null>(null)

  useEffect(() => {
    const key = import.meta.env.VITE_AMAP_JS_KEY
    if (!key) return
    const container = containerRef.current
    if (!container) return

    let cancelled = false
    // 🔴 严格模式第二次进来时第一次可能还没 resolve → cancelled 检查
    AMapLoader.load({
      key,
      version: '2.0',
      plugins: [],
    })
      .then((AMap: unknown) => {
        if (cancelled) return
        const amap = AMap as {
          Map: new (el: HTMLElement, opts: Record<string, unknown>) => MapHandle & {
            setFitView: () => void
            add: (overlays: { setMap: (m: unknown) => void }[]) => void
          }
          Marker: new (opts: Record<string, unknown>) => { setMap: (m: unknown) => void }
          Polyline: new (opts: Record<string, unknown>) => { setMap: (m: unknown) => void }
        }

        const days = activeDay ? trip.days.filter((d) => d.day === activeDay) : trip.days
        const first = days[0]?.stops[0]
        if (!first) return
        const map = new amap.Map(container, {
          zoom: 12,
          center: [first.lng, first.lat],
          mapStyle: 'amap://styles/light',
        })
        handleRef.current = map

        const overlays: { setMap: (m: unknown) => void }[] = []
        const COLOR: Record<number, string> = {
          1: '#FF6B6B',
          2: '#4D96FF',
          3: '#6BCB77',
          4: '#FFD93D',
          5: '#4ECDC4',
        }
        for (const day of days) {
          const color = COLOR[day.day] ?? '#1A1A1A'
          const pts = day.stops.map((s) => [s.lng, s.lat])
          if (pts.length >= 2) {
            overlays.push(
              new amap.Polyline({
                path: pts,
                strokeColor: color,
                strokeWeight: 5,
                strokeOpacity: 0.9,
              }),
            )
          }
          day.stops.forEach((s, i) => {
            overlays.push(
              new amap.Marker({
                position: [s.lng, s.lat],
                offset: new (amap as unknown as {
                  // eslint-disable-next-line @typescript-eslint/no-explicit-any
                  Pixel: new (x: number, y: number) => any
                }).Pixel(-14, -14),
                content: `<div style="width:28px;height:28px;border-radius:2px;background:#1A1A1A;color:#fff;font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:13px;display:flex;align-items:center;justify-content:center;border:1px solid #1A1A1A">${i + 1}</div>`,
                title: s.name,
              }),
            )
          })
        }
        map.add(overlays)
        map.setFitView()
      })
      .catch(() => {
        /* Key 未配置/无效时静默降级为占位图 */
      })

    return () => {
      cancelled = true
      handleRef.current?.destroy()
      handleRef.current = null
    }
  }, [trip, activeDay])

  const key = import.meta.env.VITE_AMAP_JS_KEY

  if (!key) {
    return (
      <div className="border border-ink rounded-[4px] bg-pop-cyan/30 h-full min-h-[320px] flex items-center justify-center">
        <div className="text-center px-6">
          <div className="font-display font-bold text-base mb-1">地图暂未启用</div>
          <p className="text-[12px] text-muted leading-5">
            在 frontend/.env.local 配置 VITE_AMAP_JS_KEY 与安全密钥后显示高德地图
          </p>
        </div>
      </div>
    )
  }

  return <div ref={containerRef} className="border border-ink rounded-[4px] w-full h-full min-h-[320px] bg-white" />
}
