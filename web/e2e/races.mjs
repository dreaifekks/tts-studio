// 迟到的响应、并发的请求、切换中的草稿：每条对应一处审查发现（sol 1–9），全部接口用假数据（mock-core）。
// 每个场景单独一个页面，失败不中断，最后汇总。用法：node e2e/races.mjs [base] [只跑的场景编号，逗号分隔]
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { ALICE_PP, ALICE_V4, candidate, job, makeWorld, mockCore, planFor, weight } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const only = process.argv[3]?.split(',')
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const quote = (world, kind = 'gen') => ({ ...planFor({ kind }, world), chosen: 'colab-L4', confirm: ['ask_over'] })
const confirm409 = (world, kind) => ({ status: 409, body: { detail: '需要确认', code: 'confirm_required', plan: quote(world, kind) } })
const sheetOf = (page) => page.getByRole('dialog', { name: /这次约花/ })

const browser = await chromium.launch()
const results = []
async function scenario(no, name, fn, { overrides = () => ({}), world = makeWorld() } = {}) {
  if (only && !only.includes(String(no))) return
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
  page.setDefaultTimeout(6000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.addInitScript(() => { window.open = () => ({ location: {}, close() {} }) })
  const ctx = { world }
  const core = await mockCore(page, { world, overrides: overrides(ctx) })
  ctx.core = core
  try {
    await fn(page, core, ctx)
    assert.deepEqual(errors, [], '页面异常')
    assert.deepEqual(core.unexpected, [], '意外的接口')
    results.push([no, name, null])
    console.log('PASS', no, name)
  } catch (e) {
    results.push([no, name, e])
    console.log('FAIL', no, name, '—', process.env.VERBOSE ? e.message : (e.message ?? String(e)).split('\n').slice(0, 3).join(' | '))
  } finally {
    await page.close()
  }
}
const posts = (core, key) => core.posts.filter((p) => p.key === key).map((p) => p.body)
const inviteHost = { lan: 'studio-host.local', tailscale: 'studio-host.tailnet.example.test' }

// 1. 确认窗显示提交那一刻的任务；批准后的请求体也是那一刻的
await scenario(1, '确认窗冻结提交时的参数和摘要', async (page, core) => {
  await page.goto(base)
  for (let i = 0; i < 4; i++) await page.getByRole('button', { name: '减少候选' }).click()
  await expect(page.locator('.auto-c')).toContainText('自动：在 studio-host 上生成')
  await expect(page.getByRole('button', { name: '♫ 出候选' })).toBeEnabled()
  await page.getByRole('button', { name: '♫ 出候选' }).click()
  await page.getByRole('button', { name: '增加候选' }).click()
  await expect(sheetOf(page)).toBeVisible()
  const text = await sheetOf(page).locator('p').first().innerText()
  assert.ok(text.includes('（2 句）') && !text.includes('（3 句）'), `确认窗的任务应是提交时的 2 句：${text}`)
  await page.getByRole('button', { name: '用 L4 开始' }).click()
  await expect.poll(() => posts(core, 'POST /tts/candidates/jobs').length).toBe(2)
  const approved = posts(core, 'POST /tts/candidates/jobs')[1]
  assert.equal(approved.n, 2)
  assert.equal(approved.approved, true)
}, {
  overrides: (ctx) => ({
    'POST /tts/candidates/jobs': async (req) => {
      if (!req.postDataJSON().approved) { await sleep(1200); return confirm409(ctx.world) }
      return { body: job('j-gen', 'candidates', 'succeeded', { candidates_result: { candidates: [candidate('a1', 94.6, 'おはよう')] } }) }
    },
  }),
})

// 2. 换设备后旧报价立刻撤下，新报价回来之前不显示旧设备、不能提交
await scenario(2, '报价绑定当前参数和设备', async (page) => {
  await page.goto(base)
  const summary = page.locator('.auto-c')
  const go = page.getByRole('button', { name: '♫ 出候选' })
  await expect(summary).toContainText('自动：在 studio-host 上生成')
  await page.getByRole('button', { name: '更改' }).click()
  await page.getByRole('option', { name: /云端 L4/ }).click()
  await sleep(150)
  const text = await summary.innerText()
  assert.ok(!text.includes('studio-host'), `新报价回来前摘要不该还写 studio-host：${text}`)
  assert.ok(await go.isDisabled(), '新报价回来前不能提交')
  await expect(summary).toContainText('这次：在 云端 L4 上生成')
  await expect(go).toBeEnabled()
}, {
  overrides: (ctx) => ({
    'POST /plan': async (req) => {
      const body = req.postDataJSON()
      if (body.compute === 'colab-L4') await sleep(1500)
      return { body: ctx.world.plan(body) }
    },
  }),
})

// 3. 草稿里临时选的设备不带进下一份草稿
await scenario(3, '临时设备选择绑定草稿', async (page, core) => {
  await page.goto(base + 'blind')
  const summary = page.locator('.auto-c')
  await page.getByRole('button', { name: '＋ 新建盲听' }).click()
  await expect(summary).toContainText('自动：在 studio-host 上出题')
  await page.getByRole('button', { name: '更改' }).click()
  await page.getByRole('option', { name: /gpu-worker/ }).click()
  await expect(summary).toContainText('这次：在 gpu-worker 上出题')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '＋ 新建盲听' }).click()
  await expect(summary).toContainText(/在 .+ 上出题/)
  await page.getByRole('button', { name: '创建并开始生成' }).click()
  await expect.poll(() => posts(core, 'POST /blind').length).toBe(1)
  assert.ok(!('compute' in posts(core, 'POST /blind')[0]), `第二份草稿不该带上次的设备：${posts(core, 'POST /blind')[0].compute}`)
}, {
  overrides: (ctx) => ({
    'POST /blind': () => {
      ctx.world.jobs.push(job('j-blind', 'blind', 'running'))
      return { body: { id: 'b-new', title: '', created: '2026-11-20T10:00:00', character: 'alice', eval_set: 'hard', repeats: 1, ref_id: 'alice-ref-1', a: null, b: null, status: 'generating', job_id: 'j-blind', n_questions: 12, n_ready: 0, n_voted: 0, revealed: false, questions: [] } }
    },
    'GET /blind/b-new': () => ({ body: { id: 'b-new', title: '', created: '2026-11-20T10:00:00', character: 'alice', eval_set: 'hard', repeats: 1, ref_id: 'alice-ref-1', a: null, b: null, status: 'generating', job_id: 'j-blind', n_questions: 12, n_ready: 0, n_voted: 0, revealed: false, questions: [] } }),
  }),
})

