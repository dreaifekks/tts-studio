// 全局只有一个 <audio>：换一条就替换 src（浏览器会放弃旧的下载），同一条再点切换暂停/继续。
// 首次 play() 直接在播放按钮的 click 手势中同步调用。
import { parseError } from './api/client'

type Listener = () => void

/** <audio> 只给出错误编号；再请求一次拿服务端的原因（盲听音频不在这台主控上时是 audio_elsewhere）。 */
async function describeFailure(src: string) {
  try {
    const res = await fetch(src)
    if (res.ok) return '音频无法播放：格式不支持'
    const err = await parseError(res)
    if (err.code === 'audio_elsewhere') return `音频在 ${err.machine ?? '另一台设备'} 上`
    return res.status === 404 ? '找不到这条音频（可能已被清理）' : err.message
  } catch { return '音频加载失败' }
}

class Player {
  readonly el = new Audio()
  currentId: string | null = null
  playing = false
  private listeners = new Set<Listener>()
  onError: (message: string) => void = () => {}

  constructor() {
    this.el.preload = 'auto'
    const sync = () => { this.playing = !this.el.paused && !this.el.ended; this.emit() }
    for (const ev of ['play', 'pause', 'ended', 'playing']) this.el.addEventListener(ev, sync)
    this.el.addEventListener('error', () => {
      if (!this.currentId) return
      this.playing = false
      this.emit()
      void describeFailure(this.el.src).then((m) => this.onError(m))
    })
  }
  subscribe(fn: Listener) { this.listeners.add(fn); return () => { this.listeners.delete(fn) } }
  private emit() { for (const fn of this.listeners) fn() }

  /** restart=false 时同一条切换暂停/继续。 */
  play(id: string, src: string, restart = false) {
    if (!restart && this.currentId === id) { this.toggle(); return }
    this.currentId = id
    this.el.src = src
    this.el.play().catch((e: unknown) => {
      if (e instanceof DOMException && e.name === 'AbortError') return
      if (e instanceof DOMException && e.name === 'NotAllowedError') this.onError('浏览器阻止了自动播放，请点播放按钮')
    })
    this.emit()
  }
  toggle() {
    if (this.el.paused) this.el.play().catch(() => {})
    else this.el.pause()
  }
  pause() { this.el.pause() }
}

export const player = new Player()
