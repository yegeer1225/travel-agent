import { useParams, Link } from 'react-router-dom'

/** 攻略详情 · 占位（M10 真做） */
export default function GuideDetail() {
  const { id } = useParams<{ id: string }>()
  return (
    <div className="relative px-14 py-10">
      <h1 className="font-display font-bold text-[34px]">攻略详情</h1>
      <div className="card mt-8 p-10 text-center">
        <div className="font-display font-bold text-lg mb-2">攻略详情建设中</div>
        <p className="text-muted text-[14px]">
          攻略 ID：{id}（功能在 M10 上线，暂不可见）
        </p>
        <Link to="/guides" className="btn-outline mt-6">
          返回攻略列表
        </Link>
      </div>
    </div>
  )
}