// 4a. 停止等待后，迟到的 409 不再弹付费确认
await scenario('4a', '停止等待后丢弃迟到的报价', async (page) => {
  await page.goto(base)
  await expect(page.getByRole('button', { name: '♫ 出候选' })).toBeEnabled()
  await page.getByRole('button', { name: '♫ 出候选' }).click()
  await page.getByRole('button', { name: '取消生成 / 停止等待' }).click()
  await sleep(2500)
  assert.equal(await sheetOf(page).count(), 0, '停止后不该再弹确认窗')
}, {
  overrides: (ctx) => ({ 'POST /tts/candidates/jobs': async () => { await sleep(1500); return confirm409(ctx.world) } }),
})

// 4b. 停止等待后才建好的任务：拿到 id 就取消
await scenario('4b', '停止等待后取消迟到建好的任务', async (page, core) => {
  await page.goto(base)
  await expect(page.getByRole('button', { name: '♫ 出候选' })).toBeEnabled()
  await page.getByRole('button', { name: '♫ 出候选' }).click()
  await page.getByRole('button', { name: '取消生成 / 停止等待' }).click()
  await expect.poll(() => core.posts.filter((p) => p.key === 'POST /jobs/j-late/cancel').length, { timeout: 5000 }).toBe(1)
}, {
  overrides: (ctx) => ({
    'POST /tts/candidates/jobs': async () => {
      await sleep(1500)
      const late = job('j-late', 'candidates', 'running')
      ctx.world.jobs.push(late)
      return { body: late }
    },
    'POST /jobs/j-late/cancel': () => ({ body: job('j-late', 'candidates', 'cancelled') }),
  }),
})

