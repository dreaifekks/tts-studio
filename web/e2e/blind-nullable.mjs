// 盲听：未揭晓时 a / b 为 null（0.1.2 起）也能列表、做题；新建用模型版本，版本失效时提示重选。全部接口用假数据（mock-core）。
// 用法：node e2e/blind-nullable.mjs [base]
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { ALICE_PP, ALICE_V4, job, mockCore } from './mock-core.mjs'

const baseUrl = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const browser = await chromium.launch()
const summary = { id: 'b-null', title: 'alice v2pro:secret-gpt+secret-sovits vs v2pro:secret-gpt+secret-sovits', created: '2026-10-24T15:00:00', character: 'alice', eval_set: 'mini', repeats: 1, ref_id: 'r', a: null, b: null, status: 'ready', job_id: 'j-null', n_questions: 1, n_ready: 1, n_voted: 0, revealed: false }
const questions = [{ id: 'q01', index: 0, text: '盲听测试句子', tests: [], ready: true, left_url: '/blind/b-null/questions/q01/left', right_url: '/blind/b-null/questions/q01/right', vote: null, note: '' }]
try {
  for (const mobile of [false, true]) {
    const page = await browser.newPage({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 } })
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    const created = []
    let results = 0
    let gone = false
    const core = await mockCore(page, {
      overrides: {
        'GET /blind': () => ({ body: { total: 2, tests: [summary, { ...summary, id: 'b-new', title: 'alice · 快速评测 · 2026-10-24 15:00' }] } }),
        'GET /blind/b-null': () => ({ body: { ...summary, questions } }),
        'GET /blind/b-null/result': () => { results++; return { status: 500, body: { detail: 'result fetched before reveal' } } },
        'POST /blind': (req) => {
          created.push(req.postDataJSON())
          return gone ? { status: 409, body: { detail: 'version gone', code: 'version_unavailable' } } : { body: { ...summary, questions } }
        },
      },
    })
    core.world.jobs.push(job('j-null', 'blind', 'succeeded'))
    await page.goto(baseUrl + 'blind')
    // 自动标题用「快速评测」，旧版自动标题（带模型名）不显示原文
    await expect(page.getByText('alice · 快速评测 · 2026-10-24', { exact: true })).toBeVisible()
    await expect(page.getByText('alice · 快速评测 · 2026-10-24 15:00', { exact: true })).toBeVisible()
    assert.ok(!(await page.locator('body').innerText()).includes('secret-gpt'))
    await page.getByText('alice · 快速评测 · 2026-10-24', { exact: true }).click()
    await expect(page.locator('.blind-card')).toContainText('盲听测试句子')
    assert.ok(!(await page.locator('body').innerText()).includes('secret-gpt'))
    await page.getByRole('button', { name: '返回列表', exact: true }).first().click()
    // 新建：A 默认推荐版本，B 默认另一个版本；提交带 version，不带审批字段
    await page.getByRole('button', { name: '＋ 新建盲听' }).click()
    await expect(page.locator('.auto-c')).toContainText('自动：在 studio-host 上出题')
    await page.getByPlaceholder('可选标题').fill('zz-test-mock-null')
    await page.getByRole('button', { name: '创建并开始生成', exact: true }).click()
    await expect(page.locator('.blind-card')).toContainText('盲听测试句子')
    assert.equal(created.length, 1)
    assert.equal(created[0].a.version, ALICE_V4); assert.equal(created[0].a.engine, 'v4')
    assert.equal(created[0].b.version, ALICE_PP); assert.equal(created[0].b.engine, 'v2proplus')
    assert.equal(created[0].title, 'zz-test-mock-null'); assert.equal(created[0].approved, false)
    // 版本失效（409 version_unavailable）：提示重选，两边都清空
    gone = true
    await page.getByRole('button', { name: '返回列表', exact: true }).first().click()
    await page.getByRole('button', { name: '＋ 新建盲听' }).click()
    await expect(page.locator('.auto-c')).toContainText('自动：在 studio-host 上出题')
    await page.getByRole('button', { name: '创建并开始生成', exact: true }).click()
    await expect(page.getByRole('alert').filter({ hasText: '这个模型版本已不可用，请重新选择。' })).toBeVisible()
    await expect(page.getByRole('button', { name: '创建并开始生成', exact: true })).toBeDisabled()
    assert.equal(created.length, 2)
    assert.equal(results, 0)
    assert.deepEqual(errors, [])
    assert.deepEqual(core.unexpected, [])
    console.log(`${mobile ? 'mobile' : 'desktop'}: null list/detail, titles, no model leak, create with versions, version_unavailable PASS`)
    await page.close()
  }
} finally {
  await browser.close()
}
