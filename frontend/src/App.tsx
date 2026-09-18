import type { ReactNode } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import Layout from './layouts/Layout'
import Home from './pages/Home'
import Spots from './pages/Spots'
import SpotDetail from './pages/SpotDetail'
import Assistant from './pages/Assistant'
import Overview from './pages/Overview'
import Guides from './pages/Guides'
import GuideDetail from './pages/GuideDetail'
import Profile from './pages/Profile'
import Login from './pages/Login'
import { isLoggedIn } from './lib/auth'

/** 鉴权前置页守卫：除 /login 外全部要求登录（M9），无 token → 跳登录并带回跳地址 */
function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation()
  if (!isLoggedIn()) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  return <>{children}</>
}

export default function App() {
  return (
    <Routes>
      {/* 登录 / 注册（前置页，不要求 token） */}
      <Route path="/login" element={<Login />} />
      {/* Layout 单实例：游客可逛首页/景点/攻略（操作时 401 → 全局登录弹窗，15.5） */}
      <Route element={<Layout />}>
        <Route path="/" element={<Home />} />
        <Route path="/spots" element={<Spots />} />
        <Route path="/spots/:id" element={<SpotDetail />} />
        <Route path="/guides" element={<Guides />} />
        <Route path="/guides/:id" element={<GuideDetail />} />
        {/* 我的数据页：直接访问引导登录 */}
        <Route path="/assistant" element={<RequireAuth><Assistant /></RequireAuth>} />
        <Route path="/overview" element={<RequireAuth><Overview /></RequireAuth>} />
        <Route path="/overview/:tripId" element={<RequireAuth><Overview /></RequireAuth>} />
        <Route path="/profile" element={<RequireAuth><Profile /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
