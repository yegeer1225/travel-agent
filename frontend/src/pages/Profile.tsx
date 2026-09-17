import { useEffect, useRef, useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import type { FavoriteItem, TripSummaryItem, UserOut } from '../types/contract'
import { deleteFavorite, getMe, listFavorites, listTrips, patchMe, uploadAvatar } from '../lib/api'
import { clearToken } from '../lib/auth'

/**
 * 个人中心（M9 落地）：
 * - 资料编辑：昵称/邮箱 patchMe 保存（之前只读不改）
 * - 头像上传：uploadAvatar(file) 拿重编码 URL → patchMe({ avatar }) 用返回值刷新
 * - 收藏 tab：GET /favorites + DELETE /favorites/{type}/{id}
 * - 我的 AI 路线规划记录：GET /trips
 */
export default function Profile() {
  const nav = useNavigate()
  const [user, setUser] = useState<UserOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // 编辑态
  const [editing, setEditing] = useState(false)
  const [nickname, setNickname] = useState('')
  const [email, setEmail] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)

  // 头像
  const fileRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadMsg, setUploadMsg] = useState<string | null>(null)

  // 收藏 + 行程
  const [favorites, setFavorites] = useState<FavoriteItem[]>([])
  const [favError, setFavError] = useState<string | null>(null)
  const [trips, setTrips] = useState<TripSummaryItem[]>([])
  const [tripError, setTripError] = useState<string | null>(null)

  const reloadMe = (u: UserOut) => {
    setUser(u)
    setNickname(u.nickname ?? '')
    setEmail(u.email ?? '')
  }

  useEffect(() => {
    let alive = true
    getMe()
      .then((u) => alive && reloadMe(u))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => alive && setLoading(false))
    listFavorites()
      .then((p) => alive && setFavorites(p.items))
      .catch((e: unknown) => alive && setFavError(e instanceof Error ? e.message : String(e)))
    listTrips()
      .then((p) => alive && setTrips(p.items))
      .catch((e: unknown) => alive && setTripError(e instanceof Error ? e.message : String(e)))
    return () => {
      alive = false
    }
  }, [])

  const logout = () => {
    clearToken()
    nav('/login', { replace: true })
  }

  // ── 资料保存 ────────────────────────────────────────
  const saveProfile = async () => {
    setSaving(true)
    setSaveMsg(null)
    try {
      const u = await patchMe({ nickname: nickname.trim() || null, email: email.trim() || null, avatar: user?.avatar ?? null })
      reloadMe(u)
      setEditing(false)
      setSaveMsg('已保存')
    } catch (e: unknown) {
      setSaveMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  // ── 头像上传（前端先拦类型和 10MB，省一次往返）────────
  const onPickFile = async (f: File | undefined) => {
    if (!f || !user) return
    setUploadMsg(null)
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(f.type)) {
      setUploadMsg('仅支持 JPG / PNG / WebP 图片')
      return
    }
    if (f.size > 10 * 1024 * 1024) {
      setUploadMsg('图片不能超过 10MB')
      return
    }
    setUploading(true)
    try {
      // 🔴 必须用后端重编码后的 URL 刷新头像，不能用本地 blob URL
      const { url } = await uploadAvatar(f)
      const u = await patchMe({ nickname: nickname.trim() || null, email: email.trim() || null, avatar: url })
      reloadMe(u)
      setUploadMsg('头像已更新')
    } catch (e: unknown) {
      setUploadMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setUploading(false)
    }
  }

  const removeFavorite = async (item: FavoriteItem) => {
    try {
      await deleteFavorite(item.target_type, item.target_id)
      setFavorites((prev) => prev.filter((x) => !(x.target_type === item.target_type && x.target_id === item.target_id)))
    } catch (e: unknown) {
      setFavError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="relative px-14 py-10">
      <div className="deco deco-ring" style={{ width: 26, height: 26, bottom: 26, left: 72 }} />

      <h1 className="font-display font-bold text-[34px]">个人中心</h1>
      <p className="text-[14px] text-muted mt-1">账号 · 我的行程 · 收藏</p>

      {/* 当前用户 + 资料编辑 */}
      <div className="card mt-8 p-6 max-w-[620px]">
        {loading && <div className="text-muted text-[13px]">加载中…</div>}
        {error && !loading && <div className="text-status-fail text-[13px]">加载失败：{error}</div>}
        {user && !loading && (
          <div className="flex items-start gap-5">
            {/* 头像：点击上传 */}
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
              className="shrink-0 group relative"
              title="点击更换头像"
            >
              {user.avatar ? (
                <img
                  src={user.avatar}
                  alt="头像"
                  className="w-16 h-16 rounded-[4px] border border-ink object-cover"
                  onError={(e) => {
                    e.currentTarget.style.display = 'none'
                  }}
                />
              ) : (
                <div className="w-16 h-16 rounded-[4px] border border-ink bg-pop-cyan flex items-center justify-center font-display font-bold text-xl">
                  {(user.nickname ?? user.username).slice(0, 1).toUpperCase()}
                </div>
              )}
              <span className="absolute inset-0 flex items-center justify-center bg-ink/50 text-white text-[10px] opacity-0 group-hover:opacity-100 rounded-[4px]">
                {uploading ? '上传中' : '更换'}
              </span>
              <input
                ref={fileRef}
                type="file"
                accept="image/jpeg,image/png,image/webp"
                className="hidden"
                onChange={(e) => {
                  void onPickFile(e.target.files?.[0])
                  e.target.value = ''
                }}
              />
            </button>

            <div className="min-w-0 flex-1">
              {editing ? (
                <div className="space-y-2">
                  <input
                    value={nickname}
                    onChange={(e) => setNickname(e.target.value)}
                    placeholder="昵称"
                    className="field"
                  />
                  <input
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="邮箱（可空）"
                    type="email"
                    className="field"
                  />
                  <div className="flex gap-2">
                    <button className="btn-black !text-[12px]" onClick={() => void saveProfile()} disabled={saving}>
                      {saving ? '保存中…' : '保存'}
                    </button>
                    <button
                      className="btn-outline !text-[12px]"
                      onClick={() => {
                        setEditing(false)
                        setNickname(user.nickname ?? '')
                        setEmail(user.email ?? '')
                        setSaveMsg(null)
                      }}
                    >
                      取消
                    </button>
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="font-bold text-[17px]">{user.nickname ?? user.username}</div>
                    <div className="text-[13px] text-muted mt-0.5">@{user.username}</div>
                    {user.email && <div className="text-[12px] text-muted mt-0.5">{user.email}</div>}
                  </div>
                  <button className="btn-outline !text-[12px]" onClick={() => setEditing(true)}>
                    编辑资料
                  </button>
                </div>
              )}
              {uploadMsg && <div className="text-[12px] text-status-pass mt-2">{uploadMsg}</div>}
              {saveMsg && <div className="text-[12px] text-muted mt-2">{saveMsg}</div>}
            </div>

            <button className="btn-outline !text-[12px]" onClick={logout}>
              退出登录
            </button>
          </div>
        )}
      </div>

      {/* 我的 AI 路线规划记录 */}
      <div className="card mt-6 p-6 max-w-[620px]">
        <h2 className="font-display font-bold text-lg mb-3">我的 AI 路线规划记录</h2>
        {tripError && <div className="text-status-fail text-[13px]">加载失败：{tripError}</div>}
        {!tripError && trips.length === 0 && <div className="text-muted text-[13px]">还没有行程，去 <Link to="/assistant" className="text-ink font-bold underline-offset-4">AI 助手</Link> 生成一份。</div>}
        {trips.length > 0 && (
          <ul className="divide-y divide-ink/10">
            {trips.map((t) => (
              <li key={t.trip_id} className="py-2.5 flex items-center justify-between gap-3">
                <Link to={`/overview/${t.trip_id}`} className="min-w-0">
                  <div className="font-bold text-[14px] truncate">{t.title || t.destination || '未命名行程'}</div>
                  <div className="text-[12px] text-muted">
                    {t.destination || '目的地未知'} · {t.summary.stop_count} 站 · {t.summary.total_distance_km.toFixed(1)} km
                    {t.summary.hard_errors > 0 && <span className="text-status-fail"> · {t.summary.hard_errors} 硬错</span>}
                  </div>
                </Link>
                <span className="text-[11px] text-muted shrink-0">
                  {t.source === 'pasted' ? '粘贴导入' : 'AI 生成'}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 收藏 */}
      <div className="card mt-6 p-6 max-w-[620px]">
        <h2 className="font-display font-bold text-lg mb-3">我的收藏</h2>
        {favError && <div className="text-status-fail text-[13px]">加载失败：{favError}</div>}
        {!favError && favorites.length === 0 && (
          <div className="text-muted text-[13px]">还没有收藏。攻略详情页可收藏攻略，景点卡片可收藏地点。</div>
        )}
        {favorites.length > 0 && (
          <ul className="divide-y divide-ink/10">
            {favorites.map((item) => (
              <li key={`${item.target_type}-${item.target_id}`} className="py-2.5 flex items-center gap-3">
                {item.cover ? (
                  <img src={item.cover} alt="" className="w-10 h-10 rounded-[4px] border border-ink/20 object-cover" onError={(e) => { e.currentTarget.style.display = 'none' }} />
                ) : (
                  <div className="w-10 h-10 rounded-[4px] border border-ink/20 bg-pop-yellow flex items-center justify-center text-[12px] font-bold">
                    {item.name.slice(0, 1)}
                  </div>
                )}
                <div className="min-w-0 flex-1">
                  <div className="font-bold text-[14px] truncate">{item.name}</div>
                  <div className="text-[11px] text-muted">{item.target_type === 'poi' ? '景点' : item.target_type === 'guide' ? '攻略' : '评论'}</div>
                </div>
                <button className="btn-outline !text-[11px] !px-2 !py-1" onClick={() => void removeFavorite(item)}>
                  取消收藏
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