// 5. 生成页的版本列表晚到时，保留用户刚选的版本
await scenario(5, '版本列表返回时保留刚选的有效版本', async (page, core) => {
  await page.goto(base)
  const picker = page.locator('.version-picker select')
  await expect(picker).toHaveValue(ALICE_V4)
  core.world.slowVersions = true
  await page.locator('nav a', { hasText: '作品' }).click()
  await page.locator('nav a', { hasText: '合成' }).click()
  await picker.selectOption(ALICE_PP)
  await sleep(2000)
  await expect(picker).toHaveValue(ALICE_PP)
  await expect(page.getByRole('button', { name: '♫ 出候选' })).toBeEnabled()
  await page.getByRole('button', { name: '♫ 出候选' }).click()
  await expect.poll(() => posts(core, 'POST /tts/candidates/jobs').length).toBe(1)
  assert.equal(posts(core, 'POST /tts/candidates/jobs')[0].version, ALICE_PP)
}, {
  overrides: (ctx) => ({
    'GET /characters/alice/versions': async () => { if (ctx.world.slowVersions) await sleep(1500); return { body: ctx.world.versions.alice } },
    'POST /tts/candidates/jobs': () => ({ body: job('j-gen', 'candidates', 'succeeded', { candidates_result: { candidates: [candidate('a1', 94.6, 'おはよう')] } }) }),
  }),
})

// 6. 盲听换角色：两边的选择立刻清空，新角色的版本回来前不能提交
const bobVersions = [
  { id: 'bob-1', label: '11 月训练', engine: 'v2proplus', gpt: 'bob-e10.ckpt', sovits: 'bob-e4.pth', score: 90.2, score_set: 'hard', recommended: true, source_run: 'bob-1', created: '2026-11-10T09:00:00' },
  { id: 'bob-base', label: '底模（零样本）', engine: 'v2proplus', gpt: 's1v3.ckpt', sovits: 's2Gv2ProPlus.pth', score: null, score_set: null, recommended: false, source_run: null, created: '2026-09-01T00:00:00' },
]
await scenario(6, '盲听换角色清空 A/B 选择', async (page, core) => {
  await page.goto(base + 'blind')
  await page.getByRole('button', { name: '＋ 新建盲听' }).click()
  const create = page.getByRole('button', { name: '创建并开始生成' })
  await expect(create).toBeEnabled()
  await page.locator('label.field', { hasText: '角色' }).first().locator('select').selectOption('bob')
  await sleep(150)
  assert.ok(await create.isDisabled(), '新角色的版本回来前不能提交')
  await expect(create).toBeEnabled({ timeout: 5000 })
  await create.click()
  await expect.poll(() => posts(core, 'POST /blind').length).toBe(1)
  const sent = posts(core, 'POST /blind')[0]
  assert.equal(sent.character, 'bob')
  assert.ok(bobVersions.some((v) => v.id === sent.a.version) && bobVersions.some((v) => v.id === sent.b.version), `A/B 应是新角色的版本：${sent.a.version} / ${sent.b.version}`)
}, {
  overrides: (ctx) => ({
    'GET /characters/bob/versions': async () => { await sleep(1500); return { body: bobVersions } },
    'POST /blind': () => ({ status: 409, body: { detail: 'stop here', code: 'version_unavailable' } }),
  }),
})

