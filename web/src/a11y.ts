import type { KeyboardEvent } from 'react'

/**
 * 自绘的单选组、标签页、列表、菜单：方向键在 [data-roving] 项之间移动焦点（跳过禁用的），Home / End 到两头。
 * select 给了时移动即选中（单选组、标签页）；不给时只移动焦点，回车 / 空格再选（弹出列表、菜单）。
 */
export function rovingKeyDown(e: KeyboardEvent<HTMLElement>, select?: (el: HTMLElement) => void, orientation: 'both' | 'horizontal' | 'vertical' = 'both') {
  const prev = orientation === 'horizontal' ? ['ArrowLeft'] : orientation === 'vertical' ? ['ArrowUp'] : ['ArrowLeft', 'ArrowUp']
  const next = orientation === 'horizontal' ? ['ArrowRight'] : orientation === 'vertical' ? ['ArrowDown'] : ['ArrowRight', 'ArrowDown']
  if (![...prev, ...next, 'Home', 'End'].includes(e.key)) return
  const items = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('[data-roving]'))
    .filter((el) => el.getAttribute('aria-disabled') !== 'true' && !(el as HTMLButtonElement).disabled)
  if (!items.length) return
  const at = items.indexOf(document.activeElement as HTMLElement)
  const to = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1
    : next.includes(e.key) ? (at + 1) % items.length : (at - 1 + items.length) % items.length
  e.preventDefault()
  items[to].focus()
  select?.(items[to])
}

/** 回车或空格当作点击（给 role=radio / option / tab 的非 button 元素用）。 */
export function activateKeyDown(e: KeyboardEvent<HTMLElement>, action: () => void) {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); action() }
}

/** 弹窗里 Tab 在弹窗内循环。 */
export function trapTab(e: KeyboardEvent<HTMLElement>) {
  if (e.key !== 'Tab') return
  const focusable = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'))
    .filter((el) => !(el as HTMLButtonElement).disabled && el.offsetParent !== null)
  if (!focusable.length) return
  const first = focusable[0], last = focusable[focusable.length - 1]
  if (e.shiftKey && (document.activeElement === first || document.activeElement === e.currentTarget)) { e.preventDefault(); last.focus() }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
}
