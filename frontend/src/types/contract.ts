/**
 * ⚠️ 本文件**自动生成**，不要手改。
 *
 * 来源：backend/app/schemas.py（契约唯一事实源）
 * 生成：cd backend && ../.venv/Scripts/python.exe scripts/gen_ts_types.py
 *
 * 三条约定（照着写就不会错）：
 * 1. 字段名是 **snake_case**，后端不下发 camelCase，前端也不转
 * 2. `| null` 是**业务值**（"接口没给这个数据"），不是"还没加载"。
 *    ⚠️ 不要用 `?? ''` / `?? 0` 抹平它 —— 0 是"免费"、'' 是"空字符串"，
 *    跟 null 是两件事，抹平了就是把"没数据"伪装成"有数据"（见 docs/api.md 4.2）
 * 3. **字段一律必填**（没有 `?`）：后端响应是全字段 dump，字段一定在，值可能是 null。
 *    （在前端手写 mock 数据时需要补全所有字段）
 */

// ==============================================================
//  枚举（前端可当 union type 直接用）
// ==============================================================

export type WeatherStatus =
  | "ok"
  | "unavailable"

export type CheckLevel =
  | "hard"
  | "soft"

export type CheckStatus =
  | "pass"
  | "fail"
  | "unknown"

export type CoordSys =
  | "GCJ-02"

export type TripSource =
  | "generated"
  | "pasted"

export type MessageRole =
  | "user"
  | "assistant"

export type TripOpKind =
  | "move"
  | "delete"
  | "update_time"

export type AuthorType =
  | "user"
  | "system"

export type Visibility =
  | "private"
  | "public"

export type TargetType =
  | "guide"
  | "poi"
  | "comment"


// ==============================================================
//  结构体
// ==============================================================

export interface Check {
  level: CheckLevel
  code: string
  status: CheckStatus
  msg: string | null
}

export interface DayStats {
  distance_km: number
  drive_min: number
  walk_km: number
}

export interface Stop {
  seq: number
  name: string
  poi_id: string
  lng: number
  lat: number
  coord_sys: CoordSys
  arrive: string | null
  stay_min: number
  leave: string | null
  from_prev_km: number
  from_prev_drive_min: number
  cost_per_person: number | null
  rating: string | null
  open_time: string | null
  match_reason: string | null
  checks: Check[]
}

export interface Weather {
  status: WeatherStatus
  day_weather: string | null
  night_weather: string | null
  day_temp: number | null
  night_temp: number | null
  day_wind: string | null
  day_power: string | null
  report_time: string | null
  note: string | null
}

export interface TripSummary {
  total_distance_km: number
  total_cost_per_person: number | null
  stop_count: number
  hard_errors: number
  soft_warnings: number
}

export interface ValidationIssue {
  code: string
  /** 人话，直接显示给用户 */
  msg: string
  level: CheckLevel | null
}

export interface Day {
  day: number
  date: string | null
  theme: string | null
  weather: Weather | null
  stops: Stop[]
  day_stats: DayStats | null
  checks: Check[]
}

export interface Validation {
  rounds: number
  fixed: ValidationIssue[]
  remaining: ValidationIssue[]
}

export interface SkeletonDay {
  day: number
  date: string | null
  theme: string
  stops: string[]
}

export interface Trip {
  trip_id: string
  session_id: string | null
  user_id: number
  title: string
  destination: string
  source: TripSource
  created_at: string
  updated_at: string
  days: Day[]
  summary: TripSummary
  validation: Validation
  checks: Check[]
}

export interface ErrorDetail {
  code: string
  msg: string
  detail: Record<string, unknown> | null
}

export interface ToolCallRecord {
  tool: string
  args: Record<string, unknown>
  ok: boolean
  summary: string
  degraded: boolean
}

export interface MessageMeta {
  tool_calls: ToolCallRecord[]
  trip_id: string | null
  checks: Check[]
}

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  meta: MessageMeta | null
  created_at: string
}

export interface Session {
  session_id: string
  title: string
  model: string | null
  created_at: string
  updated_at: string
}

export interface TripOp {
  op: TripOpKind
  day: number
  seq: number
  to_seq: number | null
  to_day: number | null
  arrive: string | null
  stay_min: number | null
}

export interface SpotCard {
  poi_id: string
  name: string
  city: string | null
  district: string | null
  address: string | null
  lng: number
  lat: number
  cost_per_person: number | null
  rating: string | null
  photos: string[]
  typecode: string | null
}

export interface HeroSlide {
  poi_id: string
  name: string
  city: string
  photo: string
}

export interface UserOut {
  id: number
  username: string
  nickname: string | null
  email: string | null
  avatar: string | null
  created_at: string
}

export interface TripSummaryItem {
  trip_id: string
  session_id: string | null
  title: string
  destination: string
  source: TripSource
  created_at: string
  updated_at: string
  summary: TripSummary
}

export interface AmapPoi {
  poi_id: string
  name: string
  alias: string[]
  type: string | null
  typecode: string | null
  lng: number
  lat: number
  address: string | null
  adname: string | null
  cityname: string | null
  adcode: string | null
  tel: string | null
  photos: string[]
  rating: string | null
  cost_per_person: number | null
  open_time: string | null
  open_time_detail: string | null
}