// 7. 模型页「设为默认组合」保存的是当前版本绑定的两半
const ALICE_V4_EARLY = 'v4/alice-v4-e5.ckpt/alice-v4-e8.pth'
await scenario(7, '模型页保存按当前版本的两半', async (page, core) => {
  core.world.weights = [weight('alice-v4-e15', 'gpt'), weight('alice-v4-e5', 'gpt', { epoch: 5, is_default: false, default_for: [] }), weight('alice-v4-e8', 'sovits')]
  core.world.versions.alice = [
    ...core.world.versions.alice,
    { id: ALICE_V4_EARLY, label: '11 月训练（早）', engine: 'v4', gpt: 'alice-v4-e5.ckpt', sovits: 'alice-v4-e8.pth', score: 91, score_set: 'hard', recommended: false, source_run: 'run-1105', created: '2026-11-05T09:00:00' },
  ]
  await page.goto(base + 'models')
  await page.locator('.raw-weights-section > summary').click()
  const panel = page.locator('.pair-panel')
  await expect(panel.locator('.version-picker select')).toHaveValue(ALICE_V4)
  core.world.slowWeights = true
  await panel.locator('.version-picker select').selectOption(ALICE_V4_EARLY)
  const save = panel.getByRole('button', { name: '设为默认组合' })
  if (await save.isEnabled()) await save.click()
  else { await expect(save).toBeEnabled({ timeout: 5000 }); await save.click() }
  await expect.poll(() => core.posts.filter((p) => p.key.startsWith('PATCH /weights/')).length, { timeout: 5000 }).toBe(2)
  const keys = core.posts.filter((p) => p.key.startsWith('PATCH /weights/')).map((p) => p.key)
  assert.deepEqual(keys, ['PATCH /weights/alice-v4-e5', 'PATCH /weights/alice-v4-e8'])
}, {
  overrides: (ctx) => {
    const patch = (id) => () => ({ body: ctx.world.weights.find((w) => w.id === id) })
    return {
      'GET /weights': async (req, world, url) => {
        if (world.slowWeights && url.searchParams.get('character')) await sleep(1500)
        return { body: { total: world.weights.length, weights: world.weights } }
      },
      'PATCH /weights/alice-v4-e15': patch('alice-v4-e15'),
      'PATCH /weights/alice-v4-e5': patch('alice-v4-e5'),
      'PATCH /weights/alice-v4-e8': patch('alice-v4-e8'),
    }
  },
})

// 8. 云端向导第 2 步：轮询不重叠，迟到的旧结果不覆盖新结果
await scenario(8, '云端轮询不重叠、按顺序写入', async (page, core, ctx) => {
  await page.goto(base + 'settings/compute')
  await page.getByRole('button', { name: '＋ 添加算力' }).click()
  await page.getByRole('button', { name: /云端 GPU（Colab）/ }).click()
  await page.getByRole('button', { name: /用 Google 账号登录/ }).click()
  await expect(page.getByText('已登录：')).toBeVisible({ timeout: 8000 })
  ctx.scanning = true
  await page.getByRole('button', { name: '下一步' }).click()
  const dialog = page.getByRole('dialog', { name: '添加算力' })
  await expect(dialog).toContainText('A100', { timeout: 15_000 })
  await expect.poll(() => ctx.pending, { timeout: 15_000 }).toBe(0)
  await sleep(300)
  assert.ok((await dialog.innerText()).includes('A100'), '迟到的旧结果不该把 A100 盖掉')
  assert.equal(ctx.maxInFlight, 1, `轮询不该重叠：同时 ${ctx.maxInFlight} 个`)
}, {
  overrides: (ctx) => {
    ctx.inFlight = 0; ctx.maxInFlight = 0; ctx.pending = 0
    const alt = (gpus) => ({ id: 'acct-alt2', label: 'studio.alt2', email: 'studio.alt2@example.com', balance: 85, month_used_estimate: 15, gpus, disabled: false, error: null })
    let scans = 0
    return {
      'POST /cloud/accounts/login': () => ({ body: { login_id: 'L1', auth_url: 'https://accounts.example.com/mock', needs_code: true } }),
      'POST /cloud/accounts/login/L1': () => ({ body: { state: 'waiting' } }),
      'GET /cloud/accounts/login/L1': () => ({ body: { state: 'done', account: alt([]) } }),
      'GET /cloud': async () => {
        const cloud = ctx.world.cloud
        if (!ctx.scanning) return { body: cloud }
        const n = ++scans
        ctx.inFlight++; ctx.pending++
        ctx.maxInFlight = Math.max(ctx.maxInFlight, ctx.inFlight)
        try {
          // 第一次很慢、带的是旧结果；之后的都已经能开 A100
          if (n === 1) { await sleep(7000); return { body: { ...cloud, accounts: [...cloud.accounts, alt([])] } } }
          return { body: { ...cloud, accounts: [...cloud.accounts, alt(['A100'])] } }
        } finally { ctx.inFlight--; ctx.pending-- }
      },
    }
  },
})

