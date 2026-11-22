import type { BlindSummary } from './api/types'
import { displayEvalSet } from './display.ts'

// Title rendering deliberately receives no response A/B combinations.
export function blindTitle(t: Pick<BlindSummary, 'title' | 'character' | 'eval_set' | 'created'>, characterLabel: string): string {
  const legacyAuto = t.title.startsWith(`${t.character} `) && /^[^ ]+ [^:]+:.+\+.+ vs [^:]+:.+\+.+$/.test(t.title)
  // 没填标题时 core 自己起的「slug · 评测集名 · 日期 时间」，换成界面叫法
  const prefix = `${t.character} · ${t.eval_set} · `
  const coreAuto = t.title.startsWith(prefix) && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(t.title.slice(prefix.length))
  return t.title && !legacyAuto && !coreAuto ? t.title : `${characterLabel} · ${displayEvalSet(t.eval_set)} · ${t.created.slice(0, 10)}`
}
