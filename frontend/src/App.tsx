import type { ReactNode } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import Layout from './layouts/Layout'
import Home from './pages/Home'
import Spots from './pages/Spots'
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
      {/* 受保护页：六个页面 + 攻略详情 */}
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Home />} />
        <Route path="/spots" element={<Spots />} />
        <Route path="/assistant" element={<Assistant />} />
        <Route path="/overview" element={<Overview />} />
        <Route path="/overview/:tripId" element={<Overview />} />
        <Route path="/guides" element={<Guides />} />
        <Route path="/guides/:id" element={<GuideDetail />} />
        <Route path="/profile" element={<Profile />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