// 9a. 作品「生成全部」遇到提醒线：弹确认窗，批准后带批准字段再提交
const work = {
  id: 'w1', title: '朝の挨拶', created: '2026-11-20T09:00:00', updated: '2026-11-20T09:00:00', render: null,
  lines: [{ id: 'l1', text: 'おはよう', character: 'alice', engine: 'v4', version: ALICE_V4, gpt: 'alice-v4-e15.ckpt', sovits: 'alice-v4-e8.pth', ref_id: null, emotion: null, lang: 'ja', pause_after: 0.3, candidates: [], picked: null }],
}
const workOverrides = (path) => (ctx) => ({
  'GET /works': () => ({ body: { works: [{ id: 'w1', title: work.title, created: work.created, updated: work.updated, n_lines: 1, n_picked: 0, rendered_at: null }] } }),
  'GET /works/w1': () => ({ body: work }),
  'PATCH /works/w1': () => ({ body: work }),
  [path]: (req) => {
    if (!req.postDataJSON()?.approved) return confirm409(ctx.world)
    const started = job('j-work', 'work_generate', 'succeeded', { target: 'w1' })
    ctx.world.jobs.push(started)
    return { body: started }
  },
})
const approvedFields = (b) => ({ approved: b.approved, approved_device: b.approved_device, approved_units: b.approved_units, compute: b.compute })
await scenario('9a', '作品生成接入确认流程', async (page, core) => {
  await page.goto(base + 'works')
  await page.getByText('朝の挨拶').first().click()
  await page.getByRole('button', { name: '生成全部' }).click()
  await expect(sheetOf(page)).toBeVisible()
  await page.getByRole('button', { name: '用 L4 开始' }).click()
  await expect.poll(() => posts(core, 'POST /works/w1/generate').length).toBe(2)
  assert.deepEqual(approvedFields(posts(core, 'POST /works/w1/generate')[1]), { approved: true, approved_device: 'colab-L4', approved_units: 0.3, compute: 'colab-L4' })
}, { overrides: workOverrides('POST /works/w1/generate') })

await scenario('9b', '作品单行重生成接入确认流程', async (page, core) => {
  await page.goto(base + 'works')
  await page.getByText('朝の挨拶').first().click()
  await page.getByRole('button', { name: '重新生成' }).first().click()
  await expect(sheetOf(page)).toBeVisible()
  await page.getByRole('button', { name: '用 L4 开始' }).click()
  await expect.poll(() => posts(core, 'POST /works/w1/lines/l1/regenerate').length).toBe(2)
  assert.deepEqual(approvedFields(posts(core, 'POST /works/w1/lines/l1/regenerate')[1]), { approved: true, approved_device: 'colab-L4', approved_units: 0.3, compute: 'colab-L4' })
}, { overrides: workOverrides('POST /works/w1/lines/l1/regenerate') })

