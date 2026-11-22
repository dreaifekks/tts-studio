// 「添加算力 → 局域网或远程电脑」四步向导：选网络、生成邀请（Tailscale 不可用时回到第 1 步）、命令和复制、倒计时、
// 轮询（等待 → 正在安装 → 已连上，自动跳步；正在安装看 redeemed、只跳一次；不重叠；迟到的结果不推进界面）、手动填的三种失败、改名和快速测速、关闭时作废邀请。
// 全部接口用假数据：mock-core，加上这里的邀请接口（overrides）。桌面 1280 和手机 390 各跑一遍。用法：node e2e/lan-wizard.mjs [base]
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { ENGINES, job, mockCore } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const IDS = ['inv-1', 'inv-2', 'inv-3', 'inv-4', 'inv-5', 'inv-6', 'inv-7', 'inv-8']
const CODES = ['7Q4-K9M', 'R2D-7XK', 'M3P-Q8Z', 'K5W-2HT', 'T9C-4VB', 'H6N-8JD', 'P2L-5RS', 'Z8E-3MA']
const command = (code) => `curl -fsSL http://studio-host.local/install | sh -s -- --pair ${code}`
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const lanDevice = (id, name) => ({
  id, name, type: 'lan', status: 'idle', activity: null, machine: name, address: null, engines: ['v2pro', 'v2proplus', 'v4'], loaded: [], wakeable: false,
  premium: false, last_seen: '2026-11-20T10:05:00+09:00', hw: { cpu: 'Intel N100', cores: 4, ram_gb: 16, accel: 'cpu', gpu: null, os: 'linux', torch: '2.5' },
  speeds: Object.fromEntries(ENGINES.map((e) => [e, { sec_per_item: null, source: 'untested' }])),
})

/**
 * 假的邀请接口，行为照 core：再生成一个时没核销的旧码作废；查不到 = 200 expired；没核销时剩余秒数向上取整、至少 1；
 * 核销后到第一次心跳前 waiting + redeemed（0 秒）；st.once[id] 是这个邀请下一次查询原样回的状态（只用一次）；
 * DELETE 永远 204、不删设备；手动填按 st.manual 里排好的结果应答（400 / used / expired / ok）。
 * st.delay[id] 是这个邀请接下来几次查询要拖多久（毫秒）；查询结果在拖完之后才算。
 */
