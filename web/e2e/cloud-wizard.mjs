// Add Compute -> Cloud GPU wizard (3 steps):
// - Decides post-done deletion solely from core's `added` boolean (never from cached /cloud list); missing `added` never deletes.
// - Cancelling (Cancel / Esc / Back before done / 换一个 / closing before step 3) calls DELETE /cloud/accounts/login/{login_id},
//   including after submitting the auth code before GET reports done.
// All endpoints use mock-core. Usage: node e2e/cloud-wizard.mjs [base]
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { mockCore } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const account = (id, email) => ({ id, label: email.split('@')[0], email, balance: 85, month_used_estimate: 0, gpus: ['T4'], disabled: false, error: null })
const NEW = account('acct-new', 'studio.new@example.com')
const OTHER = account('acct-other', 'studio.other@example.com')

const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  await page.addInitScript(() => {
    window.__openedTabs = []
    window.open = () => {
      const tab = { location: { href: '' }, closed: false, close() { this.closed = true } }
      window.__openedTabs.push(tab)
      return tab
    }
  })
  let loginSeq = 0
  let currentLogin = null
  let next = NEW
  let nextAdded = true      // boolean or undefined (simulates older core omitting `added`)
  let holdWaiting = false   // keeps GET /cloud/accounts/login/{id} in waiting after code submission
  let deferLoginPost = null // when set, POST /cloud/accounts/login waits on this promise before returning
  let cloudDown = false
  const logins = new Map()  // login_id -> { account, added, committed }

  const makeLoginRoute = () => ({
    'GET /cloud': () => (cloudDown ? { status: 503, body: { detail: '主控暂时忙不过来' } } : { body: core.world.cloud }),
    'POST /cloud/accounts/login': async () => {
      const id = `L${++loginSeq}`
      currentLogin = id
      logins.set(id, { account: next, added: nextAdded, codeSent: false, committed: false })
      if (deferLoginPost) {
        const wait = deferLoginPost
        deferLoginPost = null
        await wait
      }
      return { body: { login_id: id, auth_url: 'https://accounts.example.com/mock', needs_code: true } }
    },
  })

  // Dynamic per-login_id handlers via page.route fallback? Register L1..L20 in overrides so mockCore tracks everything cleanly.
  const loginOverrides = {}
  for (let i = 1; i <= 20; i++) {
    const id = `L${i}`
    loginOverrides[`POST /cloud/accounts/login/${id}`] = () => {
      const rec = logins.get(id)
      rec.codeSent = true
      return { body: { state: 'waiting', account: null, added: false, error: null } }
    }
    loginOverrides[`GET /cloud/accounts/login/${id}`] = () => {
      const rec = logins.get(id)
      if (!rec.codeSent || holdWaiting) return { body: { state: 'waiting', account: null, added: false, error: null } }
      rec.committed = true
      const pool = core.world.cloud.accounts
      if (!pool.some((a) => a.id === rec.account.id)) pool.push({ ...rec.account })
      const body = { state: 'done', account: rec.account, error: null }
      if (rec.added !== undefined) body.added = rec.added
      return { body }
    }
    loginOverrides[`DELETE /cloud/accounts/login/${id}`] = () => {
      const rec = logins.get(id)
      if (rec?.committed && rec.added === true) {
        core.world.cloud.accounts = core.world.cloud.accounts.filter((a) => a.id !== rec.account.id)
      }
      return { status: 204 }
    }
  }

  const core = await mockCore(page, {
    overrides: {
      ...makeLoginRoute(),
      ...loginOverrides,
    },
  })
  const dialog = page.getByRole('dialog', { name: '添加算力' })
  const button = (name) => dialog.getByRole('button', { name, exact: true })
  const stepIs = (n) => expect(dialog.locator('.wstep')).toHaveText(`云端 GPU · 第 ${n} 步，共 3 步`)
  const loginDeletes = () => core.posts.filter((p) => p.key.startsWith('DELETE /cloud/accounts/login/')).map((p) => p.key.slice('DELETE /cloud/accounts/login/'.length))
  const accountDeletes = () => core.posts.filter((p) => p.key.startsWith('DELETE /cloud/accounts/') && !p.key.startsWith('DELETE /cloud/accounts/login/'))
  const pool = () => core.world.cloud.accounts.map((a) => a.id)
  const open = async () => {
    await page.getByRole('button', { name: '＋ 添加算力' }).click()
    await dialog.getByRole('button', { name: /云端 GPU（Colab）/ }).click()
    await stepIs(1)
  }
  const login = async (who, added) => {
    next = who
    nextAdded = added
    holdWaiting = false
    await dialog.getByRole('button', { name: /用 Google 账号登录/ }).click()
    await dialog.getByLabel('授权码').fill('mock-code')
    await button('继续').click()
    await expect(dialog).toContainText(`已登录：${who.email}`)
    return currentLogin
  }
  const step = async (name, fn) => { await fn(); console.log(`PASS ${name}`) }

  await page.goto(base + 'settings/compute')
  await expect(page.locator('.dev b', { hasText: 'gpu-worker' })).toBeVisible()
  const main = core.world.cloud.accounts.find((a) => a.id === 'acct-main')
  const expectedDeletes = []

  await step('新加的账号（added=true）「换一个」：调 DELETE /cloud/accounts/login/{id} 并移除', async () => {
    await open()
    const id = await login(NEW, true)
    assert.ok(pool().includes('acct-new'))
    await button('换一个').click()
    await expect(dialog.getByRole('button', { name: /用 Google 账号登录/ })).toBeVisible()
    expectedDeletes.push(id)
    await expect.poll(loginDeletes).toEqual(expectedDeletes)
    assert.ok(!pool().includes('acct-new'))
  })

  await step('重新登录已有账号（added=false）：「换一个」「取消」都不删', async () => {
    await login(main, false)
    await button('换一个').click()
    await login(main, false)
    await button('取消').click()
    await expect(dialog).toHaveCount(0)
    await page.waitForTimeout(300)
    assert.deepEqual(loginDeletes(), expectedDeletes)
    assert.ok(pool().includes('acct-main'))
  })

  await step('本地缓存陈旧（/cloud 刷新失败、缓存里缺该账号），但 core 返回 added=false：取消不删', async () => {
    // Reproduces review blocking #1: stale client cache does not have OTHER, yet core says added=false.
    cloudDown = true
    await page.goto(base + 'settings/compute')
    await expect(page.locator('.dev b', { hasText: 'gpu-worker' })).toBeVisible()
    await open()
    await login(OTHER, false)
    await button('取消').click()
    await expect(dialog).toHaveCount(0)
    await page.waitForTimeout(300)
    assert.deepEqual(loginDeletes(), expectedDeletes)
    cloudDown = false
    await page.goto(base + 'settings/compute')
    await expect(page.locator('.dev b', { hasText: 'gpu-worker' })).toBeVisible()
  })

  await step('提交授权码后、收到 done 前取消：调 DELETE /cloud/accounts/login/{id}', async () => {
    // Reproduces review blocking #2: cancelling after code submission while still waiting must cancel on core.
    await open()
    next = NEW
    nextAdded = true
    holdWaiting = true
    await dialog.getByRole('button', { name: /用 Google 账号登录/ }).click()
    const waitingId = currentLogin
    await dialog.getByLabel('授权码').fill('mock-code')
    await button('继续').click()
    await expect(dialog).toContainText('等待浏览器授权')
    await button('取消').click()
    await expect(dialog).toHaveCount(0)
    expectedDeletes.push(waitingId)
    await expect.poll(loginDeletes).toEqual(expectedDeletes)
    holdWaiting = false
  })

  await step('新加的账号在第 1 步「取消」、第 2 步 Esc、回到选类型转局域网再取消：都调 DELETE /cloud/accounts/login/{id}', async () => {
    await open()
    const id1 = await login(NEW, true)
    await button('取消').click()
    await expect(dialog).toHaveCount(0)
    expectedDeletes.push(id1)
    await expect.poll(loginDeletes).toEqual(expectedDeletes)

    await open()
    const id2 = await login(NEW, true)
    await button('下一步').click()
    await stepIs(2)
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    expectedDeletes.push(id2)
    await expect.poll(loginDeletes).toEqual(expectedDeletes)

    await open()
    const id3 = await login(NEW, true)
    assert.ok(pool().includes('acct-new'))
    await button('上一步').click()
    await expect(dialog).toContainText('要添加哪一种')
    expectedDeletes.push(id3)
    await expect.poll(loginDeletes).toEqual(expectedDeletes)
    assert.ok(!pool().includes('acct-new'))
    await dialog.getByRole('button', { name: /局域网或远程电脑/ }).click()
    await expect(dialog.locator('.wstep')).toHaveText('局域网 / 远程 · 第 1 步，共 4 步')
    await button('取消').click()
    await expect(dialog).toHaveCount(0)
    assert.deepEqual(loginDeletes(), expectedDeletes)

    // Existing account (added=false): clicking Back on step 1 clears selection without deleting the account.
    await open()
    await login(main, false)
    await button('上一步').click()
    await expect(dialog).toContainText('要添加哪一种')
    await page.waitForTimeout(300)
    assert.deepEqual(loginDeletes(), expectedDeletes)
    assert.ok(pool().includes('acct-main'))
    await dialog.getByRole('button', { name: /云端 GPU（Colab）/ }).click()
    await expect(dialog.getByRole('button', { name: /用 Google 账号登录/ })).toBeVisible()
    await button('取消').click()
    await expect(dialog).toHaveCount(0)
  })

  await step('走完三步「完成」：不删，账号留在池里', async () => {
    await open()
    await login(NEW, true)
    await button('下一步').click()
    await stepIs(2)
    await button('下一步').click()
    await stepIs(3)
    await button('完成').click()
    await expect(dialog).toHaveCount(0)
    await page.waitForTimeout(300)
    assert.deepEqual(loginDeletes(), expectedDeletes)
    assert.ok(pool().includes('acct-new'))
  })

  await step('响应缺少 added 字段（旧版 core）：取消时不删', async () => {
    await page.goto(base + 'settings/compute')
    await expect(page.locator('.dev b', { hasText: 'gpu-worker' })).toBeVisible()
    await open()
    await login(OTHER, undefined)
    await button('取消').click()
    await expect(dialog).toHaveCount(0)
    await page.waitForTimeout(300)
    assert.deepEqual(loginDeletes(), expectedDeletes)
  })

  await step('登录 POST 未返回时点「上一步」：迟到响应直接发 DELETE 且不打开授权页', async () => {
    await open()
    const beforeTabs = await page.evaluate(() => window.__openedTabs.length)
    let releasePost
    deferLoginPost = new Promise((r) => { releasePost = r })
    await dialog.getByRole('button', { name: /用 Google 账号登录/ }).click()
    await expect.poll(() => currentLogin).toBeTruthy()
    const lateId = currentLogin
    await button('上一步').click()
    await expect(dialog).toContainText('要添加哪一种')
    releasePost()
    expectedDeletes.push(lateId)
    await expect.poll(loginDeletes).toEqual(expectedDeletes)
    const tab = await page.evaluate((i) => window.__openedTabs[i], beforeTabs)
    assert.equal(tab.location.href, '')
    assert.equal(tab.closed, true)
    await button('取消').click()
    await expect(dialog).toHaveCount(0)
  })

  await step('新账号入池但未到第 3 步时用全局导航快捷键离开：卸载向导并删掉该账号', async () => {
    core.world.cloud.accounts = core.world.cloud.accounts.filter((a) => a.id !== 'acct-new')
    await open()
    const id = await login(NEW, true)
    assert.ok(pool().includes('acct-new'))
    await page.keyboard.press('Control+Alt+Digit1')
    await expect(dialog).toHaveCount(0)
    expectedDeletes.push(id)
    await expect.poll(loginDeletes).toEqual(expectedDeletes)
    assert.ok(!pool().includes('acct-new'))
  })

  await step('登录 POST 未返回时用全局导航快捷键离开：迟到响应直接发 DELETE 且不打开授权页', async () => {
    await page.goto(base + 'settings/compute')
    await expect(page.locator('.dev b', { hasText: 'gpu-worker' })).toBeVisible()
    await open()
    const beforeTabs = await page.evaluate(() => window.__openedTabs.length)
    let releasePost
    deferLoginPost = new Promise((r) => { releasePost = r })
    await dialog.getByRole('button', { name: /用 Google 账号登录/ }).click()
    await expect.poll(() => currentLogin).toBeTruthy()
    const lateId = currentLogin
    await page.keyboard.press('Control+Alt+Digit1')
    await expect(dialog).toHaveCount(0)
    releasePost()
    expectedDeletes.push(lateId)
    await expect.poll(loginDeletes).toEqual(expectedDeletes)
    const tab = await page.evaluate((i) => window.__openedTabs[i], beforeTabs)
    assert.equal(tab.location.href, '')
    assert.equal(tab.closed, true)
  })

  assert.deepEqual(accountDeletes(), [])
  assert.deepEqual(errors, [])
  assert.deepEqual(core.unexpected, [])
  console.log(`cloud wizard: ${loginDeletes().length} login cancels, 0 direct account deletes, no page errors, no unexpected requests`)
  await page.close()
} finally {
  await browser.close()
}
