// 阶段 4 关键路径与主审修复回归测试（全部离线 mock-core）：
// 1. 四工作台导航、旧路由兼容与活跃角色跨页同步
// 2. 角色 prefs 保存 / 恢复 / 切角色回填、同角色切页保留未保存参数与情绪随机、历史复用参数与种子、FLAC 下载
// 3. 主参考 3–10s 校验、情绪随机过滤不合格短参考标签、辅助参考门控（v4 禁用、v2ProPlus 启用并持久化）
// 4. 角色页：模型版本双选盲听 PK、非推荐引擎不被误禁「设为默认」、参考设为默认与去合成、盲听揭晓将胜出者设为默认、切角色退出他角盲听详情、统计切换
// 5. 训练页：冷开默认「已有角色」、重名 slug 与非法 batch 拦截、引擎默认值与 LoRA Rank 联动、训后评测「前两名盲听」与「试用」清理辅参
// 6. 作品页：参考选择弹窗（默认 / 按情绪随机过滤 3–10s / 指定参考 / 辅参查看移除）、切不支持引擎或换角色自动清空 aux_ref_ids
// 7. 设置页「合成与打标偏好」与参考页联动
// 8. 算力 worker_outdated 门控：手选缺失 caps 的局域网设备时禁用提交并显示原因
// 用法：node e2e/stage4.mjs [base]
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { ALICE_PP, ALICE_V4, candidate, job, makeWorld, mockCore, planFor, weight } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const browser = await chromium.launch()
const KANA_PP = 'v2proplus/bob-e15.ckpt/bob-e8.pth'

const deferred = () => {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}

const step = async (name, fn) => {
  await fn()
  console.log('PASS', name)
}

