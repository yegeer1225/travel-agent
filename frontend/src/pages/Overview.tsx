import { useCallback, useEffect, useRef, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import type {
  Trip,
  TripOp,
  TripSummaryItem,
  CheckEvent,
  SSEEvent,
  RecheckResponse,
  GuideListItem,
} from '../types/contract'
import { ApiError, getTrip, listGuides, listTrips, patchTrip, publishAsGuide, recheckTrip } from '../lib/api'
import { streamSSE } from '../lib/sse'
import ToolTrajectory, { upsertTrajectory, type TrajectoryEntry } from '../components/ToolTrajectory'
import CheckCard from '../components/CheckCard'
import DayTabs from '../components/DayTabs'
import TripTable from '../components/TripTable'
import TripMap from '../components/TripMap'
import ConfirmDialog from '../components/ConfirmDialog'

export default function Overview() {
  const { tripId } = useParams<{ tripId: string }>()
  const nav = useNavigate()

  // ── 粘贴模式状态 ──
  const [pasteText, setPasteText] = useState('')
  const [destination, setDestination] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [trajectory, setTrajectory] = useState<TrajectoryEntry[]>([])
  const [checkEvt, setCheckEvt] = useState<CheckEvent | null>(null)
  const [recentTrips, setRecentTrips] = useState<TripSummaryItem[] | null>(null)

  // ── 行程状态 ──
  const [trip, setTrip] = useState<Trip | null>(null)
  const [recheck, setRecheck] = useState<RecheckResponse | null>(null)
  const [activeDay, setActiveDay] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [errorBar, setErrorBar] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  // ── 发布为攻略（M12）──
  const [publishing, setPublishing] = useState(false)
  const [publishRequesting, setPublishRequesting] = useState(false) // 仅发布请求进行中（确认框 busy）
  const [publishDialogOpen, setPublishDialogOpen] = useState(false)
  const [publishCheck, setPublishCheck] = useState<GuideListItem[]>([])
  const [publishDone, setPublishDone] = useState<{ guide_id: string; title: string } | null>(null)

  const acRef = useRef<AbortController | null>(null)
  const countersRef = useRef<Record<string, number>>({})

  // ── 加载指定行程（/overview/:tripId）──
  useEffect(() => {
    if (!tripId) return
    let alive = true
    setLoading(true)
    setErrorBar(null)
    setTrip(null)
    setRecheck(null)
    setTrajectory([])
    setCheckEvt(null)
    getTrip(tripId)
      .then((t) => {
        if (!alive) return
        setTrip(t)
        setActiveDay(t.days[0]?.day ?? null)
      })
      .catch((e: unknown) => alive && setErrorBar(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
      acRef.current?.abort()
    }
  }, [tripId])

  // 粘贴模式下列出已有行程
  useEffect(() => {
    if (tripId) return
    listTrips()
      .then((p) => setRecentTrips(p.items))
      .catch(() => setRecentTrips([]))
  }, [tripId, streaming])

  const handleEvent = useCallback((evt: SSEEvent) => {
    switch (evt.type) {
      case 'session':
        // paste 不发 session 帧；即使收到也忽略（粘贴不建会话）
        break
      case 'node':
      case 'tool_call':
      case 'tool_result':
        setTrajectory((prev) => upsertTrajectory(prev, evt, countersRef.current))
        break
      case 'token':
        // paste 流也可能出 token 文本，暂不落到界面（粘贴场景以行程为准）
        break
      case 'check':
        setCheckEvt(evt)
        break
      case 'trip':
        setTrip(evt.trip)
        setActiveDay(evt.trip.days[0]?.day ?? null)
        setRecheck(null)
        break
      case 'done':
        break
      case 'error':
        setErrorBar(evt.msg)
        break
      default:
        break
    }
  }, [])

  // ── 粘贴热启动（POST /trips/paste，SSE，不建会话）──
  const startPaste = useCallback(async () => {
    const text = pasteText.trim()
    if (!text || streaming) return
    setStreaming(true)
    setErrorBar(null)
    setTrajectory([])
    countersRef.current = {}
    setCheckEvt(null)
    setTrip(null)
    setRecheck(null)

    const ac = new AbortController()
    acRef.current = ac
    try {
      await streamSSE(
        '/api/trips/paste',
        { text, destination: destination.trim() === '' ? null : destination.trim() },
        handleEvent,
        ac.signal,
      )
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return
      if (e instanceof ApiError && e.code === 'rate_limited') {
        setErrorBar(`操作太频繁，请 ${e.retryAfter ?? 60} 秒后重试`)
      } else {
        setErrorBar(e instanceof Error ? e.message : String(e))
      }
    } finally {
      if (acRef.current === ac) acRef.current = null
      setStreaming(false)
    }
  }, [pasteText, destination, streaming, handleEvent])

  // ── 提交 op（拖拽 / 删站 / 改时间）→ 整体替换 ──
  const commitOps = useCallback(
    async (ops: TripOp[]) => {
      if (!trip || busy || streaming) return
      setBusy(true)
      setErrorBar(null)
      try {
        const next = await patchTrip(trip.trip_id, ops)
        setTrip(next) // 🔴 响应是完整 Trip，整体替换，不做本地重算
      } catch (e) {
        if (e instanceof ApiError && e.code === 'rate_limited') {
          setErrorBar(`操作太频繁，请 ${e.retryAfter ?? 60} 秒后重试`)
        } else {
          setErrorBar(e instanceof Error ? e.message : String(e))
        }
      } finally {
        setBusy(false)
      }
    },
    [trip, busy, streaming],
  )

  // ── 深度软校验（只有它调 LLM，返回校验卡不是 Trip）──
  const doRecheck = useCallback(async () => {
    if (!trip || busy || streaming) return
    setBusy(true)
    setErrorBar(null)
    try {
      const res = await recheckTrip(trip.trip_id)
      setRecheck(res)
    } catch (e) {
      if (e instanceof ApiError && e.code === 'rate_limited') {
        setErrorBar(`操作太频繁，请 ${e.retryAfter ?? 60} 秒后重试`)
      } else {
        setErrorBar(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setBusy(false)
    }
  }, [trip, busy, streaming])

  // ── 发布为攻略（M12 · api.md 3.8）──
  // 🔴 Idempotency-Key：在 click handler 内生成一次，本次意图的所有重试共用；请求结束作废
  // ⚠️ 必须声明在 handlePublishClick 之前 —— 它内部要调 doPublish，
  //    写到后面会被 lint 判为 "read during its own initialization"（TDZ 隐患）
  const doPublish = useCallback(async (tid: string) => {
    const key = crypto.randomUUID()
    setPublishRequesting(true)
    try {
      const g = await publishAsGuide(tid, key, {})
      setPublishDone({ guide_id: g.guide_id, title: g.title })
    } catch (e) {
      if (e instanceof ApiError && e.code === 'rate_limited') {
        setErrorBar(`操作太频繁，请 ${e.retryAfter ?? 60} 秒后重试`)
      } else {
        setErrorBar(e instanceof Error ? e.message : String(e))
      }
    } finally {
      setPublishRequesting(false)
      setPublishing(false)
      setPublishDialogOpen(false)
    }
  }, [])

  // 🔴 判据每次点击现查（GET /guides?source_trip_id=），不用页面缓存 —— 本地状态一定会过期
  const handlePublishClick = useCallback(async () => {
    if (!trip || publishing) return
    setPublishing(true) // 立即挡双击
    setErrorBar(null)
    setPublishDone(null)
    try {
      const page = await listGuides({ source_trip_id: trip.trip_id })
      if (page.total === 0) {
        await doPublish(trip.trip_id)
      } else {
        setPublishCheck(page.items)
        setPublishDialogOpen(true)
      }
    } catch (e) {
      setErrorBar(e instanceof Error ? e.message : String(e))
      setPublishing(false)
    }
  }, [trip, publishing, doPublish])

  const onPublishConfirm = useCallback(() => {
    if (!trip) return
    void doPublish(trip.trip_id)
  }, [trip, doPublish])

  const onPublishCancel = useCallback(() => {
    setPublishDialogOpen(false)
    setPublishing(false) // 取消 = 解锁按钮，结束
  }, [])

  const relTime = (iso: string | null): string => {
    if (!iso) return ''
    const ms = Date.now() - new Date(iso).getTime()
    const days = Math.floor(ms / 86400000)
    if (days <= 0) return '今天'
    if (days === 1) return '昨天'
    if (days < 30) return `${days} 天前`
    const months = Math.floor(days / 30)
    if (months < 12) return `${months} 个月前`
    return `${Math.floor(months / 12)} 年前`
  }

  // ── 视图一：空手进来 + 还没有行程 → 粘贴表单 ──
  if (!tripId && !trip) {
    return (
      <div className="relative px-14 py-10">
        <div className="deco deco-ring" style={{ width: 26, height: 26, bottom: 26, left: 72 }} />

        <h1 className="font-display font-bold text-[34px]">路线总览</h1>
        <p className="text-[14px] text-muted mt-1">粘贴一段现成攻略，秒出结构化行程总览（不建会话，行程已定稿）</p>

        <div className="card mt-8 p-6 max-w-[720px]">
          <textarea
            className="w-full border border-ink rounded-[4px] px-4 py-3 text-[14px] outline-none focus:border-2 resize-y leading-6 min-h-[160px]"
            placeholder={'示例：\nDay1 武侯祠→锦里→宽窄巷子\nDay2 大熊猫基地→太古里→春熙路\nDay3 都江堰→青城山'}
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
          />
          <div className="flex items-center gap-3 mt-4">
            <input
              className="w-44 border border-ink rounded-[4px] px-3 py-2.5 text-[14px] outline-none"
              placeholder="目的地（可选）"
              value={destination}
              onChange={(e) => setDestination(e.target.value)}
            />
            <button className="btn-black" onClick={startPaste} disabled={streaming || pasteText.trim() === ''}>
              {streaming ? '解析中…' : '生成路线总览'}
            </button>
          </div>
          <p className="text-[12px] text-muted mt-3">
            粘贴不建会话 · 生成结果只能确定性修改（拖拽 / 删站 / 改时间），不能回助手页接着聊
          </p>
        </div>

        {/* 粘贴流过程：轨迹 + 校验 */}
        {trajectory.length > 0 && (
          <div className="mt-6 max-w-[720px] flex flex-col gap-4">
            <ToolTrajectory entries={trajectory} />
          </div>
        )}
        {checkEvt && !trip && (
          <div className="mt-4 max-w-[720px]">
            <CheckCard checks={checkEvt.checks} round={checkEvt.round} />
          </div>
        )}

        {errorBar && <div className="mt-4 text-status-fail text-[13px] max-w-[720px]">{errorBar}</div>}

        {/* 已有行程快捷入口 */}
        {recentTrips && recentTrips.length > 0 && (
          <div className="mt-12">
            <h2 className="font-display font-bold text-lg mb-4">我的行程</h2>
            <div className="flex flex-col gap-2 max-w-[720px]">
              {recentTrips.map((t) => (
                <button
                  key={t.trip_id}
                  onClick={() => nav(`/overview/${t.trip_id}`)}
                  className="card p-4 text-left hover:bg-pop-yellow/30 transition-colors cursor-pointer"
                >
                  <div className="flex items-center gap-3">
                    <span className="font-bold truncate flex-1">{t.title}</span>
                    <span className="text-[12px] text-muted shrink-0">{t.destination}</span>
                    <span className="text-[12px] text-muted shrink-0">
                      {t.summary.stop_count} 站 · {t.summary.total_distance_km} km
                    </span>
                    <span className="text-[12px] text-status-pass shrink-0">硬错 {t.summary.hard_errors}</span>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    )
  }

  // ── 视图二：行程详情（粘贴完成 / /overview/:tripId）──
  if (!trip) {
    return (
      <div className="px-14 py-10">
        {loading && <div className="text-muted">加载中…</div>}
        {!loading && errorBar && <div className="text-status-fail text-sm">加载失败：{errorBar}</div>}
        {!loading && !errorBar && <div className="text-muted">行程不存在</div>}
      </div>
    )
  }

  const s = trip.summary

  return (
    <div className="relative px-14 py-8">
      {/* ── 头部 ── */}
      <div className="flex items-start gap-4">
        <div>
          <h1 className="font-display font-bold text-[26px]">{trip.title}</h1>
          <div className="flex items-center gap-3 mt-1 text-[13px] text-muted">
            <span>{trip.destination}</span>
            <span className="px-2 py-0.5 border border-ink rounded-[2px]">
              {trip.source === 'generated' ? 'AI 生成' : '粘贴导入'}
            </span>
            {trip.source === 'generated' && trip.session_id && (
              <button
                className="text-ink font-bold underline-offset-2 hover:underline"
                onClick={() => nav('/assistant')}
              >
                回到助手页继续聊 →
              </button>
            )}
            {trip.source === 'pasted' && <span>已定稿，仅可确定性修改</span>}
          </div>
        </div>

        <div className="ml-auto flex items-center gap-6 text-right">
          <div>
            <div className="font-display font-bold text-xl">{trip.days.length} 天</div>
            <div className="text-[11px] text-muted">行程天数</div>
          </div>
          <div>
            <div className="font-display font-bold text-xl">{s.stop_count} 站</div>
            <div className="text-[11px] text-muted">站点数</div>
          </div>
          <div>
            <div className="font-display font-bold text-xl">{s.total_distance_km.toFixed(1)} km</div>
            <div className="text-[11px] text-muted">总里程</div>
          </div>
          <div>
            <div className="font-display font-bold text-xl text-status-pass">{s.hard_errors}</div>
            <div className="text-[11px] text-muted">硬错误</div>
          </div>
          <div>
            <div className="font-display font-bold text-xl text-status-unknown">{s.soft_warnings}</div>
            <div className="text-[11px] text-muted">软警告</div>
          </div>
        </div>
      </div>

      {/* ── 操作行 ── */}
      <div className="flex items-center gap-3 mt-5">
        <DayTabs days={trip.days} active={activeDay} onChange={setActiveDay} />
        <div className="ml-auto flex items-center gap-3">
          <button className="btn-outline !text-[12px]" onClick={doRecheck} disabled={busy || streaming}>
            {busy ? '校验中…' : '重新校验（软）'}
          </button>
          <button
            className="btn-outline !text-[12px]"
            disabled={publishing || busy}
            onClick={() => void handlePublishClick()}
          >
            {publishing ? '发布中…' : '发布为攻略'}
          </button>
          <button
            className="btn-outline !text-[12px]"
            disabled={busy}
            onClick={async () => {
              try {
                const res = await fetch(`/api/trips/${trip.trip_id}/amap-import`, {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${localStorage.getItem('token') ?? ''}`,
                  },
                  body: '{}',
                })
                const body = await res.json().catch(() => null)
                if (res.ok && body?.url) window.open(body.url, '_blank')
                else setErrorBar(body?.error?.msg ?? '导出失败')
              } catch (e) {
                setErrorBar(e instanceof Error ? e.message : String(e))
              }
            }}
          >
            导出到高德
          </button>
        </div>
      </div>

      {/* 发布成功提示 */}
      {publishDone && (
        <div className="mt-4 px-4 py-2.5 border border-status-pass rounded-[4px] text-status-pass text-[13px] bg-white flex items-center gap-2">
          已发布为攻略《{publishDone.title}》。
          <Link to={`/guides/${publishDone.guide_id}`} className="font-bold underline-offset-4 hover:underline">
            去看看 →
          </Link>
        </div>
      )}

      {errorBar && (
        <div className="mt-4 px-4 py-2.5 border border-status-fail rounded-[4px] text-status-fail text-[13px] bg-white">
          {errorBar}
        </div>
      )}

      {/* ── 中部：地图 + 可拖拽表格 ── */}
      <div className="grid gap-5 mt-6" style={{ gridTemplateColumns: '420px 1fr' }}>
        <div className="min-w-0">
          <TripMap trip={trip} activeDay={activeDay} />
        </div>
        <div className="min-w-0">
          <div className="mb-2 flex items-center gap-2 text-[12px] text-muted">
            <span>拖拽排序（支持跨天 · 插入语义）</span>
            {busy && <span className="text-status-unknown font-bold">重算中…</span>}
          </div>
          <TripTable trip={trip} busy={busy || streaming} onCommit={commitOps} />
        </div>
      </div>

      {/* ── 校验区 ── */}
      <div className="grid gap-5 mt-6" style={{ gridTemplateColumns: 'repeat(2, minmax(0,1fr))' }}>
        <div>
          <div className="font-display font-bold text-sm tracking-wide uppercase mb-2">行程校验</div>
          <CheckCard checks={trip.checks} />
        </div>
        {recheck && (
          <div>
            <div className="font-display font-bold text-sm tracking-wide uppercase mb-2">
              深度校验（round {recheck.validation.rounds}）
            </div>
            <CheckCard checks={recheck.checks} />
            {recheck.validation.fixed.length > 0 && (
              <div className="card mt-3 p-4">
                <div className="font-bold text-[13px] text-status-pass mb-2">已修正</div>
                {recheck.validation.fixed.map((f, i) => (
                  <div key={i} className="text-[12px] text-muted leading-5 border-t border-ink/15 py-1.5 first:border-t-0">
                    {f.msg}
                  </div>
                ))}
              </div>
            )}
            {recheck.validation.remaining.length > 0 && (
              <div className="card mt-3 p-4">
                <div className="font-bold text-[13px] text-status-unknown mb-2">仍需要注意</div>
                {recheck.validation.remaining.map((f, i) => (
                  <div key={i} className="text-[12px] text-muted leading-5 border-t border-ink/15 py-1.5 first:border-t-0">
                    {f.msg}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* 粘贴流中的过程状态（已生成行程后通常不再显示） */}
      {trajectory.length > 0 && (
        <div className="mt-6">
          <ToolTrajectory entries={trajectory} />
        </div>
      )}

      {/* 发布为攻略：已发过 → 确认框（信息量：条数 + 最近一篇标题） */}
      <ConfirmDialog
        open={publishDialogOpen}
        title="再发一篇攻略？"
        busy={publishRequesting}
        onConfirm={onPublishConfirm}
        onCancel={onPublishCancel}
      >
        这条行程你已经发布过{' '}
        <b className="text-ink font-bold">{publishCheck.length}</b> 篇攻略，最近一篇是
        《{publishCheck[0]?.title ?? '—'}》
        {publishCheck[0]?.published_at ? `（${relTime(publishCheck[0].published_at)}）` : ''}。要再发一篇吗？
      </ConfirmDialog>
    </div>
  )
}
