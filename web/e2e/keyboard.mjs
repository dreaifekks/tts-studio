// 键盘可达（design 第 15 节）：自绘的单选、标签页、列表、菜单、确认窗只用键盘也能操作。全部接口用假数据（mock-core）。
// 用法：node e2e/keyboard.mjs [base]
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { candidate, job, mockCore, planFor, weight } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
const core = await mockCore(page, {
  overrides: {
    'POST /datasets/ds-alice/preview': () => ({ body: { id: 'ds-alice', lines_total: 800, lines_kept: 800, minutes_kept: 65, excluded_short: 0, excluded_long: 0, excluded_interjection: 0, interjection_examples: [] } }),
    'POST /train': (req) => {
      if (!req.postDataJSON()?.approved) return { status: 409, body: { detail: '需要确认', code: 'confirm_required', plan: planFor({ kind: 'train' }, core.world) } }
      const started = job('j-train', 'train', 'running', { compute: { device: 'colab-T4', name: '云端 T4', type: 'cloud', reason_code: 'train_needs_gpu', reason: '' } })
      core.world.jobs.push(started)
      return { body: started }
    },
    'POST /tts/candidates/jobs': () => ({ body: job('j-gen', 'candidates', 'succeeded', { candidates_result: { candidates: [candidate('a1', 94.6, 'おはよう'), candidate('a2', 94.5, 'おはよう、')] } }) }),
  },
})
const focused = () => page.evaluate(() => {
  const el = document.activeElement
  return `${el?.getAttribute('role') ?? el?.tagName.toLowerCase()}:${(el?.textContent ?? '').trim().slice(0, 24)}`
})
const results = []
const step = async (name, fn) => { await fn(); results.push(name); console.log('PASS', name) }

