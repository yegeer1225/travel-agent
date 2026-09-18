import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type {
  Session,
  ChatMessage,
  CheckEvent,
  SSEEvent,
  MessageMeta,
  SkeletonEvent,
} from '../types/contract'
import { ApiError, createSession, deleteSession, getSession, listSessions } from '../lib/api'
import { streamSSE, SseInterruptedError } from '../lib/sse'
import ToolTrajectory, { upsertTrajectory, type TrajectoryEntry } from '../components/ToolTrajectory'
import CheckCard from '../components/CheckCard'
import TripCard from '../components/TripCard'
import SkeletonCard from '../components/SkeletonCard'
import type { Trip } from '../types/contract'

interface UiMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  meta: MessageMeta | null
  createdAt: string
}

let uid = 0
const nextId = () => `msg-${++uid}`
const now = () => new Date().toISOString()

function toUi(m: ChatMessage): UiMessage {
  return { id: m.id, role: m.role, content: m.content, meta: m.meta, createdAt: m.created_at }
}

const SUGGESTIONS = ['成都，3天，带爸妈', '广州两日游，轻松一点', '北京 5 天，看故宫和长城']

export default function Assistant() {
  const nav = useNavigate()
  const [sessions, setSessions] = useState<Session[] | null>(null)
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<UiMessage[]>([])
  const [trajectory, setTrajectory] = useState<TrajectoryEntry[]>([])
  const [checkEvt, setCheckEvt] = useState<CheckEvent | null>(null)
  const [skeleton, setSkeleton] = useState<SkeletonEvent | null>(null)
  const [trip, setTrip] = useState<Trip | null>(null)
  const [streaming, setStreaming] = useState(false)
  const [errorBar, setErrorBar] = useState<string | null>(null)
  const [countdown, setCountdown] = useState<number | null>(null)
  const [input, setInput] = useState('')
  /** 新建会话的模型选择（A45）：下拉框兜住现有选项；合法性校验交给后端（400），前端不写死判断逻辑 */
  const [selectedModel, setSelectedModel] = useState<string>('')

  const acRef = useRef<AbortController | null>(null)
  const countersRef = useRef<Record<string, number>>({})
  const listRef = useRef<HTMLDivElement>(null)

  const refreshSessions = useCallback(() => {
    listSessions()
      .then((p) => setSessions(p.items))
      .catch(() => setSessions([]))
  }, [])

  useEffect(() => {
    refreshSessions()
    return () => acRef.current?.abort() // 🔴 严格模式：卸载时取消进行中的流
  }, [refreshSessions])

  // 429 倒计时
  useEffect(() => {
    if (countdown === null) return
    if (countdown <= 0) {
      setCountdown(null)
      return
    }
    const t = setTimeout(() => setCountdown((c) => (c === null ? null : c - 1)), 1000)
    return () => clearTimeout(t)
  }, [countdown])

  // 自动滚动到底部
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
  }, [messages, trajectory, checkEvt, trip])

  const handleEvent = useCallback((evt: SSEEvent) => {
    switch (evt.type) {
      case 'session':
        setCurrentSessionId(evt.session_id)
        break
      case 'node':
      case 'tool_call':
      case 'tool_result':
        setTrajectory((prev) => upsertTrajectory(prev, evt, countersRef.current))
        break
      case 'token':
        setMessages((prev) => {
          const next = prev.slice()
          const last = next[next.length - 1]
          if (last && last.role === 'assistant') {
            next[next.length - 1] = { ...last, content: last.content + evt.text }
          } else {
            next.push({ id: nextId(), role: 'assistant', content: evt.text, meta: null, createdAt: now() })
          }
          return next
        })
        break
      case 'check':
        setCheckEvt(evt)
        break
      case 'skeleton':
        // 18 节：骨架先行——只读草排卡，stops 是未验证地名文本，不可进详情
        setSkeleton(evt)
        break
      case 'trip':
        setTrip(evt.trip)
        setSkeleton(null) // 🔴 trip 到达 → 整体替换骨架卡，不合并不追加
        break
      case 'done':
        break // done 只代表成功结束，无需额外状态
      case 'error':
        setErrorBar(evt.msg)
        setSkeleton(null) // 🔴 只有 error 没有 trip → 骨架卡换成错误提示，不残留
        break
      default:
        break // 未知 type 必须忽略
    }
  }, [])

  const openSession = useCallback(async (sid: string) => {
    acRef.current?.abort()
    setStreaming(false)
    setErrorBar(null)
    setTrajectory([])
    setCheckEvt(null)
    setSkeleton(null)
    setTrip(null)
    try {
      const detail = await getSession(sid)
      setCurrentSessionId(sid)
      setMessages(detail.messages.map(toUi))
    } catch (e) {
      setErrorBar(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const startNew = useCallback(() => {
    acRef.current?.abort()
    setStreaming(false)
    setCurrentSessionId(null)
    setMessages([])
    setTrajectory([])
    setCheckEvt(null)
    setSkeleton(null)
    setTrip(null)
    setErrorBar(null)
  }, [])

  /** 删除会话（DELETE /sessions/{id}，不删行程）。删除当前会话时回到新会话态 */
  const onDeleteSession = useCallback(
    async (sid: string) => {
      try {
        await deleteSession(sid)
        setSessions((prev) => (prev ? prev.filter((s) => s.session_id !== sid) : prev))
        if (currentSessionId === sid) {
          startNew()
        }
      } catch (e) {
        setErrorBar(e instanceof Error ? e.message : String(e))
      }
    },
    [currentSessionId, startNew],
  )

  const send = useCallback(
    async (raw?: string) => {
      const text = (raw ?? input).trim()
      if (!text || streaming || countdown !== null) return
      let sid = currentSessionId
      if (!sid) {
        try {
          const s = await createSession({
            title: text.slice(0, 16),
            model: selectedModel === '' ? null : selectedModel,
          })
          sid = s.session_id
          setCurrentSessionId(sid)
          refreshSessions()
        } catch (e) {
          if (e instanceof ApiError && e.code === 'invalid_param') {
            setErrorBar(`模型参数不合法：${e.message}`)
          } else if (e instanceof ApiError && e.code === 'unavailable') {
            // 后端临时不可用（停服/502）：不暴露技术红字，给明确提示（17.5 验收 6）
            setErrorBar('连接中断，请重新发送')
          } else {
            setErrorBar(e instanceof Error ? e.message : String(e))
          }
          return
        }
      }

      setMessages((m) => [
        ...m,
        { id: nextId(), role: 'user', content: text, meta: null, createdAt: now() },
        { id: nextId(), role: 'assistant', content: '', meta: null, createdAt: now() },
      ])
      setInput('')
      setErrorBar(null)
      setTrajectory([])
      countersRef.current = {}
      setCheckEvt(null)
      setSkeleton(null)
      setTrip(null)
      setStreaming(true)

      const ac = new AbortController()
      acRef.current = ac

      // ── 15.13 SSE 断线重连：断流（EOF 无 done）退避重连 1s→2s→4s，最多 3 次 ──
      // 重连复用同一 POST /chat，带 Last-Event-ID 触发后端恢复（从 checkpoint 续推，不重复落用户消息）
      const RETRY_DELAYS = [1000, 2000, 4000]
      const MAX_RETRY = 3
      let lastEventId: string | null = null
      let attempts = 0
      const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

      try {
        while (true) {
          try {
            const result = await streamSSE(
              `/api/sessions/${sid}/chat`,
              { message: text, steer: false }, // 🔴 重连也必须带非空 message（min_length=1，否则 422）；后端 resume 时不落库
              handleEvent,
              ac.signal,
              { lastEventId },
            )
            lastEventId = result.lastEventId
            break // 收到 done，本轮成功
          } catch (e) {
            if (e instanceof DOMException && e.name === 'AbortError') return
            if (e instanceof ApiError && e.code === 'rate_limited') {
              // 🔴 重连撞 429：服务端要等自己发现连接断了才释放并发位（约 5s）→ 按 retry_after 等，不立刻重打
              const sec = e.retryAfter ?? 60
              if (attempts < MAX_RETRY) {
                attempts += 1
                await sleep(sec * 1000)
                continue
              }
              setCountdown(sec)
              setErrorBar(`操作太频繁，请 ${sec} 秒后重试`)
              return
            }
            if (e instanceof SseInterruptedError) {
              lastEventId = e.lastEventId ?? lastEventId
              if (attempts < MAX_RETRY) {
                await sleep(RETRY_DELAYS[attempts])
                attempts += 1
                continue
              }
              setErrorBar('连接中断，请重新发送')
              return
            }
            setErrorBar(e instanceof Error ? e.message : String(e))
            return
          }
        }
      } finally {
        if (acRef.current === ac) acRef.current = null
        setStreaming(false)
      }
    },
    [input, streaming, countdown, currentSessionId, selectedModel, handleEvent, refreshSessions],
  )

  const busyLabel = streaming
    ? skeleton
      ? '精排中…' // 🔴 骨架已在 → loading 别再转空圈（18.3）
      : 'AI 处理中…'
    : countdown !== null
      ? `${countdown}s 后可发送`
      : null

  return (
    <div className="flex flex-1 min-h-0 relative">
      {/* 左侧：会话历史 */}
      <aside className="w-[280px] shrink-0 border-r border-ink bg-white flex flex-col">
        <div className="p-4 border-b border-ink flex flex-col gap-3">
          {/* 模型选择（A45）：下拉框兜住现有选项，合法性交给后端 400 */}
          <label className="flex flex-col gap-1 text-[12px] text-muted">
            <span className="font-bold">新会话模型</span>
            <select
              className="border border-ink rounded-[4px] px-2 py-1.5 text-[13px] text-ink outline-none bg-white cursor-pointer"
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
            >
              <option value="">默认模型</option>
              <option value="deepseek-flash">deepseek-flash</option>
              <option value="deepseek-v4-pro">deepseek-v4-pro</option>
              <option value="qwen3.7-plus">qwen3.7-plus</option>
              <option value="qwen3.7-flash">qwen3.7-flash</option>
            </select>
          </label>
          <button className="btn-black w-full !py-2.5" onClick={startNew} disabled={streaming}>
            + 新建会话
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-3 flex flex-col gap-1.5">
          {sessions === null && <div className="text-muted text-[12px] p-2">加载中…</div>}
          {sessions?.map((s) => (
            <div
              key={s.session_id}
              role="button"
              tabIndex={0}
              aria-label={s.title || '未命名会话'}
              className={[
                'group flex items-center gap-1 px-3 py-2.5 rounded-[4px] border text-[13px] cursor-pointer transition-colors',
                currentSessionId === s.session_id
                  ? 'bg-pop-yellow border-ink font-bold'
                  : 'border-ink/0 hover:bg-pop-yellow/40',
              ].join(' ')}
              onClick={() => openSession(s.session_id)}
              onKeyDown={(e) => {
                // 键盘可达（P3）：Enter / Space 等效点击
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  openSession(s.session_id)
                }
              }}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate">{s.title || '未命名会话'}</span>
                  {s.model && (
                    <span className="text-[10px] px-1.5 py-0.5 border border-ink/40 rounded-[2px] text-muted shrink-0">
                      {s.model}
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-muted mt-0.5">
                  {s.updated_at.slice(5, 16).replace('T', ' ')}
                </div>
              </div>
              <button
                title="删除会话（不删行程）"
                onClick={(e) => {
                  e.stopPropagation()
                  void onDeleteSession(s.session_id)
                }}
                className="opacity-0 group-hover:opacity-100 text-[16px] leading-none px-1 text-muted hover:text-status-fail shrink-0"
              >
                ×
              </button>
            </div>
          ))}
          {sessions?.length === 0 && (
            <div className="text-muted text-[12px] p-2 leading-5">
              还没有会话，点「新建会话」或直接输入需求开始
            </div>
          )}
        </div>
      </aside>

      {/* 右侧：对话区 */}
      <main className="flex-1 flex flex-col min-w-0">
        {/* 消息列表 */}
        <div ref={listRef} className="flex-1 overflow-y-auto px-10 py-8 flex flex-col gap-5">
          {messages.length === 0 && (
            <div className="my-auto text-center">
              <div className="font-display font-bold text-2xl mb-3">AI 路线规划助手</div>
              <p className="text-muted text-[14px] mb-6">说一句需求，自动排好每天去哪、几点到、路上多久、天气合不合适</p>
              <div className="flex flex-col items-center gap-2">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="btn-outline !text-[13px]" onClick={() => send(s)} disabled={streaming || countdown !== null}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <div key={m.id} className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
              <div
                className={[
                  'max-w-[76%] px-4 py-3 rounded-[4px] border border-ink text-[14px] leading-6 whitespace-pre-wrap break-words',
                  m.role === 'user' ? 'bg-pop-yellow' : 'bg-white',
                  streaming && i === messages.length - 1 && m.role === 'assistant' ? 'type-cursor' : '',
                ].join(' ')}
              >
                {m.content === '' && streaming ? '…' : m.content}
                {m.meta && m.meta.tool_calls.length > 0 && (
                  <div className="mt-2 pt-2 border-t border-ink/20 text-[12px] text-muted">
                    调用了 {m.meta.tool_calls.length} 个工具
                    {m.meta.trip_id && (
                      <button
                        className="ml-2 text-ink font-bold underline-offset-2 hover:underline"
                        onClick={() => nav(`/overview/${m.meta!.trip_id}`)}
                      >
                        查看行程 →
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}

          {/* 当前 run 的实时状态：骨架卡（最先出现，trip 到达被整体替换）/ 轨迹卡 / 校验卡 / 行程卡 */}
          {skeleton && <SkeletonCard evt={skeleton} />}
          {trajectory.length > 0 && <ToolTrajectory entries={trajectory} />}
          {checkEvt && <CheckCard checks={checkEvt.checks} round={checkEvt.round} />}
          {trip && (
            <TripCard trip={trip} onOpenOverview={(tid) => nav(`/overview/${tid}`)} />
          )}
        </div>

        {/* 错误提示条 */}
        {errorBar && (
          <div className="mx-10 mb-3 px-4 py-2.5 border border-status-fail rounded-[4px] text-status-fail text-[13px] bg-white">
            {errorBar}
          </div>
        )}

        {/* 输入区 */}
        <div className="px-10 pb-8 pt-2 border-t border-ink/10">
          <div className="flex items-end gap-3">
            <textarea
              className="flex-1 border border-ink rounded-[4px] px-4 py-3 text-[14px] outline-none focus:border-2 resize-none leading-6"
              rows={2}
              placeholder="例如：成都，3天，带爸妈，节奏慢一点"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  send()
                }
              }}
            />
            <button className="btn-black shrink-0" onClick={() => send()} disabled={streaming || countdown !== null || input.trim() === ''}>
              {busyLabel ?? '发送'}
            </button>
          </div>
          <div className="text-[11px] text-muted mt-2">
            对话次数有限流（10 次/小时）· 发送期间按钮置灰，请勿重复点击
          </div>
        </div>
      </main>
    </div>
  )
}
