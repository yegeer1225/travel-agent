/**
 * 请求层：统一 /api 前缀 + Authorization 头 + 错误按 code 分支。
 * 参考 docs/api.md 3.7 ④（request 实现）+ 1.2（错误形状）+ 1.5（限流）。
 *
 * 约定：
 * - 前端只按 code 分支，不匹配 msg（msg 是给人看的，会改）
 * - 404 = 不存在 或 无权，统一显示"不存在"，不区分
 * - 401 unauthorized → GET：清 token 开弹窗（可重放）；写操作：跳登录页带 redirect（15.10/17.2）
 * - 429 rate_limited：读 detail.retry_after（秒），按钮置灰 + 倒计时
 */

import type {
  HealthResponse,
  Page,
  Session,
  SessionCreateRequest,
  SessionDetail,
  Trip,
  TripSummaryItem,
  RecheckResponse,
  HomeResponse,
  SpotSearchResponse,
  SpotListResponse,
  SpotCard,
  TripOp,
  RegisterRequest,
  LoginRequest,
  AuthResponse,
  UserOut,
  UpdateProfileRequest,
  AvatarUploadResponse,
  AmapImportResponse,
  GuideListItem,
  GuideDetail,
  GuideUpsertRequest,
  CommentItem,
  CommentCreateRequest,
  LikeToggleRequest,
  LikeState,
  FavoriteCreateRequest,
  FavoriteItem,
  TargetType,
} from '../types/contract'
import { handleUnauthorized, redirectToLogin, waitForLogin, getToken } from './auth'

export class ApiError extends Error {
  readonly code: string
  readonly detail: Record<string, unknown> | null

  constructor(code: string, msg: string, detail: Record<string, unknown> | null = null) {
    super(msg)
    this.name = 'ApiError'
    this.code = code
    this.detail = detail
  }

  /** 限流剩余秒数（429 rate_limited 时有效），主路径是 body 的 detail.retry_after */
  get retryAfter(): number | null {
    if (typeof this.detail?.retry_after === 'number') return this.detail.retry_after
    return null
  }
}

/** 原始请求：401 → GET 弹窗（可重放）；写操作跳登录页（15.10/17.2，判 GET 用 init.method 别用 path 猜） */
async function _raw<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${localStorage.getItem('token') ?? ''}`,
      ...(init.headers ?? {}),
    },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    // 🔴 5xx = 后端临时不可用（Vite 代理 502/500）：统一 code，页面按「服务不可用」提示，不暴露技术红字（17.5 验收 6）
    const err = new ApiError(
      res.status >= 500 ? 'unavailable' : (body?.error?.code ?? 'unknown'),
      res.status >= 500 ? `服务暂时不可用（HTTP ${res.status}）` : (body?.error?.msg ?? `HTTP ${res.status}`),
      body?.error?.detail ?? null,
    )
    if (err.code === 'unauthorized') {
      const isGet = !init.method || init.method.toUpperCase() === 'GET'
      if (isGet) handleUnauthorized() // GET：清 token 开弹窗，登录后由 request() 重放
      else redirectToLogin() // 写操作：整页跳登录，带 redirect（不弹窗）
    }
    throw err
  }
  return res.status === 204 ? (undefined as T) : res.json()
}

/**
 * 带 401 重放的请求（api.md 1.1.3 / 交接文档 15.9）：
 * - 401 + GET → 透明挂起等登录弹窗，登录成功后重放**一次**（调用方无感知）
 * - 401 + 非 GET → 直接抛（写操作不重放，防重复提交）
 * - 重放再 401 → 抛错（不循环）
 */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  try {
    return await _raw<T>(path, init)
  } catch (e) {
    const is401 = e instanceof ApiError && e.code === 'unauthorized'
    const isGet = !init.method || init.method.toUpperCase() === 'GET'
    if (!is401 || !isGet) throw e // 🔴 只重放 GET（api.md 1.1.3 第 4 条）
    await waitForLogin() // 挂起等弹窗登录；取消 → reject
    return await _raw<T>(path, init) // 重放一次，再失败就抛（不循环）
  }
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body) })
}

// ── 系统 ────────────────────────────────────────────────
export function fetchHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health')
}

// ── 会话与对话 ──────────────────────────────────────────
export function listSessions(): Promise<Page<Session>> {
  return request<Page<Session>>('/sessions')
}

/** 新建会话：model 可选（deepseek-flash 等），省略/null = 后端默认；传了不合法 → 后端 400，前端不拦截 */
export function createSession(req: SessionCreateRequest = { title: null, model: null }): Promise<Session> {
  return post<Session>('/sessions', req)
}

export function getSession(id: string): Promise<SessionDetail> {
  return request<SessionDetail>(`/sessions/${id}`)
}

export function deleteSession(id: string): Promise<void> {
  return request<void>(`/sessions/${id}`, { method: 'DELETE' })
}

// ── 行程（核心）─────────────────────────────────────────
export function listTrips(): Promise<Page<TripSummaryItem>> {
  return request<Page<TripSummaryItem>>('/trips')
}

export function getTrip(id: string): Promise<Trip> {
  return request<Trip>(`/trips/${id}`)
}

/** 拖拽/删站/改时间：一次提交一批 op，响应是重算后的完整 Trip，前端整体替换 */
export function patchTrip(id: string, ops: TripOp[]): Promise<Trip> {
  return request<Trip>(`/trips/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ ops } satisfies { ops: TripOp[] }),
  })
}