function inviteApi(getWorld) {
  const st = { tailscale: false, n: 0, invites: {}, gets: [], deleted: [], manual: [], delay: {}, once: {}, nextExpiresIn: 600 }
  const view = (id) => {
    const inv = st.invites[id]
    if (!inv) return { id, state: 'expired', expires_at: null, expires_in: 0, redeemed: false, device: null }
    const left = (inv.expires - Date.now()) / 1000
    if (inv.state === 'waiting' && left <= 0) inv.state = 'expired'
    const common = { id, expires_at: new Date(inv.expires).toISOString(), expires_in: 0, redeemed: false, device: null }
    const once = st.once[id]
    if (once) { delete st.once[id]; return { ...common, ...once } }
    if (inv.state === 'installing') return { ...common, state: 'waiting', redeemed: true }
    if (inv.state === 'connected') return { ...common, state: 'connected', redeemed: true, device: inv.device }
    if (inv.state !== 'waiting') return { ...common, state: 'expired' }
    return { ...common, state: 'waiting', expires_in: Math.max(1, Math.ceil(left)) }
  }
  const connect = (id, device) => {
    Object.assign(st.invites[id], { state: 'connected', device })
    const devices = getWorld().compute.devices
    if (!devices.some((d) => d.id === device.id)) devices.push(device)
  }
  const overrides = {
    'POST /compute/invites': (req) => {
      const { network } = req.postDataJSON()
      if (network === 'tailscale' && !st.tailscale) return { status: 409, body: { detail: '主控没连 Tailscale：没有找到 tailscale', code: 'tailscale_unavailable' } }
      for (const inv of Object.values(st.invites)) if (inv.state === 'waiting') inv.state = 'expired'
      const id = IDS[st.n], code = CODES[st.n]
      st.n += 1
      const expires = Date.now() + st.nextExpiresIn * 1000
      st.invites[id] = { id, code, network, state: 'waiting', expires, device: null }
      return { body: { id, code, network, host: 'studio-host.local', expires_at: new Date(expires).toISOString(), expires_in: st.nextExpiresIn, commands: { unix: command(code), windows: null } } }
    },
  }
  for (const id of IDS) {
    overrides[`GET /compute/invites/${id}`] = async () => {
      const entry = { id, start: Date.now(), end: null }
      st.gets.push(entry)
      const delay = st.delay[id]?.shift()
      if (delay) await sleep(delay)
      entry.end = Date.now()
      return { body: view(id) }
    }
    overrides[`DELETE /compute/invites/${id}`] = () => {
      st.deleted.push(id)
      if (st.invites[id] && st.invites[id].state !== 'connected') st.invites[id].state = 'cancelled'
      return { status: 204 }
    }
    overrides[`POST /compute/invites/${id}/manual`] = () => {
      const mode = st.manual.shift() ?? '400'
      if (mode === '400') return { status: 400, body: { detail: '连不上 http://192.0.2.10:9880：ConnectTimeout: timed out' } }
      if (mode === 'used') return { status: 409, body: { detail: '这个配对码已经用过了', code: 'invite_used' } }
      if (mode === 'expired') {
        if (st.invites[id]) st.invites[id].state = 'expired'
        return { status: 409, body: { detail: '配对码过期了：重新生成', code: 'invite_expired' } }
      }
      connect(id, lanDevice('lan-manual', 'manual-pc'))
      return { body: view(id) }
    }
  }
  return {
    st, overrides, connect,
    redeem: (id) => { st.invites[id].state = 'installing' },
    setExpiresIn: (id, seconds) => { st.invites[id].expires = Date.now() + seconds * 1000 },
  }
}

