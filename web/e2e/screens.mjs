// 关键页面截图（桌面 1280 宽和手机 390 宽）。全部接口用假数据（mock-core），不碰真实服务。
// 用法：node e2e/screens.mjs [base] [outDir]
import { chromium, expect } from '@playwright/test'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { candidate, job, mockCore, planFor, weight } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const out = process.argv[3] ?? mkdtempSync(join(tmpdir(), 'tts-studio-screens-'))
mkdirSync(out, { recursive: true })

const preview = { id: 'ds-alice', lines_total: 800, lines_kept: 800, minutes_kept: 65.2, excluded_short: 0, excluded_long: 0, excluded_interjection: 0, interjection_examples: [] }

const browser = await chromium.launch()
const shots = []
for (const [kind, viewport, scale] of [['desktop', { width: 1280, height: 800 }, 1], ['mobile', { width: 390, height: 844 }, 2]]) {
  const mobile = kind === 'mobile'
  const page = await browser.newPage({ viewport, deviceScaleFactor: scale, isMobile: mobile, hasTouch: mobile })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  // 登录要开新标签页：换成假窗口，不真的打开外部网址
  await page.addInitScript(() => { window.open = () => ({ location: {}, close() {} }) })
  let loginPolls = 0
  let codeSent = false
  let trainGate = 'confirm'
  let genGate = 'ok'
  let planDown = false
  const core = await mockCore(page, {
    overrides: {
      'POST /tts/candidates/jobs': () => (genGate === 'gone'
        ? { status: 409, body: { detail: 'version gone', code: 'version_unavailable' } }
        : { body: job('j-gen', 'candidates', 'succeeded', { compute: { device: 'local', name: 'studio-host', type: 'local', reason_code: 'local_fast_enough', reason: '' }, candidates_result: { candidates: [candidate('a1', 94.6, 'おはよう今日はいい天気だね'), candidate('a2', 94.5, 'おはよう、今日はいい天気だね')] } }) }),
      'POST /datasets/ds-alice/preview': () => ({ body: preview }),
      'POST /train/sweep': () => ({ status: 409, body: { detail: '这次要花 2.3 单元', code: 'confirm_required', plan: { ...planFor({ kind: 'train' }, core.world), confirm: ['ask_over'] } } }),
      'POST /train/sweep/plan': (req) => (planDown ? { status: 503, body: { detail: '主控暂时忙不过来，请稍后再试。' } }
        : { body: { plan: planFor({ ...(req.postDataJSON() ?? {}), kind: 'train' }, core.world), groups: [] } }),
      'POST /train': () => (trainGate === 'confirm'
        ? { status: 409, body: { detail: '这次要花 2.3 单元', code: 'confirm_required', plan: { ...planFor({ kind: 'train' }, core.world), confirm: ['ask_over'] } } }
        : 'abort'),
      'POST /cloud/accounts/login': () => ({ body: { login_id: 'L1', auth_url: 'https://accounts.example.com/mock', needs_code: true } }),
      'POST /cloud/accounts/login/L1': () => { codeSent = true; return { body: { state: 'waiting' } } },
      'GET /cloud/accounts/login/L1': () => {
        loginPolls++
        if (!codeSent) return { body: { state: 'waiting' } }
        const alt2 = { id: 'acct-alt2', label: 'studio.alt2', email: 'studio.alt2@example.com', balance: 85, month_used_estimate: 15, gpus: ['T4'], disabled: false, error: null }
        if (!core.world.cloud.accounts.some((a) => a.id === alt2.id)) {
          core.world.cloud.accounts.push(alt2)
          core.world.cloud.total_balance = 4153
          core.world.cloud.account_labels['acct-alt2'] = alt2.email
        }
        return { body: { state: 'done', account: alt2 } }
      },
    },
  })
  const { world } = core
  world.systemVersions.studio.latest = '0.0.1'   // 截图里不出现「有新版本」红点
  world.jobs.push(job('j-eval', 'eval', 'running', { compute: { device: 'lan-gpu-worker', name: 'gpu-worker', type: 'lan', reason_code: 'lan_faster', reason: '' } }))
  world.weights = [weight('alice-v4-e15', 'gpt'), weight('alice-v4-e8', 'sovits')]

  const content = page.locator('.content')
  /** 内容区是滚动容器：截整页时把视口拉到内容高度。 */
  async function shot(name, { full = false } = {}) {
    await page.waitForTimeout(350)
    await page.evaluate(() => { scrollTo(0, 0); document.querySelector('.content')?.scrollTo(0, 0) })
    if (full) {
      const h = await content.evaluate((el) => el.scrollHeight + (document.querySelector('.sidebar')?.getBoundingClientRect().height ?? 0) * (innerWidth <= 900 ? 1 : 0) + 60)
      await page.setViewportSize({ width: viewport.width, height: Math.min(Math.max(h, viewport.height), 2600) })
    }
    const file = `${kind}-${name}.png`
    await page.screenshot({ path: join(out, file) })
    shots.push(file)
    if (full) await page.setViewportSize(viewport)
  }
  const nav = (path) => page.goto(base + path)

  // ── 生成：算力摘要、候选卡片、状态栏
  await nav('')
  await expect(page.locator('.auto-c')).toContainText('自动：在 studio-host 上生成')
  for (let i = 0; i < 4; i++) await page.getByRole('button', { name: '减少候选' }).click()
  await page.getByRole('button', { name: '♫ 出候选' }).click()
  await expect(page.locator('.candidate')).toHaveCount(2)
  await shot('01-generate', { full: mobile })
  await page.getByRole('button', { name: '更改' }).click()
  await expect(page.locator('.chg-pop')).toBeVisible()
  await shot('02-generate-change')
  await page.getByRole('option', { name: /gpu-worker/ }).click()
  await expect(page.locator('.auto-c')).toContainText('这次：在 gpu-worker 上生成')
  await shot('03-generate-override')
  // 版本失效：提示重选
  genGate = 'gone'
  await page.getByRole('button', { name: '♫ 出候选' }).click()
  await expect(page.locator('.version-picker [role="alert"]')).toContainText('这个模型版本已不可用，请重新选择。')
  await shot('04-version-unavailable')
  genGate = 'ok'

  // ── 训练：超过提醒线时的确认窗
  await nav('training')
  await page.getByLabel('角色', { exact: true }).selectOption('alice')
  await page.getByLabel('引擎', { exact: true }).selectOption('v4')
  await expect(page.locator('.auto-c')).toContainText('自动：在 云端 L4 上训练')
  await shot('05-training', { full: mobile })
  await page.getByRole('button', { name: '⚙ 提交训练' }).click()
  await expect(page.getByRole('dialog', { name: /这次约花 2.3 单元/ })).toBeVisible()
  await shot('06-confirm')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  await page.getByRole('radio', { name: '扫参' }).click()
  await page.locator('.cpick', { hasText: 'alice' }).click()
  await page.locator('.cpick', { hasText: 'v4' }).click()
  await expect(page.locator('.auto-c')).toContainText('自动：在 云端 L4 上扫参')
  await shot('07-sweep', { full: mobile })
  // 扫参确认窗：写明提交那一刻的批次
  await page.getByRole('button', { name: '提交扫参' }).click()
  await expect(page.getByRole('dialog', { name: /这次约花 2.3 单元/ })).toBeVisible()
  await shot('07b-sweep-confirm')
  await page.getByRole('button', { name: '取消', exact: true }).click()
  // 报价失败：不能提交，可以「重新估算」
  planDown = true
  await page.locator('.cpick', { hasText: 'v2ProPlus' }).click()
  await expect(page.locator('.auto-c')).toContainText('暂时估算不了')
  await expect(page.getByRole('button', { name: '提交扫参' })).toBeDisabled()
  await shot('07c-plan-failed', { full: mobile })
  planDown = false

  // ── 模型：默认 / 评测组合
  await nav('models')
  await expect(page.locator('.pair-panel .auto-c')).toContainText('自动：在 studio-host 上评测')
  await shot('08-models', { full: true })

  // ── 盲听：新建
  await nav('blind')
  await page.getByRole('button', { name: '＋ 新建盲听' }).click()
  await expect(page.locator('.auto-c')).toContainText('自动：在 studio-host 上出题')
  await shot('09-blind-new', { full: true })

  // ── 设置四个标签
  await nav('settings/general')
  await expect(page.getByText('这台 Studio')).toBeVisible()
  await shot('10-settings-general', { full: mobile })
  await nav('settings/library')
  await expect(page.getByText('我的云端硬盘 › ai › tts-library')).toBeVisible()
  await shot('11-settings-library', { full: mobile })
  await nav('settings/compute')
  await page.getByRole('button', { name: '展开studio-host' }).click()
  await expect(page.locator('.speed-table').first()).toBeVisible()
  await shot('12-settings-compute', { full: true })
  await page.getByRole('button', { name: '展开studio-host' }).or(page.getByRole('button', { name: '收起studio-host' })).click()
  await page.getByRole('button', { name: 'gpu-worker的操作' }).click()
  await expect(page.getByRole('menu')).toBeVisible()
  await shot('13-settings-compute-menu')
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: '怎么选的' }).click()
  await shot('14-settings-compute-auto-tip', { full: mobile })
  await nav('settings/cloud')
  await expect(page.getByText('账号池能开的卡')).toBeVisible()
  await shot('15-settings-cloud-gpu', { full: true })
  await page.getByRole('radio', { name: '按账号' }).click()
  await expect(page.locator('.recharts-bar-rectangle').first()).toBeVisible()
  await shot('16-settings-cloud-accounts', { full: true })

  // ── 添加算力：选类型；云端 GPU 三步
  await nav('settings/compute')
  await page.getByRole('button', { name: '＋ 添加算力' }).click()
  await expect(page.getByText('要添加哪一种？')).toBeVisible()
  await shot('17-add-type')
  // 局域网 / 远程四步向导的截图（18a–18f）由 e2e/screens-lan.mjs 生成
  await page.getByRole('button', { name: /云端 GPU（Colab）/ }).click()
  await page.getByRole('button', { name: /用 Google 账号登录/ }).click()
  await expect(page.getByLabel('授权码')).toBeVisible()
  await shot('19-add-cloud-login')
  await page.getByLabel('授权码').fill('4/mock-code')
  await page.getByRole('button', { name: '继续', exact: true }).click()
  await expect(page.getByText('已登录：')).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: '下一步' }).click()
  await expect(page.getByText('能开', { exact: true })).toBeVisible()
  await shot('20-add-cloud-scan')
  await page.getByRole('button', { name: '下一步' }).click()
  await expect(page.getByText('已加入账号池')).toBeVisible()
  await shot('21-add-cloud-done')
  await page.getByRole('button', { name: '完成', exact: true }).click()

  // ── 主控不在线
  const offline = await browser.newPage({ viewport, deviceScaleFactor: scale, isMobile: mobile, hasTouch: mobile })
  await mockCore(offline, { overrides: { 'GET /health': () => 'abort' } })
  await offline.goto(base)
  await expect(offline.locator('.empty-title', { hasText: '主控不在线' })).toBeVisible()
  await offline.screenshot({ path: join(out, `${kind}-22-offline.png`) })
  shots.push(`${kind}-22-offline.png`)
  await offline.close()

  if (errors.length || core.unexpected.length) console.log(kind, 'errors', errors, 'unexpected', core.unexpected)
  console.log(kind, 'done', loginPolls, 'login polls')
  await page.close()
}
await browser.close()
console.log(shots.length, 'screenshots in', out)