// 9c. 盲听「补生成」遇到提醒线：同一个确认流程，批准字段放在请求体里
const failedBlind = {
  id: 'b-failed', title: '补生成测试', created: '2026-11-20T09:00:00', character: 'alice', eval_set: 'hard', repeats: 1, ref_id: 'alice-ref-1', a: null, b: null,
  status: 'failed', job_id: 'j-b1', n_questions: 2, n_ready: 1, n_voted: 0, revealed: false,
  questions: [0, 1].map((i) => ({ id: `q${i}`, index: i, text: `题目 ${i + 1}`, tests: [], ready: i === 0, left_url: null, right_url: null, vote: null, note: '' })),
}
await scenario('9c', '盲听补生成接入确认流程', async (page, core) => {
  await page.goto(base + 'blind')
  await page.locator('.blind-item', { hasText: '补生成测试' }).click()
  await page.getByRole('button', { name: '补生成' }).click()
  await expect(sheetOf(page)).toBeVisible()
  await page.getByRole('button', { name: '用 L4 开始' }).click()
  await expect.poll(() => posts(core, 'POST /blind/b-failed/generate').length).toBe(2)
  assert.deepEqual(approvedFields(posts(core, 'POST /blind/b-failed/generate')[1]), { approved: true, approved_device: 'colab-L4', approved_units: 0.3, compute: 'colab-L4' })
}, {
  overrides: (ctx) => ({
    'GET /blind': () => ({ body: { total: 1, tests: [failedBlind] } }),
    'GET /blind/b-failed': () => ({ body: failedBlind }),
    'POST /blind/b-failed/generate': (req) => {
      if (!req.postDataJSON()?.approved) return confirm409(ctx.world)
      ctx.world.jobs.push(job('j-b2', 'blind', 'running'))
      return { body: { ...failedBlind, status: 'generating', job_id: 'j-b2' } }
    },
  }),
})

// ───────── 第 3 轮复审（sol 复审 cf781b3 的 5 条）

// 10. 扫参确认窗写明提交那一刻的批次（角色、引擎、组数）；等确认时改了表单，批准后发出的还是窗里那一批
await scenario(10, '扫参确认窗显示冻结的批次', async (page, core) => {
  await page.goto(base + 'training')
  await page.getByRole('radio', { name: '扫参' }).click()
  await page.locator('.cpick', { hasText: 'alice' }).click()
  await page.locator('.cpick', { hasText: 'v4' }).click()
  const submit = page.getByRole('button', { name: '提交扫参' })
  await expect(submit).toBeEnabled()
  await submit.click()
  await page.locator('.cpick', { hasText: 'v2ProPlus' }).click()
  await expect(sheetOf(page)).toBeVisible()
  const text = await sheetOf(page).locator('p').first().innerText()
  assert.ok(text.includes('alice') && text.includes('v4') && text.includes('1 组') && !text.includes('v2ProPlus'), `确认窗应写明提交时的批次（alice · v4 · 1 组）：${text}`)
  await page.getByRole('button', { name: '用 L4 开始' }).click()
  await expect.poll(() => posts(core, 'POST /train/sweep').length).toBe(2)
  const approved = posts(core, 'POST /train/sweep')[1]
  assert.deepEqual([approved.characters, approved.engines, approved.approved], [['alice'], ['v4'], true])
}, {
  overrides: (ctx) => ({
    'POST /train/sweep': async (req) => {
      if (!req.postDataJSON().approved) { await sleep(1200); return confirm409(ctx.world, 'train') }
      const started = job('j-sweep', 'sweep', 'running', { target: 'sweep' })
      ctx.world.jobs.push(started)
      return { body: started }
    },
  }),
})

// 11. 报价失败（503）：不能提交（按钮和 ⌘/Ctrl ↩ 都不行），有「重新估算」，报价成功后才能提交
await scenario(11, '没有成功报价不能提交', async (page, core, ctx) => {
  ctx.planDown = true
  await page.goto(base + 'training')
  await page.getByRole('radio', { name: '扫参' }).click()
  await page.locator('.cpick', { hasText: 'alice' }).click()
  await page.locator('.cpick', { hasText: 'v4' }).click()
  await expect(page.locator('.auto-c')).toContainText('暂时估算不了')
  const sweep = page.getByRole('button', { name: '提交扫参' })
  await sleep(300)
  assert.ok(await sweep.isDisabled(), '扫参报价失败时「提交扫参」应不可用')
  ctx.planDown = false
  await page.getByRole('button', { name: '重新估算' }).click()
  await expect(page.locator('.auto-c')).toContainText('自动：在 云端 L4 上扫参')
  await expect(sweep).toBeEnabled()
  // 生成页：同样的规则，快捷键也绕不过去
  ctx.planDown = true
  await page.goto(base)
  await expect(page.locator('.auto-c')).toContainText('暂时估算不了')
  await sleep(300)
  assert.ok(await page.getByRole('button', { name: '♫ 出候选' }).isDisabled(), '生成报价失败时「出候选」应不可用')
  await page.keyboard.press('Control+Enter')
  await sleep(400)
  assert.equal(posts(core, 'POST /tts/candidates/jobs').length, 0, '报价失败时快捷键也不该提交')
  assert.equal(posts(core, 'POST /train/sweep').length, 0)
}, {
  overrides: (ctx) => ({
    'POST /train/sweep/plan': (req) => (ctx.planDown ? { status: 503, body: { detail: '主控暂时忙不过来' } }
      : { body: { plan: planFor({ ...req.postDataJSON(), kind: 'train' }, ctx.world), groups: [] } }),
    'POST /plan': (req) => (ctx.planDown ? { status: 503, body: { detail: '主控暂时忙不过来' } } : { body: planFor(req.postDataJSON(), ctx.world) }),
    'POST /train/sweep': () => 'abort',
    'POST /tts/candidates/jobs': () => 'abort',
  }),
})

