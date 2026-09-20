import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  message: string
}

/**
 * 渲染期错误兜底（前端交接.md 20.1）：包在 main.tsx 最外层，
 * 任何未捕获渲染错误 → 显示错误卡片而非整页白屏。
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' }

  static getDerivedStateFromError(err: unknown): State {
    return { hasError: true, message: err instanceof Error ? err.message : String(err) }
  }

  componentDidCatch(err: unknown, info: unknown) {
    // dev 下留栈，方便排查
    console.error('[ErrorBoundary]', err, info)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-white">
          <div className="card max-w-[420px] w-full mx-6 p-8 text-center">
            <div className="font-display font-bold text-2xl mb-3">页面出错了</div>
            <p className="text-muted text-[13px] mb-6 break-all">{this.state.message}</p>
            <button className="btn-black" onClick={() => window.location.reload()}>
              重新加载
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