/** 深度软校验：只有它调 LLM，返回校验卡不是 Trip */
export function recheckTrip(id: string): Promise<RecheckResponse> {
  return post<RecheckResponse>(`/trips/${id}/recheck`, {})
}

// ── 首页 ────────────────────────────────────────────────
export function fetchHome(): Promise<HomeResponse> {
  return request<HomeResponse>('/home')
}

// ── 用户 / 鉴权（M9，api.md 2.7）─────────────────────────
export function register(body: RegisterRequest): Promise<AuthResponse> {
  return post<AuthResponse>('/auth/register', body)
}

export function login(body: LoginRequest): Promise<AuthResponse> {
  return post<AuthResponse>('/auth/login', body)
}

/** 昵称/头像只从这里拿，不许解 token */
export function getMe(): Promise<UserOut> {
  return request<UserOut>('/auth/me')
}

export function patchMe(body: UpdateProfileRequest): Promise<UserOut> {
  return request<UserOut>('/auth/me', { method: 'PATCH', body: JSON.stringify(body) })
}

/** multipart/form-data，字段名 file；不能走公共 request（会强制 application/json） */
export async function uploadAvatar(file: File): Promise<AvatarUploadResponse> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/uploads/avatar', {
    method: 'POST',
    headers: { Authorization: `Bearer ${getToken()}` },
    body: form,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => null)
    const err = new ApiError(
      body?.error?.code ?? 'unknown',
      body?.error?.msg ?? `HTTP ${res.status}`,
      body?.error?.detail ?? null,
    )
    if (err.code === 'unauthorized') redirectToLogin() // POST：直接跳登录页（不能重放，15.10）
    throw err
  }
  return res.json()
}

// ── 景点 ────────────────────────────────────────────────
/** 收录库全量分页（2026-09-18）：景点页默认态平铺用。响应无 source/cached（那是搜索语义） */
export function listSpots(limit = 50, offset = 0): Promise<SpotListResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return request<SpotListResponse>(`/spots?${params.toString()}`)
}

export function searchSpots(
  keywords: string,
  city?: string,
  limit = 20,
  offset = 0,
): Promise<SpotSearchResponse> {
  const params = new URLSearchParams({ keywords, limit: String(limit), offset: String(offset) })
  if (city) params.set('city', city)
  return request<SpotSearchResponse>(`/spots/search?${params.toString()}`)
}

/** 景点详情（19 节）：🔴 需登录 401；响应 source 可能 local/amap/mock（SpotCard 契约无 source 字段，前端不显示） */
export function getSpot(id: string): Promise<SpotCard> {
  return request<SpotCard>(`/spots/${id}`)
}

