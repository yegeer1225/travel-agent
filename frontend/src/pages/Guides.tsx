import { Link } from 'react-router-dom'

/** 旅游攻略 · 占位（M10 真做） */
export default function Guides() {
  return (
    <div className="relative px-14 py-10">
      <div className="deco deco-circle" style={{ width: 12, height: 12, background: 'var(--color-pop-red)', top: 72, left: 852 }} />
      <div className="deco deco-dots" style={{ width: 80, height: 44, bottom: 22, right: 140, opacity: 0.6 }} />

      <h1 className="font-display font-bold text-[34px]">旅游攻略</h1>
      <p className="text-[14px] text-muted mt-1">攻略社区 · 与 AI 路线互转</p>

      <div className="card mt-8 p-10 text-center">
        <div className="font-display font-bold text-lg mb-2">攻略社区建设中</div>
        <p className="text-muted text-[14px] leading-6 max-w-[560px] mx-auto">
          攻略列表、详情、评论与点赞功能将在后续里程碑上线。
          <br />
          你可以先到 <Link to="/assistant" className="text-ink font-bold underline-offset-4">AI 路线规划助手</Link>{' '}
          生成行程，或在 <Link to="/overview" className="text-ink font-bold underline-offset-4">路线总览</Link>{' '}
          中查看结构化行程。
        </p>
      </div>
    </div>
  )
}
