import { test } from 'node:test'
import assert from 'node:assert/strict'
import { blindTitle } from '../src/blind.ts'
import type { BlindSummary, BlindTest } from '../src/api/types.ts'

const combo = { engine: 'v2pro', gpt: 'secret-gpt', sovits: 'secret-sovits' }
const summary: BlindSummary = { id: 'b-test', title: 'alice v2pro:secret-gpt+secret-sovits vs v2pro:secret-gpt+secret-sovits', created: '2026-10-24T15:00:00', character: 'alice', eval_set: 'mini', repeats: 1, ref_id: 'r', a: null, b: null, status: 'ready', job_id: 'j', n_questions: 1, n_ready: 1, n_voted: 0, revealed: false }
for (const side of [null, combo]) {
  test(`unrevealed ${side === null ? '0.1.2 null' : '0.1.1 objects'} decode and title`, () => {
    const detail = JSON.parse(JSON.stringify({ ...summary, a: side, b: side, questions: [] })) as BlindTest
    assert.deepEqual(detail.a, side); assert.deepEqual(detail.b, side)
    assert.equal(blindTitle(detail, 'alice'), 'alice · 快速评测 · 2026-10-24')
  })
}
test('display never reads response A/B', () => {
  const value = Object.defineProperties({ ...summary }, { a: { get() { throw new Error('read A') } }, b: { get() { throw new Error('read B') } } })
  assert.equal(blindTitle(value, 'alice'), 'alice · 快速评测 · 2026-10-24')
})
test('new automatic and custom titles remain unchanged', () => {
  for (const title of ['Alice · mini · 2026-10-24 15:00', '我选 A vs B', '自定义盲听']) assert.equal(blindTitle({ ...summary, title }, 'alice'), title)
  assert.equal(blindTitle({ ...summary, title: '' }, 'alice'), 'alice · 快速评测 · 2026-10-24')
})
test('core default title (slug · eval set · date time) shows display names', () => {
  assert.equal(blindTitle({ ...summary, title: 'alice · mini · 2026-10-24 15:00' }, 'alice'), 'alice · 快速评测 · 2026-10-24')
  assert.equal(blindTitle({ ...summary, title: 'alice · mini · 2026-10-24 15:00 第二轮' }, 'alice'), 'alice · mini · 2026-10-24 15:00 第二轮')
})