export interface SessionEvent {
  type: "session"
  session_id: string
  title: string | null
}

export interface NodeEvent {
  type: "node"
  node: string
  phase: "start" | "end"
  label: string
  elapsed_ms: number | null
}

export interface ToolCallEvent {
  type: "tool_call"
  call_id: string
  tool: string
  args: Record<string, unknown>
  label: string
}

export interface ToolResultEvent {
  type: "tool_result"
  call_id: string
  tool: string
  ok: boolean
  summary: string
  degraded: boolean
}

export interface TokenEvent {
  type: "token"
  text: string
}

export interface SkeletonEvent {
  type: "skeleton"
  title: string
  days: SkeletonDay[]
  note: string
}

export interface TripEvent {
  type: "trip"
  trip: Trip
}

export interface CheckEvent {
  type: "check"
  round: number
  hard_errors: number
  soft_warnings: number
  checks: Check[]
}

export interface DoneEvent {
  type: "done"
  session_id: string | null
  trip_id: string | null
}

export interface ErrorEvent {
  type: "error"
  code: string
  msg: string
}

export interface ErrorBody {
  error: ErrorDetail
}

export interface SessionCreateRequest {
  title: string | null
  model: string | null
}

export interface SessionDetail {
  session: Session
  messages: ChatMessage[]
}

export interface ChatRequest {
  message: string
  steer: boolean
}

export interface PasteTripRequest {
  text: string
  destination: string | null
}

export interface PatchTripRequest {
  ops: TripOp[]
}

export interface RecheckResponse {
  trip_id: string
  checks: Check[]
  validation: Validation
}

export interface AmapImportResponse {
  url: string
  note: string | null
}

export interface SpotSearchResponse {
  items: SpotCard[]
  total: number
  source: "amap" | "mock" | "local"
  cached: boolean
}

export interface SpotListResponse {
  items: SpotCard[]
  total: number
}

export interface HomeResponse {
  hero: HeroSlide[]
  recommended: SpotCard[]
}

export interface GuideListItem {
  guide_id: string
  title: string
  summary: string
  destination: string | null
  cover: string | null
  author_name: string
  author_type: AuthorType
  visibility: Visibility
  like_count: number
  comment_count: number
  published_at: string | null
  created_at: string
  source_trip_id: string | null
}

export interface GuideDetail {
  guide_id: string
  title: string
  summary: string
  destination: string | null
  cover: string | null
  author_name: string
  author_type: AuthorType
  visibility: Visibility
  like_count: number
  comment_count: number
  published_at: string | null
  created_at: string
  source_trip_id: string | null
  content_md: string
  poi_ids: string[]
  liked: boolean
}

export interface GuideUpsertRequest {
  title: string | null
  content_md: string | null
  destination: string | null
  cover: string | null
  poi_ids: string[] | null
}

export interface CommentItem {
  comment_id: string
  target_type: TargetType
  target_id: string
  author_name: string
  author_type: AuthorType
  content: string
  created_at: string
  is_mine: boolean
}

export interface CommentCreateRequest {
  content: string
}

export interface LikeToggleRequest {
  target_type: TargetType
  target_id: string
}

export interface LikeState {
  target_type: TargetType
  target_id: string
  liked: boolean
  count: number
}

export interface FavoriteCreateRequest {
  target_type: TargetType
  target_id: string
  name: string | null
  cover: string | null
}

export interface FavoriteItem {
  target_type: TargetType
  target_id: string
  name: string
  cover: string | null
  created_at: string
}

export interface RegisterRequest {
  username: string
  password: string
  nickname: string | null
}

export interface LoginRequest {
  username: string
  password: string
}

export interface AuthResponse {
  token: string
  expires_in: number
  user: UserOut
}

export interface UpdateProfileRequest {
  nickname: string | null
  email: string | null
  avatar: string | null
}

export interface AvatarUploadResponse {
  url: string
}

export interface HealthResponse {
  ok: boolean
  mock_mode: boolean
  model_tool: string
  model_plan: string
  amap_configured: boolean
  amap_provider_requested: string
  amap_degraded: boolean
}


// ── 下面这段是脚本手写的，不是生成的 ─────────────────────────

/** 分页外壳。所有列表接口都是它，前端只写一次解包逻辑 */
export interface Page<T> {
  items: T[]
  /** 满足条件的**总数**，不是本页条数 */
  total: number
  limit: number
  offset: number
}

/** SSE 每条 `data:` 反序列化后的联合体。按 `type` 分发 */
export type SSEEvent =
  | SessionEvent
  | NodeEvent
  | ToolCallEvent
  | ToolResultEvent
  | TokenEvent
  | TripEvent
  | CheckEvent
  | DoneEvent
  | ErrorEvent
  | SkeletonEvent

/** 前端必须**忽略未知 type**（方便后端加新事件而不破坏老前端） */
export function isKnownEvent(evt: { type: string }): evt is SSEEvent {
  return [
    'session',
    'node',
    'tool_call',
    'tool_result',
    'token',
    'trip',
    'check',
    'done',
    'error',
    'skeleton',
  ].includes(evt.type)
}