const browser = await chromium.launch()
try {
  for (const [kind, viewport] of [['desktop', { width: 1280, height: 800 }], ['mobile', { width: 390, height: 844 }]]) {
    const mobile = kind === 'mobile'
    const page = await browser.newPage({ viewport, isMobile: mobile, hasTouch: mobile, permissions: ['clipboard-read', 'clipboard-write'] })
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    // 记下复制了什么（真正的剪贴板照常写）
    await page.addInitScript(() => {
      window.__copied = []
      const write = navigator.clipboard?.writeText.bind(navigator.clipboard)
      if (write) navigator.clipboard.writeText = (text) => { window.__copied.push(text); return write(text) }
    })
    let core
    const api = inviteApi(() => core.world)
    core = await mockCore(page, {
      overrides: {
        ...api.overrides,
        'PATCH /compute/devices/lan-new': (req) => {
          const device = core.world.compute.devices.find((d) => d.id === 'lan-new')
          Object.assign(device, req.postDataJSON())
          return { body: device }
        },
        'POST /bench': () => {
          core.world.jobs.push(job('j-bench', 'bench', 'succeeded', { target: 'lan-new', stage: '' }))
          return { body: job('j-bench', 'bench', 'queued', { target: 'lan-new', progress: 0, stage: '等待' }) }
        },
      },
    })
    const dialog = page.getByRole('dialog', { name: '添加算力' })
    const stepIs = (n) => expect(dialog.locator('.wstep')).toHaveText(`局域网 / 远程 · 第 ${n} 步，共 4 步`)
    const button = (name) => dialog.getByRole('button', { name, exact: true })
    const posts = (key) => core.posts.filter((p) => p.key === key).map((p) => p.body)
    const gets = (id) => api.st.gets.filter((g) => g.id === id)
    const summary = () => dialog.locator('summary', { hasText: '没有自动连上？手动填' })
    const open = async () => {
      await page.getByRole('button', { name: '＋ 添加算力' }).click()
      await page.getByRole('button', { name: /局域网或远程电脑/ }).click()
      await stepIs(1)
    }
    /** 手机 390 宽：页面和弹窗都不能横向溢出。 */
    const noOverflow = async (where) => {
      const o = await page.evaluate(() => {
        const over = (el) => (el ? el.scrollWidth - el.clientWidth : 0)
        const box = document.querySelector('[role="dialog"]')?.getBoundingClientRect()
        return { page: document.documentElement.scrollWidth - innerWidth, body: over(document.querySelector('.modal-body')), foot: over(document.querySelector('.modal-foot')), right: box ? box.right - innerWidth : 0 }
      })
      assert.ok(o.page <= 0 && o.body <= 0 && o.foot <= 0 && o.right <= 0.5, `${kind} ${where}：横向溢出 ${JSON.stringify(o)}`)
    }
    const step = async (name, fn) => { await fn(); console.log(`${kind}: PASS ${name}`) }

    await page.goto(base + 'settings/compute')
    await expect(page.locator('.dev b', { hasText: 'gpu-worker' })).toBeVisible()

    await step('第 1 步：单选卡片方向键可选；选 Tailscale 但主控没连上时留在第 1 步并提示', async () => {
      await open()
      const lan = dialog.getByRole('radio', { name: /家里的局域网/ })
      const tailscale = dialog.getByRole('radio', { name: /^Tailscale/ })
      await expect(lan).toHaveAttribute('aria-checked', 'true')
      await expect(lan).toBeFocused()
      await page.keyboard.press('ArrowDown')
      await expect(tailscale).toHaveAttribute('aria-checked', 'true')
      await expect(tailscale).toBeFocused()
      await noOverflow('第 1 步')
      await button('下一步').click()
      await expect(dialog.getByRole('alert')).toHaveText('这台主控还没连上 Tailscale，请连接后重试，或选择家里的局域网。')
      await stepIs(1)
      assert.deepEqual(posts('POST /compute/invites'), [{ network: 'tailscale' }])
      assert.equal(gets('inv-1').length, 0)
    })

    await step('第 2 步：局域网邀请的请求体、命令、说明、倒计时；第一个控件得到焦点', async () => {
      await dialog.getByRole('radio', { name: /家里的局域网/ }).click()
      await button('下一步').click()
      await stepIs(2)
      assert.deepEqual(posts('POST /compute/invites'), [{ network: 'tailscale' }, { network: 'lan' }])
      await expect(dialog.locator('.cmd code')).toHaveText(command('7Q4-K9M'))
      await expect(dialog.getByRole('radio', { name: 'macOS / Linux' })).toBeFocused()
      await expect(dialog).toContainText('在要添加的那台电脑上打开终端，粘贴下面这一行运行。')
      await expect(dialog).toContainText('装好需要的程序（约 7GB，5–10 分钟）')
      await expect(dialog).toContainText('装完自动连回这台 Studio，这里会自己跳到下一步')
      await expect(dialog).toContainText('配对码 7Q4-K9M 只能用一次，10 分钟内有效')
      await expect(dialog).toContainText('配对码还剩约 10 分钟')
      await expect(dialog.getByText('这台主控还没连上 Tailscale', { exact: false })).toHaveCount(0)
      await noOverflow('第 2 步')
    })

    await step('复制：成功显示「已复制」，复制失败时提示手动复制', async () => {
      await button('复制').click()
      await expect(button('已复制')).toBeVisible()
      assert.equal(await page.evaluate(() => window.__copied.at(-1)), command('7Q4-K9M'))
      if (!mobile) assert.equal(await page.evaluate(() => navigator.clipboard.readText()), command('7Q4-K9M'))
      await page.evaluate(() => {
        navigator.clipboard.writeText = () => Promise.reject(new DOMException('denied', 'NotAllowedError'))
        document.execCommand = () => false
      })
      await button('已复制').click()
      await expect(dialog.getByRole('alert')).toHaveText('复制失败，请选中命令后手动复制。')
      await expect(button('复制')).toBeVisible()
    })

    await step('Windows 暂不支持：不显示命令，「我已经运行了」不能点；换回来提示清掉', async () => {
      await dialog.getByRole('radio', { name: 'Windows · 暂不支持' }).click()
      await expect(dialog.locator('.cmd')).toHaveCount(0)
      await expect(dialog).toContainText('Windows 暂不支持')
      await expect(button('我已经运行了')).toBeDisabled()
      await dialog.getByRole('radio', { name: 'macOS / Linux' }).click()
      await expect(dialog.locator('.cmd code')).toHaveText(command('7Q4-K9M'))
      await expect(dialog.getByText('复制失败，请选中命令后手动复制。')).toHaveCount(0)
      await expect(button('我已经运行了')).toBeEnabled()
    })

    await step('上一步再下一步：同网络、没过期的邀请直接复用，不再生成', async () => {
      await button('上一步').click()
      await stepIs(1)
      await expect(dialog.getByRole('radio', { name: /家里的局域网/ })).toBeFocused()
      await button('下一步').click()
      await stepIs(2)
      await expect(dialog.locator('.cmd code')).toHaveText(command('7Q4-K9M'))
      assert.equal(posts('POST /compute/invites').length, 2)
    })

    await step('倒计时跟着主控给的剩余时间走', async () => {
      api.setExpiresIn('inv-1', 75)
      await expect(dialog).toContainText('配对码还剩约 2 分钟')
      api.setExpiresIn('inv-1', 50)
      await expect(dialog).toContainText('配对码还剩约 1 分钟')
      api.setExpiresIn('inv-1', 540)
      await expect(dialog).toContainText('配对码还剩约 9 分钟')
    })

    await step('没核销就不算「正在安装」：主控报 waiting + 0 秒、redeemed 为 false 时不跳步', async () => {
      api.st.once['inv-1'] = { state: 'waiting', expires_in: 0, redeemed: false }
      await expect.poll(() => api.st.once['inv-1'], { timeout: 6000 }).toBeUndefined()
      await page.waitForTimeout(300)
      await stepIs(2)
      await expect(dialog).not.toContainText('已连上主控，正在安装…')
      await expect(dialog).toContainText('配对码还剩约 9 分钟', { timeout: 6000 })        // 下一次查询照常
    })

    await step('轮询：已核销（waiting + redeemed，正在安装）自动到第 3 步，不显示倒计时，可以重新生成', async () => {
      api.redeem('inv-1')
      await stepIs(3)
      await expect(dialog.getByRole('status')).toContainText('已连上主控，正在安装…')
      await expect(dialog).toContainText('安装完成后会自动显示设备信息。')
      await expect(dialog).not.toContainText('配对码还剩约')
      await expect(dialog).not.toContainText('配对码已过期')
      await expect(button('重新生成配对码')).toBeEnabled()
      await expect(button('取消')).toBeEnabled()
      await expect(summary()).toBeFocused()
      await summary().click()
      await dialog.getByLabel('连接地址').fill('http://192.0.2.10:9880')
      await expect(button('连接')).toBeDisabled()
      await noOverflow('第 3 步（正在安装）')
    })

    await step('正在安装时回到前面几步看命令：之后的查询不再把步骤拽回第 3 步', async () => {
      await button('上一步').click()
      await stepIs(2)
      const polled = gets('inv-1').length
      await expect.poll(() => gets('inv-1').length, { timeout: 7000 }).toBeGreaterThanOrEqual(polled + 2)
      await stepIs(2)
      await button('上一步').click()
      await stepIs(1)
      const again = gets('inv-1').length
      await expect.poll(() => gets('inv-1').length, { timeout: 7000 }).toBeGreaterThanOrEqual(again + 2)
      await stepIs(1)
      await button('下一步').click()                                                     // 同网络、正在安装的邀请照样复用
      await stepIs(2)
      assert.equal(posts('POST /compute/invites').length, 2)
    })

    await step('轮询：connected 自动到第 4 步，显示识别出的信息，之后不再查询', async () => {
      api.connect('inv-1', lanDevice('lan-new', 'worker-host'))
      await stepIs(4)
      await expect(dialog.locator('.ok-line')).toContainText('已连上')
      await expect(dialog.locator('.ok-line')).toContainText('自动识别出这些信息，名称可以改')
      const name = dialog.getByLabel('名称')
      await expect(name).toHaveValue('worker-host')
      await expect(name).toBeFocused()
      await expect(dialog.locator('.fl2')).toContainText('Linux')
      await expect(dialog.locator('.fl2')).toContainText('CPU · 4 核')
      await expect(dialog.locator('.fl2')).toContainText('v2Pro · v2ProPlus · v4')
      await expect(dialog.getByRole('radio', { name: /现在做快速测速（约 2 分钟）/ })).toHaveAttribute('aria-checked', 'true')
      await expect(dialog.getByRole('radio', { name: /以后再测，先用参考值/ })).toHaveAttribute('aria-checked', 'false')
      await expect(page.locator('.dev b', { hasText: 'worker-host' })).toHaveCount(1)
      await noOverflow('第 4 步')
      const polled = gets('inv-1').length
      await page.waitForTimeout(2600)
      assert.equal(gets('inv-1').length, polled, '连上后不再查询')
      // 查询从不重叠：每次都在上一次回来 2 秒后才发
      const list = gets('inv-1')
      for (let i = 1; i < list.length; i++) assert.ok(list[i].start - list[i - 1].end >= 1800, `第 ${i + 1} 次查询离上一次太近：${list[i].start - list[i - 1].end}ms`)
    })

    await step('完成：改名 PATCH、快速测速 POST /bench 的请求体，测速任务记成本机发起；关闭时作废邀请', async () => {
      await dialog.getByLabel('名称').fill('书房 N100')
      await button('完成').click()
      await expect(dialog).toHaveCount(0)
      assert.deepEqual(posts('PATCH /compute/devices/lan-new'), [{ name: '书房 N100' }])
      assert.deepEqual(posts('POST /bench'), [{ device: 'lan-new', quick: true }])
      await expect.poll(() => api.st.deleted).toEqual(['inv-1'])
      assert.ok((await page.evaluate(() => JSON.parse(localStorage.getItem('localJobIds') ?? '[]'))).includes('j-bench'))
      await expect(page.locator('.dev b', { hasText: '书房 N100' })).toBeVisible()
      await expect(page.getByRole('button', { name: '＋ 添加算力' })).toBeFocused()
    })

    await step('手动填：400 连不上那台（邀请还能用）、invite_used（接着看结果）→ 已连上；以后再测时不改名不测速', async () => {
      await open()
      await button('下一步').click()
      await stepIs(2)
      await expect(dialog.locator('.cmd code')).toHaveText(command('R2D-7XK'))
      await button('我已经运行了').click()
      await stepIs(3)
      await expect(dialog.getByRole('status')).toContainText('正在等待设备连接…')
      await expect(dialog).toContainText('那台电脑装完会自动连上。')
      await expect(dialog).toContainText('配对码还剩约 10 分钟')
      await expect(summary()).toBeFocused()
      await page.keyboard.press('Enter')
      const address = dialog.getByLabel('连接地址')
      await expect(address).toBeVisible()
      await expect(button('连接')).toBeDisabled()
      await address.fill('  http://192.0.2.10:9880 ')
      api.st.manual.push('400')
      await button('连接').click()
      await expect(dialog.getByRole('alert')).toHaveText('连接失败，请检查连接地址后重试。')
      assert.deepEqual(posts('POST /compute/invites/inv-2/manual'), [{ address: 'http://192.0.2.10:9880' }])
      await expect(address).toHaveValue('  http://192.0.2.10:9880 ')
      await expect(dialog.getByRole('status')).toContainText('正在等待设备连接…')
      await noOverflow('第 3 步（手动填）')
      api.st.manual.push('used')
      await address.press('Enter')
      await expect(dialog.getByRole('alert')).toHaveText('配对码已使用，正在查看连接结果…')
      assert.equal(posts('POST /compute/invites/inv-2/manual').length, 2)
      api.connect('inv-2', lanDevice('lan-study', 'study-pc'))
      await stepIs(4)
      await expect(dialog.getByLabel('名称')).toHaveValue('study-pc')
      await dialog.getByRole('radio', { name: /以后再测/ }).click()
      await button('完成').click()
      await expect(dialog).toHaveCount(0)
      assert.equal(posts('POST /bench').length, 1, '以后再测时不提交测速')
      await expect.poll(() => api.st.deleted).toEqual(['inv-1', 'inv-2'])
    })

    await step('手动填 invite_expired：显示已过期、停止查询；重新生成后回到第 2 步', async () => {
      await open()
      await button('下一步').click()
      await button('我已经运行了').click()
      await stepIs(3)
      await summary().click()
      await dialog.getByLabel('连接地址').fill('http://192.0.2.10:9880')
      api.st.manual.push('expired')
      await button('连接').click()
      await expect(dialog.getByRole('alert')).toHaveText('配对码已过期，请重新生成。')
      await expect(dialog.getByRole('status')).toHaveCount(0)
      await expect(button('连接')).toBeDisabled()
      const polled = gets('inv-3').length
      await page.waitForTimeout(2600)
      assert.equal(gets('inv-3').length, polled, '过期后不再查询')
      // 下一个配对码 4 秒就到期，它的第一次查询拖 8 秒才回来
      api.st.nextExpiresIn = 4
      api.st.delay['inv-4'] = [8000]
      await button('重新生成配对码').click()
      await stepIs(2)
      await expect(dialog.locator('.cmd code')).toHaveText(command('K5W-2HT'))
      await expect(dialog.getByRole('radio', { name: 'macOS / Linux' })).toBeFocused()
      assert.deepEqual(posts('POST /compute/invites').at(-1), { network: 'lan' })
    })

    await step('轮询不重叠：第一个查询还没回来时不发第二个；本地倒计时到期后显示过期', async () => {
      await expect(dialog).toContainText('配对码还剩约 1 分钟')
      await expect(dialog.getByRole('alert')).toHaveText('配对码已过期，请重新生成。', { timeout: 7000 })
      await expect(button('我已经运行了')).toBeDisabled()
      await expect(button('重新生成配对码')).toBeEnabled()
      assert.equal(gets('inv-4').length, 1, '第一个查询还没回来，不发第二个')
      assert.equal(gets('inv-4')[0].end, null)
    })

    await step('迟到的结果不推进界面：慢查询回来说已连上时，已经换了新的配对码', async () => {
      api.connect('inv-4', lanDevice('lan-late', 'late-pc'))
      api.st.nextExpiresIn = 600
      await button('重新生成配对码').click()
      await stepIs(2)
      await expect(dialog.locator('.cmd code')).toHaveText(command('T9C-4VB'))
      await expect.poll(() => gets('inv-4')[0].end, { timeout: 10_000 }).not.toBeNull()
      await page.waitForTimeout(800)
      await stepIs(2)
      await expect(dialog.locator('.cmd code')).toHaveText(command('T9C-4VB'))
      await expect(dialog).toContainText('配对码还剩约 10 分钟')
      assert.equal(gets('inv-4').length, 1)
    })

    await step('Esc 关闭：作废当前邀请', async () => {
      await page.keyboard.press('Escape')
      await expect(dialog).toHaveCount(0)
      await expect.poll(() => api.st.deleted).toEqual(['inv-1', 'inv-2', 'inv-5'])
    })

    await step('取消、以及从第 1 步回到选类型，都作废邀请', async () => {
      await open()
      await button('下一步').click()
      await stepIs(2)
      await button('取消').click()
      await expect(dialog).toHaveCount(0)
      await expect.poll(() => api.st.deleted.at(-1)).toBe('inv-6')
      await open()
      await button('下一步').click()
      await stepIs(2)
      await button('上一步').click()
      await stepIs(1)
      await button('上一步').click()
      await expect(dialog.getByText('要添加哪一种？')).toBeVisible()
      await expect(dialog.getByRole('button', { name: /局域网或远程电脑/ })).toBeFocused()
      await expect.poll(() => api.st.deleted.at(-1)).toBe('inv-7')
      const before = gets('inv-7').length
      await page.waitForTimeout(2600)
      assert.equal(gets('inv-7').length, before, '回到选类型后不再查询')
      await button('取消').click()
      await expect(dialog).toHaveCount(0)
    })

    assert.deepEqual(errors, [])
    assert.deepEqual(core.unexpected, [])
    console.log(`${kind}: ${api.st.gets.length} invite polls, ${core.posts.length} writes, no page errors, no unexpected requests`)
    await page.close()
  }
} finally {
  await browser.close()
}
