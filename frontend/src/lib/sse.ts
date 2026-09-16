/**
 * SSE 客户端：fetch + ReadableStream 手写解析
 *
 * ⚠️ EventSource 只支持 GET，而聊天/粘贴都是 POST → 必须手写。
 * 参考 docs/api.md 3.7 ④。
 *
 * 与参考实现的差异（交接文档 7.1）：
 * - 每帧可能带 `id:` 行，这里解析并保存，调用方存进 state，
 *   供 M6 断线恢复（Last-Event-ID）使用。
 * - 心跳 `: ping` 是注释行，跳过以 `:` 开头的行。
 * - 未知 type 必须忽略而不是抛错（isKnownEvent）。
 */

import type { SSEEvent } from '../types/contract'
import { isKnownEvent } from '../types/contract'
import { ApiError } from './api'
import { handleUnauthorized } from './auth'

export type SSEHandler = (evt: SSEEvent) => void

export interface StreamResult {
  /** 最后收到的 SSE 帧 id（可能为 null：全程没有 id 行） */
  lastEventId: string | null
}

export async function streamSSE(
  url: string,
  body: unknown,
  onEvent: SSEHandler,
  signal: AbortSignal,
  opts: { lastEventId?: string | null } = {},
): Promise<StreamResult> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
    Authorization: `Bearer ${localStorage.getItem('token') ?? ''}`, // ← M9 之前为空串
  }
  if (opts.lastEventId) headers['Last-Event-ID'] = opts.lastEventId

  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal, // ← ★ 严格模式下靠它取消，否则 effect 跑两次＝扣两次钱
  })
  if (!res.ok) {
    // 统一错误形状（api.md 1.2）：429 rate_limited 读 detail.retry_after；401 → 清 token 跳登录
    const body = await res.json().catch(() => null)
    const err = new ApiError(
      body?.error?.code ?? 'unknown',
      body?.error?.msg ?? `HTTP ${res.status}`,
      body?.error?.detail ?? null,
    )
    if (err.code === 'unauthorized') handleUnauthorized()
    throw err
  }
  if (!res.body) throw new Error('响应没有 body，无法流式读取')

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let gotDone = false
  let lastEventId: string | null = null

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })

    // 按"空行"切帧；最后一段可能不完整，留在 buf 里等下一块
    const frames = buf.split('\n\n')
    buf = frames.pop() ?? ''

    for (const frame of frames) {
      let dataLine: string | undefined
      for (const line of frame.split('\n')) {
        if (line.startsWith('id:')) lastEventId = line.slice(3).trim()
        else if (line.startsWith('data:')) dataLine = line
        // 跳过 `: ping` 心跳（以 `:` 开头的注释行）
      }
      if (!dataLine) continue
      const evt: unknown = JSON.parse(dataLine.slice(5).trim())
      const typed = evt as { type: string }
      if (!isKnownEvent(typed)) continue // 未知 type 必须忽略
      if (typed.type === 'done') gotDone = true
      onEvent(typed)
    }
  }
  if (!gotDone) throw new Error('连接中断，未收到 done')
  return { lastEventId }
}