// ── 行程：高德 APP 唤端（M7 🔵）──────────────────────────
export function importToAmap(tripId: string): Promise<AmapImportResponse> {
  return post<AmapImportResponse>(`/trips/${tripId}/amap-import`, {})
}

// ── 攻略社区 / 互动（M10 + M11，api.md 2.6）───────────────
export interface GuideListParams {
  city?: string
  keywords?: string
  /** '1' 只看自己的全部（要 token）；省略 = 只看 public */
  mine?: '1'
  /** 只查"在这条行程下发过的"攻略（发布前确认框判据，隐含只看自己） */
  source_trip_id?: string
}

export function listGuides(params: GuideListParams = {}): Promise<Page<GuideListItem>> {
  const q = new URLSearchParams()
  if (params.city) q.set('city', params.city)
  if (params.keywords) q.set('keywords', params.keywords)
  if (params.mine) q.set('mine', params.mine)
  if (params.source_trip_id) q.set('source_trip_id', params.source_trip_id)
  const s = q.toString()
  return request<Page<GuideListItem>>(`/guides${s ? `?${s}` : ''}`)
}

/** 行程 → 攻略（M12）：正文由后端从行程渲染。Idempotency-Key 必须在 click handler 内生成一次，重试沿用 */
export function publishAsGuide(
  tripId: string,
  idempotencyKey: string,
  body: { title?: string | null; destination?: string | null } = {},
): Promise<GuideDetail> {
  return request<GuideDetail>(`/trips/${tripId}/publish-as-guide`, {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(body),
  })
}

export function getGuide(id: string): Promise<GuideDetail> {
  return request<GuideDetail>(`/guides/${id}`)
}

/** 创建攻略（默认 private） */
export function createGuide(body: GuideUpsertRequest): Promise<GuideDetail> {
  return post<GuideDetail>('/guides', body)
}

export function patchGuide(id: string, body: GuideUpsertRequest): Promise<GuideDetail> {
  return request<GuideDetail>(`/guides/${id}`, { method: 'PATCH', body: JSON.stringify(body) })
}

export function publishGuide(id: string): Promise<GuideDetail> {
  return post<GuideDetail>(`/guides/${id}/publish`, {})
}

export function unpublishGuide(id: string): Promise<GuideDetail> {
  return post<GuideDetail>(`/guides/${id}/unpublish`, {})
}

export function deleteGuide(id: string): Promise<void> {
  return request<void>(`/guides/${id}`, { method: 'DELETE' })
}

export function listComments(guideId: string): Promise<Page<CommentItem>> {
  return request<Page<CommentItem>>(`/guides/${guideId}/comments`)
}

export function createComment(guideId: string, content: string): Promise<CommentItem> {
  return post<CommentItem>(`/guides/${guideId}/comments`, { content } satisfies CommentCreateRequest)
}

export function deleteComment(id: string): Promise<void> {
  return request<void>(`/comments/${id}`, { method: 'DELETE' })
}

/** 点赞/取消二合一，返回最终状态，前端直接用，不要本地猜 */
export function toggleLike(req: LikeToggleRequest): Promise<LikeState> {
  return post<LikeState>('/likes/toggle', req)
}

export function getLikeState(target_type: TargetType, target_id: string): Promise<LikeState> {
  const q = new URLSearchParams({ target_type, target_id })
  return request<LikeState>(`/likes?${q.toString()}`)
}

// ── 收藏（M9，api.md 2.6）───────────────────────────────
export function listFavorites(target_type?: TargetType): Promise<Page<FavoriteItem>> {
  const q = target_type ? `?target_type=${target_type}` : ''
  return request<Page<FavoriteItem>>(`/favorites${q}`)
}

/** 收 poi 必传 name（当前 SpotCard 快照）；收 guide/comment 别传 name（后端自己取） */
export function addFavorite(req: FavoriteCreateRequest): Promise<FavoriteItem> {
  return post<FavoriteItem>('/favorites', req)
}

export function deleteFavorite(target_type: TargetType, target_id: string): Promise<void> {
  return request<void>(`/favorites/${target_type}/${target_id}`, { method: 'DELETE' })
}
