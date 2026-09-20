import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import App from './App'
import ErrorBoundary from './components/ErrorBoundary'

// 🔴 高德安全密钥必须在**模块顶层**注入（写在组件里或 useEffect 里就晚了）。
// 2021-12 之后申请的 JS Key 必须配 securityJsCode，否则报 INVALID_USER_SCODE。
window._AMapSecurityConfig = {
  securityJsCode: import.meta.env.VITE_AMAP_JS_SECURITY_CODE ?? '',
}

declare global {
  interface Window { _AMapSecurityConfig: { securityJsCode: string } }
}

createRoot(document.getElementById('root')!).render(
  <ErrorBoundary>
    <StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </StrictMode>
  </ErrorBoundary>,
)