// 12. 盲听换角色：等新角色的版本时不能选旧角色的版本；手动组合的两半都得是当前角色能选的权重
await scenario(12, '盲听换角色后不能带上旧角色的权重', async (page, core) => {
  const bob = (id, type, engine) => weight(id, type, { engine, character: 'bob', default_for: ['bob'], evals: [], best_eval: null })
  core.world.weights = [
    weight('alice-v4-e15', 'gpt'), weight('alice-v4-e8', 'sovits'),
    weight('alice-pp-e15', 'gpt', { engine: 'v2proplus' }), weight('alice-pp-e8', 'sovits', { engine: 'v2proplus' }),
    bob('bob-v4-e10', 'gpt', 'v4'), bob('bob-v4-e4', 'sovits', 'v4'), bob('bob-pp-e10', 'gpt', 'v2proplus'), bob('bob-pp-e4', 'sovits', 'v2proplus'),
  ]
  await page.goto(base + 'blind')
  await page.getByRole('button', { name: '＋ 新建盲听' }).click()
  await expect(page.getByRole('button', { name: '创建并开始生成' })).toBeEnabled()
  await page.locator('label.field', { hasText: '角色' }).first().locator('select').selectOption('bob')
  const sides = page.locator('.blind-side')
  const stale = []
  for (const [i, version, gpt] of [[0, ALICE_V4, 'bob-v4-e10.ckpt'], [1, ALICE_PP, 'bob-pp-e10.ckpt']]) {
    const side = sides.nth(i)
    const values = await side.locator('.version-picker select option').evaluateAll((os) => os.map((o) => o.value))
    if (!values.includes(version)) continue
    // 修复前：等 bob 的版本时还能选alice的版本，再手改 GPT，SoVITS 留着alice的
    stale.push(version)
    await side.locator('.version-picker select').selectOption(version)
    if (!(await side.locator('details').evaluate((d) => d.open))) await side.locator('summary', { hasText: '更多设置' }).click()
    await side.locator('label.field', { hasText: 'GPT' }).locator('select').selectOption(gpt)
  }
  await sleep(2600)
  const create = page.getByRole('button', { name: '创建并开始生成' })
  if (await create.isEnabled()) await create.click()
  await sleep(500)
  const aliceHalf = (x) => !!x && [x.gpt, x.sovits].some((n) => n?.startsWith('alice'))
  const bad = posts(core, 'POST /blind').filter((b) => aliceHalf(b.a) || aliceHalf(b.b))
  assert.equal(bad.length, 0, `不该提交旧角色的权重：${JSON.stringify(bad.map((b) => [b.a, b.b]))}`)
  assert.deepEqual(stale, [], `等新角色的版本时不该还能选旧角色的版本：${stale}`)
}, {
  overrides: () => ({
    'GET /characters/bob/versions': async () => { await sleep(2500); return { body: [] } },
    'POST /blind': () => ({ status: 409, body: { detail: 'stop here', code: 'version_unavailable' } }),
  }),
})

