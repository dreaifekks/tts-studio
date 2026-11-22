import { create } from 'zustand'
import { getJSON } from './api/client'
import type { SystemVersions } from './api/types'

export const webVersion: string = __TTS_VERSION__

/**
 * latest 是否比 current 新。开发构建（0.1.3-dev+<sha>）是 0.1.3 之后的提交，和 0.1.3 同一个基础版本时不算旧。
 */
export function hasNewerVersion(latest: string, current: string | null): boolean {
  if (!current) return false
  const parse = (v: string) => v.replace(/^v/, '').split(/[-+]/)[0].split('.').map(Number)
  const a = parse(latest)
  const b = parse(current)
  for (let i = 0; i < 3; i++) if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0)
  return false
}

interface VersionState {
  info: SystemVersions | null
  checking: boolean
  error: string | null
}
export const useVersions = create<VersionState>(() => ({ info: null, checking: false, error: null }))

export const latestVersion = (s: VersionState) => s.info?.studio.latest ?? null
export const updateAvailable = (s: VersionState) => {
  const latest = latestVersion(s)
  return !!latest && hasNewerVersion(latest, webVersion)
}

/** GET /system/versions：主控查 GitHub 上的发布（有缓存）；refresh 时重新查。 */
export async function checkVersions(refresh = false) {
  if (useVersions.getState().checking) return
  useVersions.setState({ checking: true })
  try {
    const info = await getJSON<SystemVersions>('/system/versions', { query: { refresh: refresh || undefined }, retry503: false, signal: AbortSignal.timeout(30_000) })
    useVersions.setState({ info, error: info.studio.error ? '暂时查不到新版本' : null })
  } catch { useVersions.setState({ error: '暂时查不到新版本' }) }
  finally { useVersions.setState({ checking: false }) }
}
