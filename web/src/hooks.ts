import { useState } from 'react'
import type { SortPref } from './store'

/** 每个页面记住自己上次的排序（localStorage）。 */
export function useSortPref(page: string, fallback: SortPref) {
  const key = `sort:${page}`
  const [value, setValue] = useState<SortPref>(() => {
    try { return { ...fallback, ...JSON.parse(localStorage.getItem(key) ?? '') } } catch { return fallback }
  })
  const update = (v: SortPref) => { localStorage.setItem(key, JSON.stringify(v)); setValue(v) }
  return [value, update] as const
}