try {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } })
  page.setDefaultTimeout(8000)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))

  const world = makeWorld()
  world.refs = [
    { id: 'alice-ref-1', label: '', audio_url: '/refs/alice-ref-1/audio', character: 'alice', text: 'おはよう。今日はいい天気だね。', lang: 'ja', duration: 4.2, tags: ['平静'] },
    { id: 'alice-ref-2', label: '', audio_url: '/refs/alice-ref-2/audio', character: 'alice', text: 'やった！すごく嬉しいよ！', lang: 'ja', duration: 5.5, tags: ['开心'] },
    { id: 'alice-ref-short', label: '', audio_url: '/refs/alice-ref-short/audio', character: 'alice', text: 'えっ？', lang: 'ja', duration: 1.4, tags: ['惊讶'] },
    { id: 'bob-ref-1', label: '', audio_url: '/refs/bob-ref-1/audio', character: 'bob', text: 'bobのデフォルト音声です。', lang: 'ja', duration: 4.8, tags: ['平静'] },
  ]
  world.versions.bob = [
    { id: KANA_PP, label: 'bob 11 月', engine: 'v2proplus', gpt: 'bob-e15.ckpt', sovits: 'bob-e8.pth', score: 91.2, score_set: 'hard', recommended: true, source_run: 'bob-v2pp', created: '2026-11-12T09:00:00' },
  ]
  // 模拟另一引擎在 /models 里有自动默认，但当前推荐默认仍是 ALICE_V4
  world.models.characters[0].engines.v2proplus.default_gpt = 'alice-pp-e15.ckpt'
  world.models.characters[0].engines.v2proplus.default_sovits = 'alice-pp-e8.pth'
  world.weights = [
    weight('alice-v4-e15', 'gpt'),
    weight('alice-v4-e8', 'sovits'),
    weight('alice-pp-e15', 'gpt', { engine: 'v2proplus', is_default: false, default_for: [] }),
    weight('alice-pp-e8', 'sovits', { engine: 'v2proplus', is_default: false, default_for: [] }),
  ]

  const histItem = candidate('h1', 93.4, '歴史から再利用するセリフ', {
    text: '歴史から再利用するセリフ',
    seed: 778899,
    speed: 1.25,
    fragment_interval: 0.55,
    temperature: 0.85,
    ref_id: 'alice-ref-2',
    aux_ref_ids: ['alice-ref-1'],
    engine: 'v2proplus',
    gpt: 'alice-pp-e15.ckpt',
    sovits: 'alice-pp-e8.pth',
  })

  const revealedBlind = {
    id: 'b-win', title: 'alice PK', created: '2026-11-20T12:00:00', character: 'alice', eval_set: 'hard', repeats: 1,
    ref_id: 'alice-ref-1', a: null, b: null, status: 'ready', job_id: 'j-bw', n_questions: 2, n_ready: 2, n_voted: 2, revealed: true,
    questions: [],
  }
  const revealedResult = {
    id: 'b-win', character: 'alice', eval_set: 'hard', revealed_at: '2026-11-20T12:30:00', n_questions: 2, n_voted: 2,
    a: { label: 'A', combo: { engine: 'v2proplus', gpt: 'alice-pp-e15.ckpt', sovits: 'alice-pp-e8.pth' }, wins: 2, losses: 0, ties: 0, win_rate: 1, auto_score_mean: 92.1 },
    b: { label: 'B', combo: { engine: 'v4', gpt: 'alice-v4-e15.ckpt', sovits: 'alice-v4-e8.pth' }, wins: 0, losses: 2, ties: 0, win_rate: 0, auto_score_mean: 94.6 },
    questions: [],
  }

  const workDoc = {
    id: 'w-s4', title: '第四阶段测试作品', created: '2026-11-20T09:00:00', updated: '2026-11-20T09:00:00', render: null,
    lines: [{
      id: 'l-1', text: '最初のセリフ', character: 'alice', engine: 'v2proplus', version: ALICE_PP,
      gpt: 'alice-pp-e15.ckpt', sovits: 'alice-pp-e8.pth', ref_id: 'alice-ref-1', emotion: null,
      top_k: 15, top_p: 1, temperature: 1, repetition_penalty: 1.35, seed: -1, speed: 1,
      fragment_interval: 0.3, aux_ref_ids: ['alice-ref-2'], text_split_method: 'cut5', sample_steps: null,
      lang: 'ja', pause_after: 0.5, candidates: [], picked: null,
    }],
  }

  const statsQueries = []
  const downloadUrls = []
  const storageQueries = []
  let holdBobPrefsGet = null
  let holdAlicePrefsPatch = null
  let holdAllStatsGet = null
  let holdCloudGenJob = null
  let holdComputeGet = null
  let holdSlowComputeOnce = null
  let holdStorageRefresh = null
  let failModelsOnce = false
  let holdMioVersionsGet = null
  let holdAliceVersionsGet = null
  let cloudConfirmGen = false
  let cloudLiveStatus = false
  let cloudRequeueStage = false
  let alicePrefsGetCount = 0

  const core = await mockCore(page, {
    world,
    overrides: {
      'GET /models': (req, w) => {
        if (failModelsOnce) {
          failModelsOnce = false
          return { status: 500, body: { detail: '模拟加载模型失败' } }
        }
        return { body: w.models }
      },
      'POST /library': (req, w) => {
        const body = req.postDataJSON() ?? {}
        w.library = { ...w.library, selected: true, kind: 'local', path: body.path ?? '/tmp/lib-new' }
        w.storage = {
          ...w.storage,
          measured_at: new Date().toISOString(),
          library: {
            selected: true,
            kind: 'local',
            path: w.library.path,
            total_bytes: Math.round(3.2 * 1024 ** 3),
            local_bytes: Math.round(3.2 * 1024 ** 3),
            categories: [
              { key: 'weights', label: '模型权重', bytes: Math.round(2.0 * 1024 ** 3), files: 4 },
              { key: 'refs', label: '参考音频', bytes: Math.round(1.2 * 1024 ** 3), files: 120 },
            ],
          },
        }
        return { body: w.library }
      },
      'GET /system/storage': async (req, w, u) => {
        const ref = u.searchParams.get('refresh') ?? ''
        storageQueries.push(ref)
        if (ref === 'true' && holdStorageRefresh) {
          const gate = holdStorageRefresh
          holdStorageRefresh = null
          await gate.promise
        }
        if (w.storage === null) return { status: 404, body: { detail: 'Not Found' } }
        if (ref === 'true' && w.storage.library?.kind === 'drive') {
          w.storage = {
            ...w.storage,
            measured_at: new Date().toISOString(),
            library: { ...w.storage.library, local_bytes: Math.round(4.5 * 1024 ** 3) },
          }
        }
        return { body: w.storage }
      },
      'GET /compute': async (req, w) => {
        if (holdSlowComputeOnce) {
          const { gate, snapshot } = holdSlowComputeOnce
          holdSlowComputeOnce = null
          await gate.promise
          return { body: snapshot }
        }
        if (holdComputeGet) await holdComputeGet.promise
        return { body: w.compute }
      },
      'GET /characters/carol/versions': async (req, w) => {
        if (holdMioVersionsGet) await holdMioVersionsGet.promise
        return { body: w.versions.carol ?? [] }
      },
      'GET /characters/alice/versions': async (req, w) => {
        if (holdAliceVersionsGet) await holdAliceVersionsGet.promise
        return { body: w.versions.alice ?? [] }
      },
      'GET /history': () => ({ body: { total: 1, items: [histItem] } }),
      'POST /tts/candidates/jobs': (req, w) => {
        const body = req.postDataJSON() ?? {}
        if (cloudConfirmGen && body.compute === 'colab-L4' && !body.approved) {
          return { status: 409, body: { detail: '需要确认', code: 'confirm_required', plan: planFor({ kind: 'gen', engine: body.engine, items: body.n, compute: 'colab-L4' }, w) } }
        }
        if (cloudConfirmGen && body.compute === 'colab-L4' && body.approved) {
          const jid = cloudLiveStatus ? 'j-gen-cloud-live' : 'j-gen-cloud'
          if (cloudLiveStatus) {
            const l4 = w.compute.devices.find((d) => d.id === 'colab-L4')
            if (l4) { l4.status = 'busy'; l4.activity = '正在生成'; l4.warm = 0 }
          }
          const running = job(jid, 'candidates', 'running', {
            progress: 0.35, stage: '正在上传模型与参考音频…',
            compute: { device: 'colab-L4', name: '云端 L4', type: 'cloud', reason_code: 'pinned', reason: '' },
          })
          w.jobs = [...w.jobs.filter((x) => x.id !== jid), running]
          return { body: running }
        }
        return { body: job('j-gen-s4', 'candidates', 'succeeded', { candidates_result: { candidates: [candidate('c-s4', 95.2, 'テスト候補')] } }) }
      },
      'GET /jobs/j-gen-cloud/events': async (req, w) => {
        const running = job('j-gen-cloud', 'candidates', 'running', {
          progress: 0.35, stage: '正在上传模型与参考音频…',
          compute: { device: 'colab-L4', name: '云端 L4', type: 'cloud', reason_code: 'pinned', reason: '' },
        })
        if (holdCloudGenJob) await holdCloudGenJob.promise
        const done = job('j-gen-cloud', 'candidates', 'succeeded', {
          progress: 1, stage: '已完成',
          compute: { device: 'colab-L4', name: '云端 L4', type: 'cloud', reason_code: 'pinned', reason: '' },
          candidates_result: { candidates: [candidate('c-cloud', 96.1, 'クラウド生成候補', { compute: 'colab-L4' })] },
        })
        w.jobs = w.jobs.map((x) => (x.id === done.id ? done : x))
        return { sse: `data: ${JSON.stringify(running)}\n\ndata: ${JSON.stringify(done)}\n\n` }
      },
      'GET /jobs/j-gen-cloud-live/events': async (req, w) => {
        const running = job('j-gen-cloud-live', 'candidates', 'running', {
          progress: 0.35, stage: '正在上传模型与参考音频…',
          compute: { device: 'colab-L4', name: '云端 L4', type: 'cloud', reason_code: 'pinned', reason: '' },
        })
        if (cloudRequeueStage) {
          cloudRequeueStage = false
          const l4 = w.compute.devices.find((d) => d.id === 'colab-L4')
          if (l4) { l4.status = 'waking'; l4.activity = null; l4.warm = 0 }
          const requeued = job('j-gen-cloud-live', 'candidates', 'queued', {
            progress: 0.35, stage: '等待云端 GPU 重新分配…',
            compute: { device: 'colab-L4', name: '云端 L4', type: 'cloud', reason_code: 'pinned', reason: '' },
          })
          w.jobs = w.jobs.map((x) => (x.id === requeued.id ? requeued : x))
          return { sse: `data: ${JSON.stringify(running)}\n\ndata: ${JSON.stringify(requeued)}\n\n` }
        }
        if (holdCloudGenJob) await holdCloudGenJob.promise
        const l4 = w.compute.devices.find((d) => d.id === 'colab-L4')
        if (l4) { l4.status = 'idle'; l4.activity = null; l4.warm = 1 }
        const done = job('j-gen-cloud-live', 'candidates', 'succeeded', {
          progress: 1, stage: '已完成',
          compute: { device: 'colab-L4', name: '云端 L4', type: 'cloud', reason_code: 'pinned', reason: '' },
          candidates_result: { candidates: [candidate('c-cloud-live', 96.1, 'クラウド生成候補', { compute: 'colab-L4' })] },
        })
        w.jobs = w.jobs.map((x) => (x.id === done.id ? done : x))
        return { sse: `data: ${JSON.stringify(running)}\n\ndata: ${JSON.stringify(done)}\n\n` }
      },
      'GET /blind': () => ({ body: { total: 1, tests: [revealedBlind] } }),
      'GET /blind/b-win': () => ({ body: revealedBlind }),
      'GET /blind/b-win/result': () => ({ body: revealedResult }),
      'GET /reviews/stats': async (req, w, u) => {
        const ch = u.searchParams.get('character') ?? ''
        statsQueries.push(ch)
        if (ch === '' && holdAllStatsGet) await holdAllStatsGet.promise
        return { body: { overall: { n: ch ? 3 : 9, avg_rating: 4.5, good_ratio: 0.8, avg_score: 93, tag_ratios: {} }, dimensions: [] } }
      },
      'GET /characters/bob/prefs': async (req, w) => {
        if (holdBobPrefsGet) await holdBobPrefsGet.promise
        return { body: w.prefs.bob }
      },
      'GET /characters/alice/prefs': (req, w) => {
        alicePrefsGetCount += 1
        return { body: w.prefs.alice }
      },
      'PATCH /characters/alice/prefs': async (req, w) => {
        if (holdAlicePrefsPatch) await holdAlicePrefsPatch.promise
        const body = req.postDataJSON() ?? {}
        const cur = w.prefs.alice
        const next = { ...cur, synth_params: { ...(cur.synth_params ?? {}) }, aux_ref_ids: [...(cur.aux_ref_ids ?? [])] }
        if ('default_ref' in body) next.default_ref = body.default_ref || null
        if ('last_ref_id' in body) next.last_ref_id = body.last_ref_id || null
        if ('aux_ref_ids' in body) next.aux_ref_ids = Array.isArray(body.aux_ref_ids) ? [...new Set(body.aux_ref_ids.filter(Boolean))] : []
        if (body.reset_synth_params) next.synth_params = {}
        if ('synth_params' in body) {
          if (body.synth_params === null) next.synth_params = {}
          else for (const [k, v] of Object.entries(body.synth_params ?? {})) {
            if (v === null) delete next.synth_params[k]
            else next.synth_params[k] = v
          }
        }
        w.prefs.alice = next
        const ch = w.models.characters.find((c) => c.slug === 'alice')
        if (ch) { ch.default_ref = next.default_ref; ch.prefs = next }
        return { body: next }
      },
      'POST /datasets/ds-alice/preview': () => ({ body: { id: 'ds-alice', lines_total: 800, lines_kept: 640, minutes_kept: 52, excluded_short: 60, excluded_long: 20, excluded_interjection: 80, interjection_examples: ['えっ'] } }),
      'POST /train': (req) => {
        const j = job('j-train-s4', 'train', 'succeeded', { target: req.postDataJSON().slug })
        world.jobs.push(j)
        return { body: j }
      },
      'GET /train/j-train-s4/eval': () => ({
        body: {
          job_id: 'j-train-s4', character: 'alice', engine: 'v4', eval_set: 'hard', ref_id: 'alice-ref-1',
          best: { gpt: 'alice-v4-e15.ckpt', sovits: 'alice-v4-e8.pth', score_median: 94.6, cer_median: 0.01, spk_sim_median: 0.84, n: 36 },
          combos: [
            { gpt: 'alice-v4-e15.ckpt', sovits: 'alice-v4-e8.pth', score_median: 94.6, cer_median: 0.01, spk_sim_median: 0.84, n: 36 },
            { gpt: 'v4-base.ckpt', sovits: 'v4-base.pth', score_median: 88.1, cer_median: 0.03, spk_sim_median: 0.78, n: 36 },
          ],
        },
      }),
      'GET /works': () => ({ body: { works: [{ id: 'w-s4', title: workDoc.title, created: workDoc.created, updated: workDoc.updated, n_lines: 1, n_picked: 0, rendered_at: null }] } }),
      'GET /works/w-s4': () => ({ body: workDoc }),
      'PATCH /works/w-s4': (req) => ({ body: { ...workDoc, ...req.postDataJSON() } }),
      'GET /audio/c-s4': (req, w, u) => {
        downloadUrls.push(u.pathname + u.search)
        return { binary: Buffer.from('fLaCmock'), contentType: 'audio/flac' }
      },
    },
  })

  await step('1. 四工作台导航 + 旧路由兼容', async () => {
    await page.goto(base)
    const navTexts = await page.locator('nav a').allInnerTexts()
    assert.deepEqual(navTexts.map((t) => t.replace(/^.\s*/, '').trim()), ['合成', '作品', '角色', '训练', '设置'])

    await page.goto(base + 'references')
    await expect(page.getByRole('tab', { name: '参考音频' })).toHaveAttribute('aria-selected', 'true')
    await page.goto(base + 'stats')
    await expect(page.getByRole('tab', { name: '评价统计' })).toHaveAttribute('aria-selected', 'true')
    await page.goto(base + 'history')
    await expect(page.getByRole('radio', { name: '历史记录' })).toHaveAttribute('aria-checked', 'true')
  })

  await step('2. 合成页：角色 prefs 保存/恢复 + 同角色切页保留未保存参数与情绪随机 + 历史复用 + FLAC 下载', async () => {
    await page.goto(base)
    await expect(page.locator('.param-card .badge')).toHaveText('默认')
    await page.getByLabel('语速').fill('1.3')
    await page.getByLabel('分段间隔').fill('0.6')
    await expect(page.locator('.param-card .badge')).toHaveText('未保存修改')

    // 勾选情绪随机，切到「作品」再切回「合成」应保留未保存参数与情绪随机状态
    await page.getByRole('group', { name: '情绪筛选' }).getByRole('button', { name: '开心' }).click()
    await page.getByLabel(/按所选情绪随机/).check()
    await page.locator('nav a', { hasText: '作品' }).click()
    await page.locator('nav a', { hasText: '合成' }).click()
    await expect(page.getByLabel('语速')).toHaveValue('1.3')
    await expect(page.getByLabel('分段间隔')).toHaveValue('0.6')
    await expect(page.getByLabel(/按所选情绪随机/)).toBeChecked()
    await page.getByLabel(/按所选情绪随机/).uncheck()

    // 存为角色默认
    await page.getByRole('button', { name: '存为角色默认' }).click()
    await expect(page.locator('.param-card .badge')).toHaveText('已保存')
    const savedPatch = core.posts.filter((p) => p.key === 'PATCH /characters/alice/prefs').at(-1)
    assert.equal(savedPatch.body.synth_params.speed, 1.3)
    assert.equal(savedPatch.body.synth_params.fragment_interval, 0.6)

    // 切到bob再切回alice，确认从 prefs 恢复
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('bob')
    await expect(page.getByLabel('语速')).toHaveValue('1')
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('alice')
    await expect(page.getByLabel('语速')).toHaveValue('1.3')
    await expect(page.getByLabel('分段间隔')).toHaveValue('0.6')

    // 恢复默认
    await page.getByRole('button', { name: '恢复默认' }).click()
    await expect(page.locator('.param-card .badge')).toHaveText('默认')
    await expect(page.getByLabel('语速')).toHaveValue('1')

    // 切换到历史记录并一键复用参数与种子
    await page.getByRole('radio', { name: '历史记录' }).click()
    await expect(page.getByText('歴史から再利用するセリフ')).toBeVisible()
    await page.getByRole('button', { name: '复用参数与种子' }).click()
    await expect(page.getByRole('textbox', { name: '台词', exact: true })).toHaveValue('歴史から再利用するセリフ')
    await expect(page.getByLabel('语速')).toHaveValue('1.25')
    await expect(page.getByLabel('分段间隔')).toHaveValue('0.55')
    await page.locator('details summary', { hasText: '进阶设置' }).click()
    await expect(page.getByLabel('固定种子', { exact: true })).toBeChecked()
    await expect(page.getByLabel('固定种子值')).toHaveValue('778899')

    // 出候选并测试 FLAC 下载菜单项
    await page.getByRole('button', { name: '♫ 出候选' }).click()
    await expect(page.locator('.candidate')).toHaveCount(1)
    await page.getByRole('button', { name: '候选 c-s4 的更多操作' }).click()
    await page.getByRole('menuitem', { name: /下载 FLAC/ }).click()
    await expect.poll(() => downloadUrls.length).toBe(1)
    assert.equal(downloadUrls[0], '/audio/c-s4?format=flac')
  })

  await step('3. 主参考 3–10s 校验、情绪随机过滤短参考标签与辅助参考门控（v4 禁用、v2ProPlus 启用）', async () => {
    await page.goto(base)
    await expect(page.locator('.gen-topbar .version-picker select')).toHaveValue(ALICE_V4)
    await expect(page.locator('.aux-note')).toContainText('不支持辅助参考融合')
    await expect(page.getByRole('button', { name: '加入辅助参考 alice-ref-2' })).toBeDisabled()
    await expect(page.getByText('超出 3–10 秒，不可作主参考')).toBeVisible()
    // 仅出现在 1.4s 短参考上的「惊讶」标签不出现在情绪随机筛选按钮组中
    await expect(page.getByRole('group', { name: '情绪筛选' }).getByRole('button', { name: '惊讶' })).toHaveCount(0)

    // 切到 v2proplus，支持 aux_ref_ids（防抖写入）
    await page.locator('.gen-topbar .version-picker select').selectOption(ALICE_PP)
    const addAux = page.getByRole('button', { name: '加入辅助参考 alice-ref-2' })
    await expect(addAux).toBeEnabled()
    await addAux.click()
    await expect(page.locator('.aux-pill')).toHaveCount(1)
    await expect.poll(() => core.posts.filter((p) => p.key === 'PATCH /characters/alice/prefs' && p.body?.aux_ref_ids).length).toBeGreaterThan(0)
    const auxPatch = core.posts.filter((p) => p.key === 'PATCH /characters/alice/prefs' && p.body?.aux_ref_ids).at(-1)
    assert.deepEqual(auxPatch.body.aux_ref_ids, ['alice-ref-2'])

    // 提交生成应携带 aux_ref_ids
    await expect(page.getByRole('button', { name: '♫ 出候选' })).toBeEnabled()
    await page.getByRole('button', { name: '♫ 出候选' }).click()
    await expect.poll(() => core.posts.filter((p) => p.key === 'POST /tts/candidates/jobs').length).toBe(2)
    const genWithAux = core.posts.filter((p) => p.key === 'POST /tts/candidates/jobs').at(-1).body
    assert.deepEqual(genWithAux.aux_ref_ids, ['alice-ref-2'])

    // 切回 v4 后提交生成不发送 aux_ref_ids
    await page.locator('.gen-topbar .version-picker select').selectOption(ALICE_V4)
    await expect(page.getByRole('button', { name: '♫ 出候选' })).toBeEnabled()
    await page.getByRole('button', { name: '♫ 出候选' }).click()
    await expect.poll(() => core.posts.filter((p) => p.key === 'POST /tts/candidates/jobs').length).toBe(3)
    const genOnV4 = core.posts.filter((p) => p.key === 'POST /tts/candidates/jobs').at(-1).body
    assert.deepEqual(genOnV4.aux_ref_ids, [])
  })

  await step('4. 角色页：统一「已为默认」口径 + 双选盲听 PK + 盲听切角退回列表 + 胜出者设为默认 + 参考默认 + 统计切换', async () => {
    await page.goto(base + 'characters/versions')
    await expect(page.locator('.default-voice-card')).toContainText('当前默认声音')
    // 10 月训练（v2proplus）虽有该引擎自动默认，但并非角色推荐默认，其「设为默认」按钮应可点击
    const ppRow = page.locator(`.version-row[data-version-id="${ALICE_PP}"]`)
    await expect(ppRow.getByRole('button', { name: '设为默认' })).toBeEnabled()
    const v4Row = page.locator(`.version-row[data-version-id="${ALICE_V4}"]`)
    await expect(v4Row.getByRole('button', { name: '已为默认' })).toBeDisabled()

    await page.getByLabel('勾选 11 月训练 参与盲听 PK').check()
    await page.getByLabel('勾选 10 月训练 参与盲听 PK').check()
    await expect(page.locator('.pk-bar')).toBeVisible()
    await page.getByRole('button', { name: '发起盲听 PK' }).click()
    await expect(page.getByRole('tab', { name: '盲听' })).toHaveAttribute('aria-selected', 'true')
    const sides = page.locator('.blind-side .version-picker select')
    await expect(sides.nth(0)).toHaveValue(ALICE_V4)
    await expect(sides.nth(1)).toHaveValue(ALICE_PP)

    // 盲听揭晓结果页：切左侧角色到bob时应退出alice的盲听详情回到列表
    await page.getByRole('button', { name: '返回列表' }).click()
    await page.locator('.blind-item', { hasText: 'alice PK' }).click()
    await expect(page.getByRole('button', { name: '将胜出者（A）设为默认' })).toBeVisible()
    await page.locator('.char-list').getByRole('option', { name: /bob/ }).click()
    await expect(page.getByRole('button', { name: '＋ 新建盲听' })).toBeVisible()

    // 切回alice并点击「将胜出者（A）设为默认」，确认更新推荐版本且合成页自动跟随新默认
    await page.locator('.char-list').getByRole('option', { name: /alice/ }).click()
    await page.locator('.blind-item', { hasText: 'alice PK' }).click()
    await page.getByRole('button', { name: '将胜出者（A）设为默认' }).click()
    await expect(page.getByRole('status')).toContainText('已设为 alice / v2ProPlus 的默认组合')
    // 验证 A→参考音频→切 B→回盲听 不会停留在 A 的详情
    await page.getByRole('tab', { name: '参考音频' }).click()
    await page.locator('.char-list').getByRole('option', { name: /bob/ }).click()
    await page.getByRole('tab', { name: '盲听' }).click()
    await expect(page.getByRole('button', { name: '＋ 新建盲听' })).toBeVisible()
    await page.locator('.char-list').getByRole('option', { name: /alice/ }).click()

    // 切回模型版本确认 10 月训练已成为默认，且侧栏回合成页自动跟随新推荐版本 ALICE_PP
    await page.getByRole('tab', { name: '模型版本' }).click()
    await expect(page.locator(`.version-row[data-version-id="${ALICE_PP}"]`).getByRole('button', { name: '已为默认' })).toBeDisabled()
    await page.locator('nav a', { hasText: '合成' }).click()
    await expect(page.locator('.gen-topbar .version-picker select')).toHaveValue(ALICE_PP)

    // 回到参考音频子页：当角色无 last_ref_id 时更换默认参考，侧栏回合成应跟随新默认且不提交旧默认 ID
    world.prefs.alice.last_ref_id = null
    world.prefs.alice.default_ref = 'alice-ref-1'
    world.models.characters[0].default_ref = 'alice-ref-1'
    await page.goto(base)
    await expect(page.locator('.gen-ref-item.selected')).toContainText('おはよう')
    await page.locator('nav a', { hasText: '角色' }).click()
    await page.getByRole('tab', { name: '参考音频' }).click()
    const ref2Row = page.locator('.ref-row[data-id="alice-ref-2"]')
    await ref2Row.getByRole('button', { name: '设为默认' }).click()
    await expect(ref2Row.getByRole('button', { name: '已为默认' })).toBeDisabled()
    await page.locator('nav a', { hasText: '合成' }).click()
    await expect(page.locator('.gen-ref-item.selected')).toContainText('やった！')
    const genBefore = core.posts.filter((p) => p.key === 'POST /tts/candidates/jobs').length
    await page.getByRole('button', { name: '♫ 出候选' }).click()
    await expect.poll(() => core.posts.filter((p) => p.key === 'POST /tts/candidates/jobs').length).toBe(genBefore + 1)
    assert.equal(core.posts.filter((p) => p.key === 'POST /tts/candidates/jobs').at(-1).body.ref_id, null)

    // 评价统计：切换范围时立即清空旧数据进入加载态，再展示新数据
    await page.goto(base + 'characters/stats')
    await expect.poll(() => statsQueries.at(-1)).toBe('alice')
    await expect(page.locator('.summary')).toContainText('3')
    holdAllStatsGet = deferred()
    await page.getByRole('radio', { name: '全部角色' }).click()
    await expect(page.getByText('正在读取统计…')).toBeVisible()
    await expect(page.locator('.summary')).toHaveCount(0)
    holdAllStatsGet.resolve()
    holdAllStatsGet = null
    await expect.poll(() => statsQueries.at(-1)).toBe('')
    await expect(page.locator('.summary')).toContainText('9')
  })

  await step('5. 训练页：冷开默认已有角色 + 重名 slug / 非法 batch 拦截 + 引擎默认值 & LoRA Rank + 训后评测', async () => {
    await page.goto(base + 'training')
    await expect(page.getByRole('radio', { name: '已有角色' })).toHaveAttribute('aria-checked', 'true')
    await expect(page.getByLabel('SoVITS 轮数')).toHaveValue('8')
    await expect(page.getByLabel('LoRA Rank')).toHaveCount(0)

    // 切到 v4（LoRA 引擎）自动套用 2 / 4 / 1 / LoRA Rank 32
    await page.getByLabel('引擎').selectOption('v4')
    await expect(page.getByLabel('SoVITS 轮数')).toHaveValue('2')
    await expect(page.getByLabel('SoVITS 每批')).toHaveValue('4')
    await expect(page.getByLabel('LoRA Rank')).toHaveValue('32')
    await page.getByLabel('LoRA Rank').selectOption('64')
    await page.getByRole('button', { name: '恢复此引擎默认' }).click()
    await expect(page.getByLabel('LoRA Rank')).toHaveValue('32')

    // 非法 batch=0 应被拦截
    await page.getByLabel('SoVITS 每批').fill('0')
    await expect(page.locator('.block-reason')).toContainText('SoVITS 每批大小至少为 1')
    await expect(page.getByRole('button', { name: '⚙ 提交训练' })).toBeDisabled()
    await page.getByLabel('SoVITS 每批').fill('4')

    // 「新建角色」输入重名 slug（alice）应被拦截
    await page.getByRole('radio', { name: '新建角色' }).click()
    await expect(page.locator('.block-reason')).toContainText('请输入新角色的 slug')
    await page.getByLabel('角色 slug').fill('alice')
    await expect(page.locator('.block-reason')).toContainText('该角色已存在，请改名或切到「已有角色」')
    await expect(page.getByRole('button', { name: '⚙ 提交训练' })).toBeDisabled()

    // 切回「已有角色」提交训练并验证 lora_rank=64 与训后评测「前两名盲听」
    await page.getByRole('radio', { name: '已有角色' }).click()
    await page.getByLabel('LoRA Rank').selectOption('64')
    await expect(page.getByRole('button', { name: '⚙ 提交训练' })).toBeEnabled()
    await page.getByRole('button', { name: '⚙ 提交训练' }).click()
    await expect.poll(() => core.posts.filter((p) => p.key === 'POST /train').length).toBe(1)
    assert.equal(core.posts.find((p) => p.key === 'POST /train').body.lora_rank, 64)

    await page.getByRole('button', { name: '查看评测' }).click()
    await expect(page.getByRole('button', { name: '前两名盲听' })).toBeVisible()
    await page.getByRole('button', { name: '前两名盲听' }).click()
    await expect(page.getByRole('tab', { name: '盲听' })).toHaveAttribute('aria-selected', 'true')
    await expect(page.locator('.blind-side .version-picker select').nth(0)).toHaveValue(ALICE_V4)
  })

  await step('6. 作品页：参考弹窗查看/移除辅参、过滤 3–10s 情绪、切 v4 自动清理不兼容 aux_ref_ids', async () => {
    await page.goto(base + 'works')
    await page.getByText('第四阶段测试作品').click()
    assert.equal(await page.getByPlaceholder('参考 ID（优先于情绪）').count(), 0)
    // 初始行带有 1 条辅助参考，触发按钮显示「+ 1 辅参」
    await expect(page.getByRole('button', { name: '选择参考音频' })).toContainText('+ 1 辅参')
    await page.getByRole('button', { name: '选择参考音频' }).click()
    const modal = page.getByRole('dialog', { name: /选择参考音频/ })
    await expect(modal.getByText('本行辅助参考（1）')).toBeVisible()
    // 情绪随机标签列表只包含 3–10s 参考的标签，不含 1.4s 的「惊讶」
    await modal.getByRole('radio', { name: '按情绪随机' }).click()
    await expect(modal.getByRole('button', { name: /惊讶/ })).toHaveCount(0)
    await modal.getByRole('button', { name: /开心/ }).click()
    await expect(page.getByRole('button', { name: '选择参考音频' })).toContainText('按情绪随机：开心')

    // 切到不支持 aux_ref_ids 的 v4 版本，应自动清空该行 aux_ref_ids
    await page.locator('.w-version').selectOption(ALICE_V4)
    await expect(page.getByRole('button', { name: '选择参考音频' })).not.toContainText('辅参')

    // 再次打开选单条参考，并验证短参考禁用
    await page.getByRole('button', { name: '选择参考音频' }).click()
    await modal.getByRole('radio', { name: '指定参考' }).click()
    await expect(modal.getByText('超出 3–10 秒，不可作主参考')).toBeVisible()
    await modal.locator('.gen-ref-item', { hasText: 'やった！' }).getByRole('button', { name: '选择' }).click()
    await expect(page.getByRole('button', { name: '选择参考音频' })).toContainText('参考：やった！')
  })

  await step('7. 设置页「合成与打标偏好」与参考页联动', async () => {
    await page.goto(base + 'settings/general')
    await page.getByRole('radio', { name: 'FLAC' }).click()
    await page.getByLabel('速标对应标签（逗号分隔）').fill('欢快,愤怒,害羞,难过,平静')
    await page.goto(base + 'characters/refs')
    await expect(page.locator('.tag-keys')).toContainText('欢快')
  })

  await step('8. 算力 worker_outdated 门控：手选缺失 caps 设备后禁用提交并展示原因', async () => {
    // 先把 gpu-worker 设为空 caps，并把当前主控上参数改为非默认分段间隔
    await page.goto(base)
    await page.getByRole('button', { name: '更改' }).click()
    await page.getByRole('option', { name: /gpu-worker/ }).click()
    await expect(page.locator('.auto-c')).toContainText('这次：在 gpu-worker 上生成')
    // 模拟 gpu-worker 升级前缺失 fragment_interval 能力，调分段间隔触发重新估算
    world.compute.devices.find((d) => d.id === 'lan-gpu-worker').caps = []
    await page.getByLabel('分段间隔').fill('0.8')
    await expect(page.locator('.where .warn')).toContainText('局域网设备版本较旧，请更新后再用分段间隔')
    await expect(page.getByRole('button', { name: '♫ 出候选' })).toBeDisabled()
  })

  await step('9. 切角色参数隔离/部分覆盖 + 跨角色试用恢复 prefs + 快速 A→B→A 主参考不回退', async () => {
    // 准备：alice有已存 speed=1.35，bob后端有 speed=1.2 / fragment_interval=0.5 / temperature=0.95（不放在 /models 缓存里以测试 GET 在途编辑）
    world.prefs.alice = { slug: 'alice', default_ref: 'alice-ref-1', synth_params: { speed: 1.35 }, last_ref_id: null, aux_ref_ids: [] }
    world.prefs.bob = { slug: 'bob', default_ref: 'bob-ref-1', synth_params: { speed: 1.2, fragment_interval: 0.5, temperature: 0.95 }, last_ref_id: null, aux_ref_ids: [] }
    world.models.characters[0].prefs = world.prefs.alice
    world.models.characters[1].prefs = undefined

    await page.goto(base)
    await expect(page.getByLabel('语速')).toHaveValue('1.35')
    await page.getByLabel('语速').fill('1.45')

    // 切到bob时卡住 GET /characters/bob/prefs：应立即重置为默认值（不串用 1.45），在途仅改温度=0.65，返回后未改字段仍应用 1.2 / 0.5
    holdBobPrefsGet = deferred()
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('bob')
    await expect(page.getByLabel('语速')).toHaveValue('1')
    await page.getByLabel('温度').fill('0.65')
    holdBobPrefsGet.resolve()
    holdBobPrefsGet = null
    await expect(page.getByLabel('语速')).toHaveValue('1.2')
    await expect(page.getByLabel('分段间隔')).toHaveValue('0.5')
    await expect(page.getByLabel('温度')).toHaveValue('0.65')

    // 跨角色训练评测「试用」：当前活跃角色是bob，试用alice评测组合，应重置参数并加载alice prefs（speed=1.35，温度回默认 1）
    await page.locator('nav a', { hasText: '训练' }).click()
    await page.getByRole('button', { name: '查看评测' }).click()
    await page.getByRole('button', { name: '★ 试用' }).click()
    await expect(page.locator('.gen-topbar').getByLabel('角色')).toHaveValue('alice')
    await expect(page.getByLabel('语速')).toHaveValue('1.35')
    await expect(page.getByLabel('温度')).toHaveValue('1')

    // 快速 A→B→A：alice选新主参考 alice-ref-2 后立刻切bob（触发 flush PATCH）再切回alice，GET 必须等 PATCH 完成，主参考不回退
    world.prefs.alice.last_ref_id = null
    await page.goto(base)
    await expect(page.locator('.gen-ref-item.selected')).toContainText('おはよう')
    holdAlicePrefsPatch = deferred()
    const getsBefore = alicePrefsGetCount
    await page.locator('.gen-ref-item', { hasText: 'やった！' }).click()
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('bob')
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('alice')
    // PATCH 尚未返回，同角色的 GET 尚未发出
    assert.equal(alicePrefsGetCount, getsBefore)
    holdAlicePrefsPatch.resolve()
    holdAlicePrefsPatch = null
    await expect.poll(() => alicePrefsGetCount).toBeGreaterThan(getsBefore)
    await expect(page.locator('.gen-ref-item.selected')).toContainText('やった！')
  })

  await step('10. 默认引擎跟随「常用模型」（v2proplus 优先 + 改选 v4 / v5turbo 联动训练/作品/盲听）+ 未安装口径对齐', async () => {
    // 模拟设备仅安装 v2proplus / v2pro / v4 / v5turbo / v5dev（未安装 v1/v2/v3）
    const origEngines = world.compute.devices.map((d) => [...d.engines])
    const modern = ['v2proplus', 'v2pro', 'v4', 'v5turbo', 'v5dev']
    for (const d of world.compute.devices) d.engines = [...modern]

    await page.goto(base + 'settings/general')
    const installedGroup = page.getByRole('group', { name: '常用模型' })
    await expect(installedGroup).toContainText('v2ProPlus')
    await expect(installedGroup).toContainText('v5 Turbo')
    await expect(installedGroup).toContainText('v5 Dev')
    const missingGroup = page.getByRole('group', { name: '当前算力未安装的模型' })
    await expect(missingGroup).toContainText('v3')
    await expect(missingGroup).toContainText('未安装')

    // 初始仅勾选 v2ProPlus → 训练页默认引擎应为 v2proplus，等 /compute 加载完成后断言下拉中未安装引擎标注
    const computeLoaded = page.waitForResponse((r) => new URL(r.url()).pathname === '/compute')
    await page.goto(base + 'training')
    await computeLoaded
    await expect(page.getByLabel('引擎')).toHaveValue('v2proplus')
    await expect.poll(async () => {
      const texts = await page.getByLabel('引擎').locator('option').allInnerTexts()
      return texts[0]?.startsWith('v2ProPlus') && texts.some((t) => t.includes('v1') && t.includes('当前算力未安装'))
    }).toBe(true)

    // 多选 v4 + v2ProPlus 仍优先 v2proplus
    await page.goto(base + 'settings/general')
    await installedGroup.locator('.cpick', { hasText: /^v4$/ }).click()
    await page.goto(base + 'training')
    await expect(page.getByLabel('引擎')).toHaveValue('v2proplus')

    // 仅选 v4 → 训练页、盲听（无版本角色）、作品（无版本角色新增行）全部联动到 v4
    await page.goto(base + 'settings/general')
    await installedGroup.locator('.cpick', { hasText: 'v2ProPlus' }).click()
    await page.goto(base + 'training')
    await expect(page.getByLabel('引擎')).toHaveValue('v4')
    await expect(page.getByLabel('LoRA Rank')).toHaveValue('32')

    // 盲听：给无模型版本的角色 carol 新建盲听，A/B 边默认引擎应为 v4
    await page.goto(base + 'characters/blind')
    await page.locator('.char-list').getByRole('option', { name: /carol/ }).click()
    await page.getByRole('button', { name: '＋ 新建盲听' }).click()
    for (const side of [0, 1]) {
      const box = page.locator('.blind-side').nth(side)
      await box.locator('details summary', { hasText: '更多设置' }).click()
      await expect(box.getByLabel('引擎')).toHaveValue('v4')
    }

    // 作品页：经侧栏跳转保持当前活跃角色为无版本的 carol，添加新行默认引擎应为 v4
    await page.locator('nav a', { hasText: '作品' }).click()
    await page.getByText('第四阶段测试作品').click()
    await page.getByRole('button', { name: '＋ 添加行' }).click()
    await expect(page.locator('.line').last().locator('.w-engine')).toHaveValue('v4')

    // 改选仅 v5 Turbo → 训练 / 盲听 / 作品同步联动为 v5turbo
    await page.goto(base + 'settings/general')
    await installedGroup.locator('.cpick', { hasText: 'v5 Turbo' }).click()
    await installedGroup.locator('.cpick', { hasText: /^v4$/ }).click()
    await page.goto(base + 'training')
    await expect(page.getByLabel('引擎')).toHaveValue('v5turbo')

    await page.goto(base + 'characters/blind')
    await page.locator('.char-list').getByRole('option', { name: /carol/ }).click()
    await page.getByRole('button', { name: '＋ 新建盲听' }).click()
    await page.locator('.blind-side').nth(0).locator('details summary', { hasText: '更多设置' }).click()
    await expect(page.locator('.blind-side').nth(0).getByLabel('引擎')).toHaveValue('v5turbo')

    await page.locator('nav a', { hasText: '作品' }).click()
    await page.getByText('第四阶段测试作品').click()
    await page.getByRole('button', { name: '＋ 添加行' }).click()
    await expect(page.locator('.line').last().locator('.w-engine')).toHaveValue('v5turbo')

    // 恢复常用模型为 v2ProPlus 和原始设备引擎列表
    await page.goto(base + 'settings/general')
    await installedGroup.locator('.cpick', { hasText: 'v2ProPlus' }).click()
    await installedGroup.locator('.cpick', { hasText: 'v5 Turbo' }).click()
    world.compute.devices.forEach((d, i) => { d.engines = origEngines[i] })
  })

  await step('11. 算力 & 云端 GPU 展开速度表：显式显示具体耗时、训练速度与评测打分', async () => {
    await page.goto(base + 'settings/compute')
    await page.getByRole('button', { name: '展开studio-host' }).click()
    const hostTable = page.locator('.dev-wrap').first().locator('.speed-table')
    await expect(hostTable).toContainText('约 2.9 秒/句')
    await expect(hostTable).toContainText('约 31 秒/句')
    await expect(hostTable).toContainText('已实测')
    await expect(hostTable).toContainText('训练')
    await expect(hostTable).toContainText('约 5.5 秒/行（千句约 92 分钟）')
    await expect(hostTable).toContainText('评测打分')
    await expect(hostTable).toContainText('约 4.3 秒/条')

    await page.getByRole('tab', { name: '云端 GPU' }).click()
    await page.getByRole('button', { name: '展开L4' }).click()
    const l4Table = page.locator('.gpu-wrap').first().locator('.speed-table')
    await expect(l4Table).toContainText('v2ProPlus')
    await expect(l4Table).toContainText('约 4.0 秒/句')
    await expect(l4Table).toContainText('v5 Turbo')
    await expect(l4Table).toContainText('约 10 秒/句')
    await expect(l4Table).toContainText('v5 Dev')
    await expect(l4Table).toContainText('约 40 秒/句')
    await expect(l4Table).toContainText('训练')
    await expect(l4Table).toContainText('约 1.1 秒/行（千句约 18 分钟）')
    // 云端 GPU 的 scorer=0，不应显示评测打分行
    await expect(l4Table).not.toContainText('评测打分')
  })

  await step('12. 合成页手动选云端 GPU 跑生成：/plan 携带完整上下文 + 含准备时间/单元 + 确认窗 + 云端阶段进度', async () => {
    world.compute.auto.ask_over = 0.1
    world.prefs.alice.aux_ref_ids = ['alice-ref-2']
    world.prefs.alice.last_ref_id = 'alice-ref-1'
    cloudConfirmGen = true
    holdCloudGenJob = deferred()

    await page.goto(base)
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('alice')
    await page.locator('.gen-topbar .version-picker select').selectOption(ALICE_PP)
    await expect.poll(() => {
      const p = core.posts.filter((x) => x.key === 'POST /plan').at(-1)?.body
      return p?.character === 'alice' && p?.version === ALICE_PP && p?.gpt === 'alice-pp-e15.ckpt'
        && p?.sovits === 'alice-pp-e8.pth' && p?.ref_id === 'alice-ref-1'
        && Array.isArray(p?.aux_ref_ids) && p.aux_ref_ids.includes('alice-ref-2')
    }).toBe(true)

    await page.getByRole('button', { name: '更改' }).click()
    await page.getByRole('option', { name: /云端 L4/ }).click()
    await expect(page.locator('.auto-c')).toContainText('这次：在 云端 L4 上生成')
    await expect(page.locator('.auto-c')).toContainText('含准备约 6 分钟')
    await expect(page.locator('.auto-c')).toContainText('约 0.3 单元')

    await page.getByRole('button', { name: '♫ 出候选' }).click()
    const sheet = page.getByRole('dialog', { name: /这次约花 0\.3 单元/ })
    await expect(sheet).toBeVisible()
    await expect(sheet).toContainText('其中准备环境约 6 分钟')
    await sheet.getByRole('button', { name: '用 L4 开始' }).click()

    // 任务已提交，SSE 尚未结束前应展示云端阶段进度与设备名
    await expect(page.locator('.job-progress')).toContainText('云端 L4')
    await expect(page.locator('.job-progress')).toContainText('正在上传模型与参考音频…')
    await expect(page.locator('.job-progress')).toContainText('35%')

    const genPosts = core.posts.filter((x) => x.key === 'POST /tts/candidates/jobs').slice(-2)
    assert.equal(genPosts[0].body.compute, 'colab-L4')
    assert.equal(genPosts[1].body.approved, true)
    assert.equal(genPosts[1].body.approved_device, 'colab-L4')
    assert.equal(genPosts[1].body.approved_units, 0.3)

    holdCloudGenJob.resolve()
    holdCloudGenJob = null
    await expect(page.locator('.candidate')).toContainText('クラウド生成候補')
    cloudConfirmGen = false
    world.compute.auto.ask_over = 2
  })

  await step('13. 窄桌面（1024px / 900px / 768px）与移动端（390px）无横向溢出巡检', async () => {
    const viewports = [
      { width: 1024, height: 800 },
      { width: 900, height: 800 },
      { width: 768, height: 900 },
      { width: 390, height: 844 },
    ]
    const routes = [
      '',
      'works',
      'characters/versions',
      'characters/refs',
      'characters/blind',
      'characters/stats',
      'training',
      'settings/general',
      'settings/library',
      'settings/compute',
      'settings/cloud',
    ]
    for (const vp of viewports) {
      await page.setViewportSize(vp)
      for (const route of routes) {
        await page.goto(base + route)
        if (route === 'works') {
          await page.getByText('第四阶段测试作品').click()
          await expect(page.locator('.line')).toHaveCount(1)
        } else if (route === 'settings/compute') {
          await page.getByRole('button', { name: '展开studio-host' }).click()
          await expect(page.locator('.speed-table').first()).toBeVisible()
        } else if (route === 'settings/cloud') {
          await page.getByRole('button', { name: '展开L4' }).click()
          await expect(page.locator('.speed-table').first()).toBeVisible()
        } else {
          await expect(page.locator('.page')).toBeVisible()
        }
        const overflow = await page.evaluate(() => ({
          docScroll: document.documentElement.scrollWidth,
          inner: window.innerWidth,
          bodyScroll: document.body.scrollWidth,
        }))
        assert.ok(
          overflow.docScroll <= overflow.inner + 1 && overflow.bodyScroll <= overflow.inner + 1,
          `横向溢出 @ ${vp.width}x${vp.height} /${route}: docScroll=${overflow.docScroll} bodyScroll=${overflow.bodyScroll} inner=${overflow.inner}`,
        )
      }
    }
    await page.setViewportSize({ width: 1400, height: 900 })
  })

  await step('14. 首轮审查回归：异步初始化不覆盖输入 + 纯 LAN 排除 + 扫参超参联动 + 作品版本加载区分 + 合成切无版本角色 + 速度表设备口径与零值边界', async () => {
    // 14a. 训练页：常用模型为仅 LAN 安装的 v1，/compute 迟到前手改 SoVITS 轮数为 12，返回后切到 v2proplus 且不覆盖 12
    const origDevicesEngines = world.compute.devices.map((d) => [...d.engines])
    const origComputeEngines = [...world.compute.engines]
    const origMiniV2Pro = { ...world.compute.devices[0].speeds.v2pro }
    for (const d of world.compute.devices) {
      d.engines = d.type === 'lan' ? ['v1'] : ['v2proplus', 'v4']
    }
    await page.evaluate(() => localStorage.setItem('commonModels', JSON.stringify(['v1'])))
    holdComputeGet = deferred()
    await page.goto(base + 'training')
    await expect(page.getByLabel('引擎')).toHaveValue('v1')
    await page.getByLabel('SoVITS 轮数').fill('12')
    holdComputeGet.resolve()
    holdComputeGet = null
    // 排除纯 LAN 设备的 v1，自动落到 local/cloud 可用的 v2proplus，但手改的 SoVITS 轮数 12 必须保留
    await expect(page.getByLabel('引擎')).toHaveValue('v2proplus')
    await expect(page.getByLabel('SoVITS 轮数')).toHaveValue('12')
    await expect.poll(async () => {
      const texts = await page.getByLabel('引擎').locator('option').allInnerTexts()
      return texts.some((t) => t.includes('v1') && t.includes('当前算力未安装'))
    }).toBe(true)

    // 14b. 扫参：勾选 v4 时自动同步首组为 v4 默认超参（2），手改为 5 后切引擎不再覆盖
    await page.getByRole('radio', { name: '扫参' }).click()
    await page.locator('.cpick', { hasText: /^v4$/ }).click()
    await expect(page.getByLabel('组合 1 SoVITS 轮数')).toHaveValue('2')
    await page.getByLabel('组合 1 SoVITS 轮数').fill('5')
    await page.locator('.cpick', { hasText: 'v2ProPlus' }).click()
    await page.locator('.cpick', { hasText: /^v4$/ }).click()
    await expect(page.getByLabel('组合 1 SoVITS 轮数')).toHaveValue('5')

    // 恢复设备引擎与常用模型为 v2proplus
    world.compute.devices.forEach((d, i) => { d.engines = origDevicesEngines[i] })
    await page.evaluate(() => localStorage.setItem('commonModels', JSON.stringify(['v2proplus'])))

    // 14c. 盲听：carol 版本列表（空列表）延迟返回时，不覆盖用户已手动编辑的 A 侧，未编辑的 B 侧照常补默认
    await page.goto(base + 'characters/blind')
    await page.locator('.char-list').getByRole('option', { name: /carol/ }).click()
    holdMioVersionsGet = deferred()
    await page.getByRole('button', { name: '＋ 新建盲听' }).click()
    const sideA = page.locator('.blind-side').nth(0)
    const sideB = page.locator('.blind-side').nth(1)
    await sideA.locator('details summary', { hasText: '更多设置' }).click()
    await sideA.getByLabel('引擎').selectOption('v5dev')
    holdMioVersionsGet.resolve()
    holdMioVersionsGet = null
    await expect(sideA.getByLabel('引擎')).toHaveValue('v5dev')
    await sideB.locator('details summary', { hasText: '更多设置' }).click()
    await expect(sideB.getByLabel('引擎')).toHaveValue('v2proplus')

    // 14d. 合成页：从有 v4 推荐版本的alice切到无版本的 carol 时，引擎重置为 preferredEngine（v2proplus），不残留 v4
    await page.goto(base)
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('alice')
    await page.locator('.gen-topbar .version-picker select').selectOption(ALICE_V4)
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('carol')
    await page.locator('details summary', { hasText: '进阶设置' }).click()
    await expect(page.locator('details').filter({ hasText: '进阶设置' }).getByLabel('引擎')).toHaveValue('v2proplus')

    // 14e. 作品页：版本请求挂起尚未返回时，新增行复用合成页已缓存的 alice 推荐版本（ALICE_PP）
    await page.locator('.gen-topbar').getByLabel('角色').selectOption('alice')
    await expect(page.locator('.gen-topbar .version-picker select')).toHaveValue(ALICE_PP)
    holdAliceVersionsGet = deferred()
    await page.locator('nav a', { hasText: '作品' }).click()
    await page.getByText('第四阶段测试作品').click()
    await page.getByRole('button', { name: '＋ 添加行' }).click()
    // 未等 /characters/alice/versions 返回，刚添加的行已复用缓存版本（ALICE_PP → 引擎 v2proplus）
    await expect(page.locator('.line')).toHaveCount(2)
    holdAliceVersionsGet.resolve()
    holdAliceVersionsGet = null
    await expect(page.locator('.line').last().locator('.w-version')).toHaveValue(ALICE_PP)

    // 14f. 速度表：主控 compute.engines 仅含 v2proplus 时，云端 L4 仍展示自身 v4 / v5 Turbo / v5 Dev；sec_per_item=0 统一按未测处理
    world.compute.engines = ['v2proplus']
    world.compute.devices[0].speeds.v2pro = { sec_per_item: 0, source: 'measured' }
    await page.evaluate(() => localStorage.setItem('commonModels', JSON.stringify(['v2proplus', 'v4'])))

    await page.goto(base + 'settings/compute')
    await page.getByRole('button', { name: '展开studio-host' }).click()
    const v2ProRow = page.locator('.dev-wrap').first().locator('.speed-table tbody tr', { hasText: /^v2Pro\b/ })
    await expect(v2ProRow).toContainText('未测')
    await expect(v2ProRow).not.toContainText('已实测')
    await expect(v2ProRow.locator('.sic.none')).toHaveCount(1)
    await expect(v2ProRow.locator('.sic.fast')).toHaveCount(0)

    await page.getByRole('tab', { name: '云端 GPU' }).click()
    const l4Wrap = page.locator('.gpu-wrap').first()
    // 收起态应展示 L4 自身的 v4 常用模型速度图标（不被主控 compute.engines 过滤掉）
    await expect(l4Wrap.locator('.gpur .speed')).toContainText('v4')
    await page.getByRole('button', { name: '展开L4' }).click()
    const l4Table = l4Wrap.locator('.speed-table')
    await expect(l4Table).toContainText('v4')
    await expect(l4Table).toContainText('v5 Turbo')
    await expect(l4Table).toContainText('v5 Dev')

    // 恢复现场
    world.compute.engines = origComputeEngines
    world.compute.devices[0].speeds.v2pro = origMiniV2Pro
    await page.evaluate(() => localStorage.setItem('commonModels', JSON.stringify(['v2proplus'])))

    // 14g. 盲听发起 /plan 携带 cloud_capable: false，选择器里云端 GPU 置灰且提示「不支持这项任务」
    await page.goto(base + 'characters/blind')
    await page.locator('.char-list').getByRole('option', { name: /alice/ }).click()
    await page.getByRole('button', { name: '＋ 新建盲听' }).click()
    await expect.poll(() => core.posts.filter((x) => x.key === 'POST /plan' && x.body?.cloud_capable === false).length).toBeGreaterThan(0)
    await page.getByRole('button', { name: '更改' }).click()
    const cloudOpt = page.getByRole('option', { name: /云端 L4/ })
    await expect(cloudOpt).toHaveAttribute('aria-disabled', 'true')
    await expect(cloudOpt).toContainText('不支持这项任务')
  })

  await step('15. 云端 GPU 状态同步（生成/退回排队/训练/热机保留中/防旧响应覆盖）+ 低磁盘提示 + 设置 → 资料库空间占用（Drive/本机/重新统计/切库清空重扫/404 降级/390px 无溢出）', async () => {
    const GB = 1024 ** 3
    const l4 = world.compute.devices.find((d) => d.id === 'colab-L4')
    const studioHost = world.compute.devices.find((d) => d.id === 'local')
    const originalHostDiskFree = studioHost.disk_free_bytes
    const origLibrary = structuredClone(world.library)
    const origStorage = structuredClone(world.storage)

    // 15a. 云端生成任务运行中：任务提交进入活跃态时主动刷新 /compute，状态栏即时显示「云端 L4 正在生成」而非「云端 GPU 未开」
    world.compute.auto.ask_over = 0.1
    cloudConfirmGen = true
    cloudLiveStatus = true
    cloudRequeueStage = true
    holdCloudGenJob = deferred()
    await page.goto(base)
    await expect(page.locator('.gw-label')).toContainText('云端 GPU 未开')
    await page.getByRole('button', { name: '更改' }).click()
    await page.getByRole('option', { name: /云端 L4/ }).click()
    await page.getByRole('button', { name: '♫ 出候选' }).click()
    const sheet = page.getByRole('dialog', { name: /这次约花/ })
    await sheet.getByRole('button', { name: '用 L4 开始' }).click()

    // 任务从 running 退回 queued 时，也必须触发 /compute 刷新（云端 L4 从 正在生成 → 唤醒中）
    await expect(page.locator('.gw-label')).toContainText('云端 L4 唤醒中')
    await expect(page.locator('.gw-label')).not.toContainText('云端 GPU 未开')

    // 切到设置 → 算力，确认云端 GPU 行显示活跃状态而非「未开」
    await page.locator('nav a', { hasText: '设置' }).click()
    await page.getByRole('tab', { name: '算力' }).click()
    await expect(page.locator('.dev-cloud .st')).toContainText('云端 L4 唤醒中')
    await expect(page.locator('.dev-cloud .st .dot.busy')).toHaveCount(1)

    // 迟到的旧 /compute 响应不能覆盖更新后的新状态：先发一个挂起的旧请求（快照为「正在生成」），再触发新请求（返回「保留中」），放行旧请求后仍保持「保留中」
    const staleSnapshot = structuredClone(world.compute)
    staleSnapshot.devices.find((d) => d.id === 'colab-L4').status = 'busy'
    staleSnapshot.devices.find((d) => d.id === 'colab-L4').activity = '正在生成'
    staleSnapshot.devices.find((d) => d.id === 'colab-L4').warm = 0
    const slowGate = deferred()
    holdSlowComputeOnce = { gate: slowGate, snapshot: staleSnapshot }
    await page.getByRole('button', { name: '刷新连接' }).click()
    await expect.poll(() => holdSlowComputeOnce).toBe(null)

    // 任务结束进入终态，服务端切为 idle + warm=1，前端自动发新 /compute，状态栏与算力页变为「云端 L4 保留中」
    holdCloudGenJob.resolve()
    holdCloudGenJob = null
    await expect(page.locator('.gw-label')).toContainText('云端 L4 保留中')
    await expect(page.locator('.dev-cloud .st')).toContainText('云端 L4 保留中')
    await expect(page.locator('.dev-cloud .st .dot.ok')).toHaveCount(1)

    // 放行早先挂起的慢 /compute 响应，不应将状态回退为「云端 L4 正在生成」
    slowGate.resolve()
    await page.waitForTimeout(150)
    await expect(page.locator('.gw-label')).toContainText('云端 L4 保留中')
    await expect(page.locator('.dev-cloud .st')).toContainText('云端 L4 保留中')

    cloudConfirmGen = false
    cloudLiveStatus = false
    world.compute.auto.ask_over = 2

    // 云端训练状态（status='training', activity='正在训练'）
    l4.status = 'training'
    l4.activity = '正在训练'
    l4.warm = 0
    await page.getByRole('button', { name: '刷新连接' }).click()
    await expect(page.locator('.gw-label')).toContainText('云端 L4 正在训练')
    await expect(page.locator('.dev-cloud .st')).toContainText('云端 L4 正在训练')
    await page.getByRole('tab', { name: '云端 GPU' }).click()
    await expect(page.locator('.gpu-wrap').first()).toContainText('正在训练')

    // 释放热机与任务，恢复「云端 GPU 未开」/「未开」
    l4.status = 'idle'
    l4.activity = null
    l4.warm = 0
    await page.getByRole('button', { name: '刷新连接' }).click()
    await expect(page.locator('.gw-label')).toContainText('云端 GPU 未开')
    await page.getByRole('tab', { name: '算力' }).click()
    await expect(page.locator('.dev-cloud .st')).toContainText('未开')

    // 15b. 主控磁盘剩余 < 20 GB（十进制 20_000_000_000）：状态栏橙色提示 + 算力页展开区下方显示本机可用空间与缓存占用
    studioHost.disk_free_bytes = Math.round(12.5 * GB)
    await page.getByRole('button', { name: '刷新连接' }).click()
    await expect(page.locator('.statusbar .disk-warn')).toContainText('磁盘剩余 12.5 GB')
    await page.getByRole('button', { name: '展开studio-host' }).click()
    const devDisk = page.locator('.dev-wrap').first().locator('.dev-disk')
    await expect(devDisk).toContainText('本机可用空间')
    await expect(devDisk).toContainText('12.5 GB')
    await expect(devDisk).toContainText('共 512 GB')
    await expect(devDisk).toContainText('缓存占用')
    await expect(devDisk).toContainText('18.4 GB')
    await expect(devDisk.locator('b.warn')).toContainText('12.5 GB')

    // 15c. 设置 → 资料库「空间占用」卡片：Google Drive 客观文案、分段占比条、明细、单段整盘进度条与「重新统计」
    storageQueries.length = 0
    await page.locator('.statusbar .disk-warn').click()
    await expect(page.getByRole('tab', { name: '资料库' })).toHaveAttribute('aria-selected', 'true')
    const libBlock = page.getByTestId('storage-library')
    await expect(libBlock).toContainText('资料库占用')
    await expect(libBlock).toContainText('Google Drive')
    await expect(libBlock).toContainText('文件总大小 24.6 GB')
    await expect(libBlock).toContainText('本机占用 4.2 GB')
    await expect(libBlock).not.toContainText('其余仅在云端')
    for (const label of ['模型权重', '训练数据集', '训练产出', '作品音频', '参考音频', '其他']) {
      await expect(libBlock).toContainText(label)
    }
    assert.ok((await libBlock.locator('.storage-seg').count()) >= 6)

    const localBlock = page.getByTestId('storage-local')
    await expect(localBlock).toContainText('主控本机存储（studio-host）')
    await expect(localBlock).toContainText('剩余 128 GB / 共 512 GB')
    // 磁盘使用条只用整盘 used_bytes / total_bytes 单段展示
    await expect(localBlock.locator('.disk-bar .storage-seg')).toHaveCount(1)
    await expect(localBlock).toContainText('TTS Studio 本机占用')
    await expect(localBlock).toContainText('18.4 GB')
    for (const label of ['语音引擎与基础模型', '数据集解包缓存', '历史音频与运行数据', '训练暂存', '缓存模型']) {
      await expect(localBlock).toContainText(label)
    }

    // 点击「重新统计」触发 GET /system/storage?refresh=true 并更新数据
    await page.getByRole('button', { name: '重新统计' }).click()
    await expect.poll(() => storageQueries.at(-1)).toBe('true')
    await expect(libBlock).toContainText('本机占用 4.5 GB')

    // 主控本机磁盘 < 20 GB 时标橙色提醒
    world.storage.disk.free_bytes = Math.round(14.2 * GB)
    world.storage.disk.used_bytes = world.storage.disk.total_bytes - world.storage.disk.free_bytes
    await page.getByRole('button', { name: '重新统计' }).click()
    await expect(localBlock).toContainText('剩余 14.2 GB / 共 512 GB')
    await expect(localBlock.locator('.storage-meta.warn')).toContainText('空间不足')

    // 桌面 1400px 与 390px 移动端检查资料库空间占用与算力页（含云端活跃 + 展开磁盘）均无横向溢出
    l4.status = 'training'
    l4.activity = '正在训练'
    await page.getByRole('button', { name: '刷新连接' }).click()
    for (const vp of [{ width: 1400, height: 900 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(vp)
      for (const tabName of ['资料库', '算力']) {
        await page.getByRole('tab', { name: tabName }).click()
        if (tabName === '算力') {
          const btn = page.getByRole('button', { name: /studio-host/ }).first()
          if ((await btn.getAttribute('aria-expanded')) === 'false') await btn.click()
        }
        const ov = await page.evaluate(() => ({
          docScroll: document.documentElement.scrollWidth,
          bodyScroll: document.body.scrollWidth,
          inner: window.innerWidth,
        }))
        assert.ok(
          ov.docScroll <= ov.inner + 1 && ov.bodyScroll <= ov.inner + 1,
          `步骤 15 横向溢出 @ ${vp.width}x${vp.height} ${tabName}: doc=${ov.docScroll} body=${ov.bodyScroll} inner=${ov.inner}`,
        )
      }
    }
    await page.setViewportSize({ width: 1400, height: 900 })

    // 切库成功后立即清空旧存储并强制 refresh=true，即使后续 reloadModels() 失败也不受阻塞
    await page.getByRole('tab', { name: '资料库' }).click()
    const storageGate = deferred()
    holdStorageRefresh = storageGate
    failModelsOnce = true
    const refreshCountBefore = storageQueries.filter((q) => q === 'true').length
    await page.getByRole('button', { name: '更改…' }).click()
    await page.getByLabel('资料库文件夹').fill('/Users/demo/local-lib')
    await page.getByRole('button', { name: '使用这个文件夹' }).click()
    // 立即清空旧存储卡片进入加载态，并已发出 GET /system/storage?refresh=true
    await expect(page.getByText('正在统计空间占用…')).toBeVisible()
    await expect(page.getByTestId('storage-library')).toHaveCount(0)
    await expect.poll(() => storageQueries.filter((q) => q === 'true').length).toBe(refreshCountBefore + 1)
    storageGate.resolve()
    // 收到新本地资料库统计：直接显示总大小 3.2 GB，无 Google Drive 文案
    await expect(libBlock).toContainText('3.2 GB')
    await expect(libBlock).not.toContainText('Google Drive')
    await expect(libBlock).not.toContainText('本机占用')
    // 收起模拟 reloadModels 失败留下的错误条
    if (await page.locator('.error-bar').count()) {
      await page.getByRole('button', { name: '收起错误' }).click()
    }

    // 老主控返回 404 时静默降级不报错
    world.storage = null
    await page.goto(base + 'settings/library')
    await expect(page.locator('h5', { hasText: '位置' })).toBeVisible()
    await expect(page.getByTestId('storage-library')).toHaveCount(0)
    await expect(page.locator('.error-bar')).toHaveCount(0)

    // 恢复现场
    studioHost.disk_free_bytes = originalHostDiskFree
    l4.status = 'idle'
    l4.activity = null
    l4.warm = 0
    world.library = origLibrary
    world.storage = origStorage
  })

  assert.deepEqual(errors, [])
  assert.deepEqual(core.unexpected, [])
  console.log('ALL 15 STAGE 4 E2E SCENARIOS PASS')
  await page.close()
} finally {
  await browser.close()
}
