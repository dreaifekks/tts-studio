// 「添加算力 → 局域网或远程电脑」四步向导的截图（桌面 1280 宽 1x + 手机 390 宽 2x）。
// 全部接口用假数据（mock-core + 这里的邀请接口），不碰真实服务。用法：node e2e/screens-lan.mjs [base] [outDir]
import { chromium, expect } from '@playwright/test'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { ENGINES, mockCore } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const out = process.argv[3] ?? mkdtempSync(join(tmpdir(), 'tts-studio-screens-'))
mkdirSync(out, { recursive: true })

const CODE = '7Q4-K9M'
const device = {
  id: 'lan-worker-host', name: 'worker-host', type: 'lan', status: 'idle', activity: null, machine: 'worker-host', address: null, engines: ['v2pro', 'v2proplus', 'v4'],
  loaded: [], wakeable: false, premium: false, last_seen: '2026-11-20T10:05:00+09:00',
  hw: { cpu: 'Intel N100', cores: 4, ram_gb: 16, accel: 'cpu', gpu: null, os: 'linux', torch: '2.5' },
  speeds: Object.fromEntries(ENGINES.map((e) => [e, { sec_per_item: null, source: 'untested' }])),
}

const browser = await chromium.launch()
const shots = []
for (const [kind, viewport, scale] of [['desktop', { width: 1280, height: 800 }, 1], ['mobile', { width: 390, height: 844 }, 2]]) {
  const mobile = kind === 'mobile'
  const page = await browser.newPage({ viewport, deviceScaleFactor: scale, isMobile: mobile, hasTouch: mobile })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  // 邀请：waiting（剩余时间按 expires 算）→ installing（waiting + 已核销）→ connected
  const invite = { state: 'waiting', expires: 0 }
  const status = () => ({
    id: 'inv-1', expires_at: new Date(invite.expires).toISOString(), device: invite.state === 'connected' ? device : null,
    state: invite.state === 'connected' ? 'connected' : 'waiting',
    expires_in: invite.state === 'waiting' ? Math.max(1, Math.ceil((invite.expires - Date.now()) / 1000)) : 0,
    redeemed: invite.state !== 'waiting',
  })
  const core = await mockCore(page, {
    overrides: {
      'POST /compute/invites': () => {
        invite.expires = Date.now() + 600_000
        return { body: { id: 'inv-1', code: CODE, network: 'lan', host: 'studio-host.local', expires_at: new Date(invite.expires).toISOString(), expires_in: 600,
          commands: { unix: `curl -fsSL http://studio-host.local/install | sh -s -- --pair ${CODE}`, windows: null } } }
      },
      'GET /compute/invites/inv-1': () => ({ body: status() }),
      'POST /compute/invites/inv-1/manual': () => ({ status: 400, body: { detail: '连不上 http://192.0.2.10:9880：ConnectTimeout' } }),
      'DELETE /compute/invites/inv-1': () => ({ status: 204 }),
    },
  })
  core.world.systemVersions.studio.latest = '0.0.1'   // 截图里不出现「有新版本」红点

  const dialog = page.getByRole('dialog', { name: '添加算力' })
  const stepIs = (n) => expect(dialog.locator('.wstep')).toHaveText(`局域网 / 远程 · 第 ${n} 步，共 4 步`)
  async function shot(name) {
    await page.waitForTimeout(350)
    const file = `${kind}-${name}.png`
    await page.screenshot({ path: join(out, file) })
    shots.push(file)
  }

  await page.goto(base + 'settings/compute')
  await expect(page.locator('.dev b', { hasText: 'gpu-worker' })).toBeVisible()
  await page.getByRole('button', { name: '＋ 添加算力' }).click()
  await page.getByRole('button', { name: /局域网或远程电脑/ }).click()
  await stepIs(1)
  await shot('18a-lan-network')

  await dialog.getByRole('button', { name: '下一步' }).click()
  await stepIs(2)
  await expect(dialog).toContainText('配对码还剩约 10 分钟')
  await shot('18b-lan-command')

  invite.expires = Date.now() + 475_000
  await dialog.getByRole('button', { name: '我已经运行了' }).click()
  await stepIs(3)
  await expect(dialog).toContainText('配对码还剩约 8 分钟')
  await shot('18c-lan-waiting')

  const manual = dialog.locator('summary', { hasText: '没有自动连上？手动填' })
  await manual.click()
  await dialog.getByLabel('连接地址').fill('http://192.0.2.10:9880')
  await dialog.getByRole('button', { name: '连接', exact: true }).click()
  await expect(dialog.getByRole('alert')).toHaveText('连接失败，请检查连接地址后重试。')
  await shot('18e-lan-manual')
  await manual.click()

  invite.state = 'installing'
  await expect(dialog.getByRole('status')).toContainText('已连上主控，正在安装…')
  await shot('18d-lan-installing')

  invite.state = 'connected'
  core.world.compute.devices.push(device)
  await stepIs(4)
  await expect(dialog.getByLabel('名称')).toHaveValue('worker-host')
  await shot('18f-lan-connected')

  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await expect(dialog).toHaveCount(0)
  if (errors.length || core.unexpected.length) console.log(kind, 'errors', errors, 'unexpected', core.unexpected)
  console.log(kind, 'done')
  await page.close()
}
await browser.close()
console.log(shots.length, 'screenshots in', out)
