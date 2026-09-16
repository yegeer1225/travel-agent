import { Swiper, SwiperSlide } from 'swiper/react'
import { Pagination, Autoplay } from 'swiper/modules'
import 'swiper/css'
import 'swiper/css/pagination'
import type { HeroSlide } from '../types/contract'

/**
 * 首页 Hero 轮播（Swiper，全站唯一，只在首页出现一次）。
 * hero < 3 张不做轮播，直接单图（api.md 3.6）。
 * 图片加载失败 → 纯色块 + 站名（不显示灰底占位图）。
 */
export default function HeroCarousel({ slides }: { slides: HeroSlide[] }) {
  if (slides.length === 0) return null

  const renderSlide = (s: HeroSlide) => (
    <div className="relative w-full h-[340px] overflow-hidden border border-ink rounded-[4px] bg-pop-cyan">
      {s.photo ? (
        <img
          src={s.photo}
          alt={s.name}
          className="w-full h-full object-cover"
          onError={(e) => {
            const img = e.currentTarget
            img.style.display = 'none'
          }}
        />
      ) : null}
      {/* 图片挂掉时底色色块兜底，不出现破图 */}
      <div className="absolute inset-0 flex items-center justify-center -z-0">
        <span className="font-display font-bold text-4xl tracking-wider text-ink uppercase">
          {s.name}
        </span>
      </div>
      <div className="absolute bottom-3 left-3 bg-ink text-white text-[12px] px-2.5 py-1 rounded-[2px] z-10">
        {s.name} · {s.city}
      </div>
    </div>
  )

  if (slides.length < 3) {
    return <div className="w-full">{renderSlide(slides[0])}</div>
  }

  return (
    <Swiper
      modules={[Pagination, Autoplay]}
      pagination={{ clickable: true }}
      autoplay={{ delay: 4000, disableOnInteraction: false }}
      loop
      spaceBetween={16}
      className="w-full"
    >
      {slides.map((s, i) => (
        <SwiperSlide key={`${s.poi_id}-${i}`}>{renderSlide(s)}</SwiperSlide>
      ))}
    </Swiper>
  )
}