try {
  await page.goto(base)
  await expect(page.locator('.auto-c')).toContainText('自动：在 studio-host 上生成')

  await step('分段单选：方向键切换「合成模式」', async () => {
    await page.getByRole('radio', { name: '出候选' }).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page.getByRole('radio', { name: '对比引擎' })).toHaveAttribute('aria-checked', 'true')
    assert.match(await focused(), /^radio:对比引擎/)
    await page.keyboard.press('Home')
    await expect(page.getByRole('radio', { name: '出候选' })).toHaveAttribute('aria-checked', 'true')
    await expect(page.locator('.auto-c')).toContainText('自动：在 studio-host 上生成')
  })

  await step('「更改」：回车打开列表，方向键移动，回车选择，Esc 关闭', async () => {
    await page.getByRole('button', { name: '更改' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('listbox')).toBeVisible()
    assert.match(await focused(), /^option:自动选择/)
    await page.keyboard.press('ArrowDown')
    await page.keyboard.press('ArrowDown')
    assert.match(await focused(), /^option:gpu-worker/)
    await page.keyboard.press('Enter')
    await expect(page.locator('.auto-c')).toContainText('这次：在 gpu-worker 上生成')
    await page.getByRole('button', { name: '更改' }).focus()
    await page.keyboard.press('ArrowDown')
    await expect(page.getByRole('listbox')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('listbox')).toHaveCount(0)
    await page.getByRole('button', { name: '恢复自动' }).press('Enter')
    await expect(page.locator('.auto-c')).toContainText('自动：在 studio-host 上生成')
  })

  await step('候选卡片 ⋯ 菜单：回车打开（生成编号只读、生成详情可选），Esc 关闭并回到按钮', async () => {
    await page.getByRole('button', { name: '♫ 出候选' }).press('Enter')
    await expect(page.locator('.candidate')).toHaveCount(2)
    const more = page.getByRole('button', { name: '候选 a1 的更多操作' })
    await more.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('menu')).toContainText('生成编号：')
    assert.match(await focused(), /^menuitem:生成详情/)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('menu')).toHaveCount(0)
    await expect(more).toBeFocused()
  })

  await step('设置标签页：方向键切换并改地址', async () => {
    await page.goto(base + 'settings/general')
    await page.getByRole('tab', { name: /通用/ }).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page).toHaveURL(/\/ui\/settings\/library$/)
    assert.match(await focused(), /^tab:.*资料库/)
    await page.keyboard.press('End')
    await expect(page).toHaveURL(/\/ui\/settings\/cloud$/)
  })

  await step('设备 ⋯ 菜单：回车打开、方向键移动、Esc 回到按钮', async () => {
    await page.goto(base + 'settings/compute')
    const button = page.getByRole('button', { name: 'gpu-worker的操作' })
    await button.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('menu')).toBeVisible()
    assert.match(await focused(), /^menuitem:改名 \/ 编辑…/)
    await page.keyboard.press('ArrowDown')
    assert.match(await focused(), /^menuitem:重新配对…/)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('menu')).toHaveCount(0)
    await expect(button).toBeFocused()
  })

  await step('展开设备行：回车切换 aria-expanded', async () => {
    const exp = page.getByRole('button', { name: '展开studio-host' })
    await exp.focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('button', { name: '收起studio-host' })).toHaveAttribute('aria-expanded', 'true')
  })

  await step('添加算力：类型卡片可 Tab、弹窗里 Tab 循环、Esc 关闭', async () => {
    await page.getByRole('button', { name: '＋ 添加算力' }).press('Enter')
    const dialog = page.getByRole('dialog', { name: '添加算力' })
    await expect(dialog).toBeVisible()
    for (let i = 0; i < 8; i++) {
      await page.keyboard.press('Tab')
      assert.ok(await dialog.evaluate((d) => d.contains(document.activeElement)), '焦点留在弹窗里')
    }
    await page.getByRole('button', { name: /局域网或远程电脑/ }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('radio', { name: /家里的局域网/ })).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
  })

  await step('确认窗：方向键换设备、按钮跟着变、Esc 取消；确认后带 approved_device / approved_units 再提交', async () => {
    await page.goto(base + 'training')
    await expect(page.getByRole('radio', { name: '已有角色' })).toHaveAttribute('aria-checked', 'true')
    await expect(page.locator('.auto-c')).toContainText('自动：在 云端 L4 上训练')
    await page.getByRole('button', { name: '⚙ 提交训练' }).press('Enter')
    const sheet = page.getByRole('dialog', { name: /这次约花/ })
    await expect(sheet).toBeVisible()
    await expect(sheet.locator('p').first()).toContainText('（其中准备环境约 4 分钟）')   // core 给的 prep_sec 取整
    await expect(sheet.getByRole('radio', { name: /云端 L4/ })).toContainText('云端 GPU · 要先准备环境约 4 分钟')
    await expect(page.getByRole('button', { name: '用 L4 开始' })).toBeFocused()
    await page.getByRole('radio', { name: /云端 L4/ }).focus()
    await page.keyboard.press('ArrowDown')
    await expect(page.getByRole('button', { name: '用 T4 开始' })).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(sheet).toHaveCount(0)
    const trains = () => core.posts.filter((p) => p.key === 'POST /train').map((p) => p.body)
    assert.equal(trains().length, 1, '取消后没有再提交')
    await page.getByRole('button', { name: '⚙ 提交训练' }).press('Enter')
    await expect(sheet).toBeVisible()
    await page.getByRole('radio', { name: /云端 L4/ }).focus()
    await page.keyboard.press('ArrowDown')
    await page.getByRole('button', { name: '用 T4 开始' }).press('Enter')
    await expect(sheet).toHaveCount(0)
    await expect(page.locator('.card', { hasText: '运行中' }).getByText('· 云端 T4')).toBeVisible()
    const [, first, approved] = trains()
    assert.equal(trains().length, 3)
    assert.equal(first.approved, false)
    assert.equal(first.compute, undefined)
    assert.deepEqual({ compute: approved.compute, approved: approved.approved, approved_device: approved.approved_device, approved_units: approved.approved_units },
      { compute: 'colab-T4', approved: true, approved_device: 'colab-T4', approved_units: 2.3 })
  })

  await step('模型表：Tab 进表格，方向键选行，表头排序按钮可回车', async () => {
    core.world.weights = [weight('alice-v4-e15', 'gpt'), weight('alice-v4-e8', 'sovits')]
    await page.goto(base + 'models')
    await page.locator('.raw-weights-section > summary').click()
    const rows = page.locator('.weights-table tbody tr')
    await expect(rows).toHaveCount(2)
    await rows.first().focus()
    await page.keyboard.press('ArrowDown')
    await expect(rows.nth(1)).toBeFocused()
    await expect(rows.nth(1)).toHaveAttribute('aria-selected', 'true')
    await page.getByRole('button', { name: '显示名' }).press('Enter')
    await expect(page.locator('th', { hasText: '显示名' })).toHaveAttribute('aria-sort', /ascending|descending/)
  })

  assert.deepEqual(errors, [])
  assert.deepEqual(core.unexpected, [])
  console.log(`${results.length} keyboard checks PASS`)
} finally {
  await browser.close()
}