// 13. 取消创建中的盲听：迟到的响应不打开那场盲听、不顶掉下一份草稿；迟到建好的任务取消
const lateBlind = {
  id: 'b-late', title: '', created: '2026-11-20T10:00:00', character: 'alice', eval_set: 'hard', repeats: 1, ref_id: 'alice-ref-1', a: null, b: null,
  status: 'generating', job_id: 'j-late-blind', n_questions: 12, n_ready: 0, n_voted: 0, revealed: false, questions: [],
}
await scenario(13, '取消创建中的盲听不顶掉下一份草稿', async (page, core) => {
  await page.goto(base + 'blind')
  await page.getByRole('button', { name: '＋ 新建盲听' }).click()
  const create = page.getByRole('button', { name: '创建并开始生成' })
  await expect(create).toBeEnabled()
  await create.click()
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('button', { name: '＋ 新建盲听' }).click()
  await page.getByPlaceholder('可选标题').fill('第二份草稿')
  await sleep(2000)
  assert.equal(await page.getByPlaceholder('可选标题').count(), 1, '迟到的响应把第二份草稿顶掉了')
  await expect(page.getByPlaceholder('可选标题')).toHaveValue('第二份草稿')
  await expect.poll(() => core.posts.filter((p) => p.key === 'POST /jobs/j-late-blind/cancel').length, { timeout: 3000 }).toBe(1)
}, {
  overrides: (ctx) => ({
    'POST /blind': async () => {
      await sleep(1500)
      ctx.world.jobs.push(job('j-late-blind', 'blind', 'running', { target: 'b-late' }))
      return { body: lateBlind }
    },
    'GET /blind/b-late': () => ({ body: lateBlind }),
    'POST /jobs/j-late-blind/cancel': () => ({ body: job('j-late-blind', 'blind', 'cancelled', { target: 'b-late' }) }),
  }),
})

// 14. 添加局域网设备：生成配对码期间不能换网络（换了也不能拿着原网络的命令往下走）
await scenario(14, '生成邀请时不能换网络', async (page) => {
  await page.goto(base + 'settings/compute')
  await page.getByRole('button', { name: '＋ 添加算力' }).click()
  await page.getByRole('button', { name: /局域网或远程电脑/ }).click()
  const dialog = page.getByRole('dialog', { name: '添加算力' })
  const tailscale = dialog.getByRole('radio', { name: /^Tailscale/ })
  await expect(dialog.getByRole('radio', { name: /家里的局域网/ })).toHaveAttribute('aria-checked', 'true')
  await dialog.getByRole('button', { name: '下一步' }).click()
  await tailscale.click({ force: true })
  await sleep(100)
  const chosen = (await tailscale.getAttribute('aria-checked')) === 'true' ? 'tailscale' : 'lan'
  await expect(dialog.locator('.cmd code')).toBeVisible({ timeout: 5000 })
  const command = await dialog.locator('.cmd code').innerText()
  assert.ok(command.includes(inviteHost[chosen]), `选的是 ${chosen}，命令却是：${command}`)
}, {
  overrides: () => {
    const status = (id) => () => ({ body: { id, state: 'waiting', expires_at: new Date(Date.now() + 590_000).toISOString(), expires_in: 590, redeemed: false, device: null } })
    return {
      'POST /compute/invites': async (req) => {
        const { network } = req.postDataJSON()
        await sleep(1200)
        const host = inviteHost[network]
        return { body: { id: `inv-${network}`, code: 'ABC-123', network, host, expires_at: new Date(Date.now() + 600_000).toISOString(), expires_in: 600,
          commands: { unix: `curl -fsSL http://${host}:9982/install | sh -s -- --pair ABC-123`, windows: null } } }
      },
      'GET /compute/invites/inv-lan': status('inv-lan'),
      'GET /compute/invites/inv-tailscale': status('inv-tailscale'),
      'DELETE /compute/invites/inv-lan': () => ({ status: 204 }),
      'DELETE /compute/invites/inv-tailscale': () => ({ status: 204 }),
    }
  },
})

await browser.close()
const failed = results.filter(([, , e]) => e)
console.log(`${results.length - failed.length}/${results.length} PASS`)
if (failed.length) process.exit(1)
