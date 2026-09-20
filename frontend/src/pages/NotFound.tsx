import { Link } from 'react-router-dom'

/** 404 页（前端交接.md 20.3）：路由表 path="*" 兜底，不白屏 */
export default function NotFound() {
  return (
    <div className="relative px-14 py-10">
      <div className="mt-24 text-center">
        <div className="font-display font-bold text-5xl mb-3">404</div>
        <p className="text-muted text-[14px] mb-6">页面不存在</p>
        <Link to="/" className="btn-black inline-block">
          回首页
        </Link>
      </div>
    </div>
  )
}
