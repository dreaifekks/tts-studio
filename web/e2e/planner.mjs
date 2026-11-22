// 方案 C「在哪跑」的几条规则：「更改」只对这一次生效；指定的设备不能用时说明原因、不提交；扫参全部完成时显示「都已完成」；
// 评测：版本列表还没回来就点了一行，提交的是这一行（不被随后到的推荐版本盖掉）。
// 全部接口用假数据（mock-core）。用法：node e2e/planner.mjs [base]
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { candidate, job, mockCore, planFor, weight } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const browser = await chromium.launch()
try {
  for (const [kind, viewport] of [['desktop', { width: 1280, height: 800 }], ['mobile', { width: 390, height: 844 }]]) {
    const mobile = kind === 'mobile'
    const page = await browser.newPage({ viewport, isMobile: mobile, hasTouch: mobile })
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    let workerOffline = false
    let slowVersions = false
    const core = await mockCore(page, {
      overrides: {
        'POST /tts/candidates/jobs': (req) => {
          if (workerOffline && req.postDataJSON()?.compute === 'lan-gpu-worker') {
            const plan = planFor({ kind: 'gen', compute: 'lan-gpu-worker' }, core.world)
            plan.candidates = plan.candidates.map((c) => (c.device === 'lan-gpu-worker' ? { ...c, available: false, reason_code: 'offline' } : c))
            return { status: 409, body: { detail: 'compute_unavailable', code: 'compute_unavailable', plan } }
          }
          return { body: job('j-gen', 'candidates', 'succeeded', { candidates_result: { candidates: [candidate('a1', 94.6, 'おはよう')] } }) }
        },
        'POST /train/sweep/plan': () => ({ body: { plan: null, groups: [] } }),
        'GET /characters/alice/versions': async () => {
          if (slowVersions) await new Promise((resolve) => setTimeout(resolve, 1500))
          return { body: core.world.versions.alice }
        },
        'POST /weights/eval': () => {
          const started = job('j-eval-row', 'eval', 'running')
          core.world.jobs.push(started)
          return { body: started }
        },
      },
    })
    const gens = () => core.posts.filter((p) => p.key === 'POST /tts/candidates/jobs').map((p) => p.body)
    const summary = page.locator('.auto-c')
    const pick = async (name) => {
      await page.getByRole('button', { name: '更改' }).click()
      await page.getByRole('option', { name }).click()
    }

    // 「更改」只对这一次：带 compute 提交一次，之后回到自动
    await page.goto(base)
    await expect(summary).toContainText('自动：在 studio-host 上生成')
    // 云端那一行写要先准备环境几分钟（core 给的 prep_sec 取整）
    await page.getByRole('button', { name: '更改' }).click()
    await expect(page.getByRole('option', { name: /云端 L4/ })).toContainText('云端 GPU · 要先准备环境约 6 分钟')
    await page.getByRole('button', { name: '收起' }).click()
    await pick(/gpu-worker/)
    await expect(summary).toContainText('这次：在 gpu-worker 上生成')
    await page.getByRole('button', { name: '♫ 出候选' }).click()
    await expect(page.locator('.candidate')).toHaveCount(1)
    await expect(summary).toContainText('自动：在 studio-host 上生成')
    await page.getByRole('button', { name: '♫ 出候选' }).click()
    await expect.poll(() => gens().length).toBe(2)
    assert.equal(gens()[0].compute, 'lan-gpu-worker')
    assert.equal(gens()[1].compute, undefined)

    // 指定的设备离线：说明原因，不出候选
    workerOffline = true
    await pick(/gpu-worker/)
    await page.getByRole('button', { name: '♫ 出候选' }).click()
    await expect(page.locator('.warn', { hasText: '设备离线' })).toBeVisible()
    assert.equal(gens().length, 3)
    assert.ok(!(await page.locator('body').innerText()).includes('compute_unavailable'), '不显示错误代码')

    // 扫参：报价为 null 表示都已完成
    await page.goto(base + 'training')
    await page.getByRole('radio', { name: '扫参' }).click()
    await page.locator('.cpick', { hasText: 'alice' }).click()
    await page.locator('.cpick', { hasText: 'v4' }).click()
    await expect(summary).toContainText('都已完成')
    await expect(page.getByRole('button', { name: '都已完成' })).toBeDisabled()
    assert.equal(core.posts.filter((p) => p.key === 'POST /train/sweep').length, 0)

    // 评测：版本列表慢，回来之前点了 GPT 第 5 轮那一行
    if (!mobile) {
      core.world.weights = [weight('alice-v4-e15', 'gpt'), weight('alice-v4-e5', 'gpt', { epoch: 5 }), weight('alice-v4-e8', 'sovits')]
      slowVersions = true
      const loaded = page.waitForResponse((r) => r.url().endsWith('/characters/alice/versions'))
      await page.goto(base + 'models')
      await page.locator('.raw-weights-section > summary').click()
      await page.locator('.weights-table tbody tr', { hasText: 'GPT 第 5 轮' }).click()
      await loaded
      await page.waitForTimeout(300)
      await page.locator('.pair-panel').getByRole('button', { name: '评测', exact: true }).click()
      await expect.poll(() => core.posts.filter((p) => p.key === 'POST /weights/eval').length).toBe(1)
      const sent = core.posts.find((p) => p.key === 'POST /weights/eval').body
      assert.deepEqual({ version: sent.version, gpt: sent.gpt, sovits: sent.sovits }, { version: null, gpt: 'alice-v4-e5.ckpt', sovits: 'alice-v4-e8.pth' })
    }

    // core 没给 prep_sec 时云端那一行照旧
    core.world.plan = (input) => ({ ...planFor(input, core.world), candidates: planFor(input, core.world).candidates.map((c) => ({ ...c, prep_sec: null })) })
    await page.goto(base)
    await page.getByRole('button', { name: '更改' }).click()
    await expect(page.getByRole('option', { name: /云端 L4/ })).toContainText('云端 GPU · 要先准备环境 5–7 分钟')

    assert.deepEqual(errors, [])
    assert.deepEqual(core.unexpected, [])
    console.log(`${kind}: override once / compute_unavailable / sweep all done${mobile ? '' : ' / eval row while versions load'} PASS`)
    await page.close()
  }
} finally {
  await browser.close()
}
