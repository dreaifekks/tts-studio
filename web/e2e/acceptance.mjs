// 真实环境验收：会生成音频、写 review、改 tag（测完恢复）、建 zz-test- 作品（测完删除）。不提交训练（POST /train、/train/sweep 一律拦截）。
// 用法：node e2e/acceptance.mjs [base] [shotDir]
import { chromium } from '@playwright/test'
import { mkdtempSync, mkdirSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const BASE = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const ORIGIN = new URL(BASE).origin
const SHOTS = (process.argv[3] ?? mkdtempSync(join(tmpdir(), 'tts-studio-screens-'))).replace(/\/?$/, '/')
mkdirSync(SHOTS, { recursive: true })
mkdirSync('/tmp/tts-dl', { recursive: true })
const stamp = new Date().toISOString().replace(/\D/g, '').slice(4, 12)
const WORK_TITLE = `zz-test-web-${stamp}`
const results = []
const log = (ok, name, detail = '') => { results.push({ ok, name, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`) }
const check = (cond, name, detail) => log(!!cond, name, detail)
const started = Date.now()

async function api(method, path, body) {
  const res = await fetch(ORIGIN + path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined })
  const text = await res.text()
  return { status: res.status, json: text ? JSON.parse(text) : null }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const createdWorks = new Set()
const refsToRestore = new Map()
const reviewedAudio = new Set()   // 本轮写过 review 的音频，结束时清掉

const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })
const status = (page) => page.locator('.statusbar').innerText()
async function waitStatus(page, re, timeout = 300_000) {
  await page.waitForFunction((src) => new RegExp(src).test(document.querySelector('.statusbar')?.textContent ?? ''), re.source, { timeout })
}
const waitConnected = (page) => page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('已连接'), null, { timeout: 60_000 })
const nav = (page, title) => page.locator('nav a', { hasText: title }).click()
const shot = (page, name) => page.screenshot({ path: `${SHOTS}${name}.png` })
const summaryText = (page, scope = '') => page.locator(`${scope} .where .auto-sum`).first().innerText()
const isTrainPost = (r) => r.method() === 'POST' && ['/train', '/train/sweep'].includes(new URL(r.url()).pathname)
/** 绝不提交训练：POST /train、/train/sweep 直接拦掉（/train/sweep/plan 是预览报价，不拦）。 */
async function blockTraining(page, posts = []) {
  const handler = (route) => { posts.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON() }); return route.abort() }
  await page.route('**/train', handler)
  await page.route('**/train/sweep', handler)
  return posts
}

try {
  // ───────── 1. 主控不在线：/health 连不上时各页只显示「主控不在线」，不拉数据；「重新连接」后恢复
  {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    const page = await ctx.newPage()
    await blockTraining(page)
    let down = true
    const apiCalls = []
    await page.route('**/health', (route) => (down ? route.abort('connectionrefused') : route.continue()))
    page.on('request', (r) => {
      const u = new URL(r.url())
      if (u.origin === ORIGIN && !u.pathname.startsWith('/ui')) apiCalls.push(`${r.method()} ${u.pathname}`)
    })
    await page.goto(BASE)
    await page.waitForSelector('.empty.offline', { timeout: 30_000 })
    const offlineShown = async () => (await page.locator('.empty.offline').count()) === 1
      && (await page.locator('.empty.offline .empty-title').innerText()) === '主控不在线'
      && (await page.getByRole('button', { name: '重新连接' }).isVisible())
    const notOffline = []
    for (const t of ['生成', '参考库 / Tag', '历史', '统计', '作品', '训练', '模型', '盲听', '设置']) {
      await nav(page, t)
      await page.waitForTimeout(300)
      if (!(await offlineShown())) notOffline.push(t)
    }
    check(!notOffline.length, '离线：九个页面（含设置）都只显示「主控不在线」和「重新连接」', notOffline.join(', ') || '全部')
    const st = (await status(page)).replace(/\n/g, ' ')
    check(st.includes('未连接') && st.includes('主控不在线') && st.includes('0 个任务运行中'), '离线：状态栏显示「未连接 · 主控不在线」', st)
    const others = apiCalls.filter((c) => c !== 'GET /health')
    check(apiCalls.length > 0 && others.length === 0, '离线：打开并切换各页只查 /health，不拉别的数据', others.join(', ') || `只有 ${apiCalls.length} 次 /health`)
    await nav(page, '参考库 / Tag')
    await shot(page, 'desktop-offline')
    await page.setViewportSize({ width: 390, height: 844 })
    await shot(page, 'mobile-offline')
    await page.setViewportSize({ width: 1440, height: 900 })
    // 主控回来：点「重新连接」加载数据
    down = false
    await page.getByRole('button', { name: '重新连接' }).click()
    await page.waitForSelector('.ref-row', { timeout: 60_000 })
    await page.waitForFunction(() => /已连接/.test(document.querySelector('.statusbar')?.textContent ?? '') && /资料库已同步 · studio-host/.test(document.querySelector('.statusbar .gw-label')?.textContent ?? ''), null, { timeout: 30_000 })
    const st2 = (await status(page)).replace(/\n/g, ' ')
    check(/已连接/.test(st2) && /资料库已同步 · studio-host \S+ · 云端 GPU 未开/.test(st2) && /\d+ 个任务运行中 ▴/.test(st2) && (await page.getByRole('button', { name: '刷新连接' }).count()) === 1,
      '重新连接：页面加载参考库，状态栏「已连接 · 资料库已同步 · studio-host … · 云端 GPU 未开 | … | N 个任务运行中 ▴ | ↻」', st2)
    // 已连上之后：一次 /health 失败不算离线，连续两次才算。页面每 15 秒查一次 /health，紧跟在一次定时查询之后做，避免它插进来多算一次失败
    await page.waitForRequest((r) => new URL(r.url()).pathname === '/health', { timeout: 20_000 })
    await page.waitForTimeout(300)
    down = true
    await page.getByRole('button', { name: '刷新连接' }).click()
    await page.waitForTimeout(1000)
    check((await page.locator('.empty.offline').count()) === 0 && (await page.locator('.ref-row').count()) > 0, '离线：已连接时 /health 失败一次不切到离线页')
    await page.getByRole('button', { name: '刷新连接' }).click()
    await page.waitForSelector('.empty.offline', { timeout: 15_000 })
    check((await status(page)).includes('未连接'), '离线：连续两次 /health 失败后显示「主控不在线」')
    down = false
    await page.getByRole('button', { name: '重新连接' }).click()
    await page.waitForSelector('.ref-row', { timeout: 60_000 })
    check((await page.locator('.empty.offline').count()) === 0, '重新连接：恢复后参考库重新加载')

    // 云端花费超过提醒线时的确认窗：本环境只有本机，真实提交不会出现，这里用模拟的 409 confirm_required 检查界面
    await nav(page, '生成')
    await page.waitForFunction(() => /自动：/.test(document.querySelector('.where .auto-sum')?.textContent ?? ''), null, { timeout: 30_000 })
    let posts409 = 0
    const quote = {
      recommended: 'colab-T4', reason_code: 'cloud_halves', reason: '', chosen: 'colab-T4', confirm: ['ask_over'], month_used: 0, ask_over: 0.2, ask_month: 500,
      candidates: [
        { device: 'local', name: 'studio-host', type: 'local', gpu: null, seconds: 30, units: null, speed_source: 'measured', available: true, reason_code: null, reason: null, idle: true, premium: false, wake: false },
        { device: 'colab-T4', name: '云端 T4', type: 'cloud', gpu: 'T4', seconds: 400, units: 0.3, speed_source: 'reference', available: true, reason_code: null, reason: null, idle: true, premium: false, wake: false },
      ],
    }
    await page.route('**/tts/candidates/jobs', (route) => { posts409++; return route.fulfill({ status: 409, json: { detail: '需要确认', code: 'confirm_required', plan: quote } }) })
    await page.getByRole('button', { name: '出候选' }).click()
    const sheet = page.locator('.sheet[role="dialog"]')
    await sheet.waitFor({ timeout: 15_000 })
    const head = await sheet.locator('h3').innerText()
    check(head === '这次约花 0.3 单元，开始吗？' && await sheet.getByRole('button', { name: '用 T4 开始' }).isVisible() && await sheet.getByRole('button', { name: '取消', exact: true }).isVisible(),
      '确认窗（模拟 409 confirm_required）：「这次约花 N 单元，开始吗？」「用 X 开始」「取消」', head)
    await shot(page, 'desktop-compute-confirm')
    await sheet.getByRole('radio', { name: /studio-host/ }).click()
    check(await sheet.locator('h3').innerText() === '这次免费，开始吗？' && await sheet.getByRole('button', { name: '用 studio-host 开始' }).isVisible(), '确认窗：换选 studio-host 后标题和按钮跟着变')
    await sheet.getByRole('button', { name: '取消', exact: true }).click()
    await sheet.waitFor({ state: 'detached' })
    await page.waitForTimeout(500)
    check(posts409 === 1 && (await page.locator('.error-bar').count()) === 0 && await page.getByRole('button', { name: '♫ 出候选' }).isEnabled(),
      '确认窗：取消后不再提交、不报错，可以重新出候选', `POST ${posts409} 次`)
    await ctx.close()
  }

  // ───────── 2. 桌面主流程
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true })
  const page = await ctx.newPage()
  globalThis.mainPage = page
  const errors = []
  page.on('pageerror', (e) => errors.push({ text: e.message, url: '' }))
  page.on('console', (m) => { if (m.type() === 'error') errors.push({ text: m.text(), url: m.location()?.url ?? '' }) })
  const trainPosts = await blockTraining(page)
  await page.goto(BASE)
  await waitConnected(page)

  // 出候选下方的「在哪跑」摘要与「更改」
  for (let i = 0; i < 3; i++) await page.getByRole('button', { name: '减少候选' }).click()
  await page.waitForFunction(() => /^自动：在 \S+ 上生成 · 约 \d+ 分钟/.test(document.querySelector('.where .auto-sum')?.textContent ?? ''), null, { timeout: 30_000 })
  const genSummary = await summaryText(page)
  check(/^自动：在 studio-host 上生成 · 约 \d+ 分钟 · 本机就很快$/.test(genSummary), '生成：「出候选」下方一行算力摘要', genSummary)
  const where = page.locator('.where').first()
  await where.getByRole('button', { name: '更改' }).click()
  const listbox = page.getByRole('listbox', { name: '这一次在哪生成' })
  await listbox.waitFor()
  const options = (await listbox.getByRole('option').allInnerTexts()).map((t) => t.replace(/\n/g, ' '))
  const quoted = (await api('POST', '/plan', { kind: 'gen', engine: 'v2pro', items: 3 })).json.candidates
  check(options[0].startsWith('自动选择') && options.some((o) => o.startsWith('studio-host')) && options.length === quoted.length + 1
    && (await listbox.locator('[role="option"][aria-disabled="true"]').count()) === quoted.filter((c) => !c.available).length,
  '生成：「更改」打开设备列表（自动选择 / studio-host / 不可用的云端置灰）', options.map((o) => o.split(' ')[0]).join(' | '))
  await listbox.getByRole('option', { name: /^studio-host/ }).click()
  await page.waitForFunction(() => /^这次：在 studio-host 上生成 · 约 \d+ 分钟 · 免费$/.test(document.querySelector('.where .auto-sum')?.textContent ?? ''), null, { timeout: 15_000 })
  check(await where.getByRole('button', { name: '恢复自动' }).isVisible(), '生成：选 studio-host 后摘要变为「这次：…」，出现「恢复自动」', await summaryText(page))
  await where.getByRole('button', { name: '恢复自动' }).click()
  await page.waitForFunction(() => /^自动：/.test(document.querySelector('.where .auto-sum')?.textContent ?? ''), null, { timeout: 15_000 })

  // 出候选：「记录 Debug」开关已移除。这里在请求里补 debug:true，后面才能检查「⋯ → 生成详情」里的采样轨迹
  const genBodies = []
  await page.route('**/tts/candidates/jobs', (route) => {
    const body = route.request().postDataJSON()
    genBodies.push(body)
    return route.continue({ postData: JSON.stringify({ ...body, debug: true }) })
  })
  // 页面内记录每次候选数变化，避免轮询漏掉中间状态
  await page.evaluate(() => {
    window.__counts = []
    new MutationObserver(() => {
      const n = document.querySelectorAll('.candidate').length
      if (window.__counts.at(-1) !== n) window.__counts.push(n)
    }).observe(document.body, { subtree: true, childList: true, characterData: true })
  })
  const version = (await api('GET', '/characters/alice/versions')).json.find((v) => v.recommended)
  await page.getByRole('button', { name: '出候选' }).click()
  await waitStatus(page, /已生成 3 个候选/)
  await page.unroute('**/tts/candidates/jobs')
  const sent = genBodies[0] ?? {}
  check(genBodies.length === 1 && sent.version === version?.id && sent.engine === version?.engine && sent.gpt === version?.gpt && sent.n === 3 && sent.debug === false && !('compute' in sent),
    '生成：按所选「模型版本」提交（带 version，自动选设备不带 compute）', JSON.stringify({ version: sent.version, n: sent.n, debug: sent.debug, compute: sent.compute }))
  check((await page.locator('.sheet').count()) === 0, '生成：只有本机时不弹花费确认窗')
  const counts = await page.evaluate(() => window.__counts)
  const distinct = [...new Set(counts)]
  check(distinct.includes(1) || distinct.includes(2), 'SSE：候选边生成边显示', `观测到的候选数序列 ${distinct.join('→')}`)
  check((await page.locator('.candidate.best .best-label').count()) === 1, '候选：首条标「最佳」并按评分排序')
  const metricTitles = await page.locator('.candidate').first().locator('.metric-title').allInnerTexts()
  check(metricTitles.join('/') === '评分/读音准确/音色相似', '候选：指标为 评分 / 读音准确 / 音色相似', metricTitles.join(' / '))
  const scores = await page.locator('.candidate .metric-value').evaluateAll((els) => els.filter((_, i) => i % 3 === 0).map((e) => Number(e.textContent)))
  check(scores.length === 3 && scores.every((s, i) => Number.isFinite(s) && (i === 0 || s <= scores[i - 1])), '候选：评分降序', scores.join(' ≥ '))
  await shot(page, 'desktop-generate')

  // 播放 / 键盘
  const cards = page.locator('.candidate')
  await cards.nth(0).locator('button.play').click()
  await page.waitForFunction(() => document.querySelector('.candidate .play')?.textContent === '❚❚', null, { timeout: 15_000 })
  check((await status(page)).includes('暂停'), '播放：候选播放，底部出现「暂停」')
  await cards.nth(0).locator('.candidate-text').click()
  await page.keyboard.press(' ')
  await page.waitForTimeout(300)
  check((await cards.nth(0).locator('button.play').innerText()) === '▶', '快捷键：空格暂停当前候选')
  await page.keyboard.press('ArrowDown')
  check(await cards.nth(1).evaluate((e) => e.classList.contains('selected')), '快捷键：↓ 选择下一条')
  await page.keyboard.press('ArrowUp')
  check(await cards.nth(0).evaluate((e) => e.classList.contains('selected')), '快捷键：↑ 选择上一条')
  const idA = await cards.nth(0).getAttribute('data-id')
  const idB = await cards.nth(1).getAttribute('data-id')
  reviewedAudio.add(idA)

  // 生成详情（原来卡片上的「Debug」按钮，现在收在「⋯」里）
  await cards.nth(0).getByRole('button', { name: /的更多操作$/ }).click()
  const menu = page.getByRole('menu', { name: /的更多操作$/ })
  await menu.waitFor()
  const menuText = (await menu.innerText()).replace(/\n/g, ' | ')
  check(/生成编号：\d+/.test(menuText) && (await menu.getByRole('menuitem', { name: '生成详情' }).count()) === 1, '候选：「⋯」里有生成编号和「生成详情」', menuText)
  await menu.getByRole('menuitem', { name: '生成详情' }).click()
  await page.waitForSelector('.modal .recharts-line', { timeout: 15_000 })
  const box = await page.locator('.modal .recharts-surface').boundingBox()
  await page.mouse.move(box.x + box.width * 0.45, box.y + box.height * 0.5)
  await page.waitForTimeout(400)
  const tip = await page.locator('.modal .chart-tip').innerText().catch(() => '')
  check(/token \d+/.test(tip) && tip.includes('Top-5') && tip.includes('采样概率'), '生成详情：熵曲线 hover 显示 token、概率、Top-5', tip.split('\n')[0])
  check(await page.locator('.modal .recharts-reference-line').count() >= 1, '生成详情：EOS 竖线')
  await page.waitForFunction(() => { const i = document.querySelector('.modal .mel img'); return i && i.complete && i.naturalWidth > 0 }, null, { timeout: 15_000 })
  check(true, '生成详情：mel 频谱图加载')
  await shot(page, 'desktop-debug')
  await page.setViewportSize({ width: 390, height: 844 })
  await shot(page, 'mobile-debug')
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.keyboard.press('Escape')
  check(await page.locator('.modal').count() === 0, '生成详情：Esc 关闭')

  // 加入作品：新建作品 + 新建一行
  await cards.nth(0).getByRole('button', { name: '加入作品' }).click()
  await page.locator('.modal').getByLabel('作品', { exact: true }).selectOption('')
  await page.locator('.modal input').first().fill(WORK_TITLE)
  await shot(page, 'desktop-add-to-work')
  await page.getByRole('button', { name: '加入并选用' }).click()
  await waitStatus(page, /已加入新行并选用此音频/)
  const works = (await api('GET', '/works')).json.works.filter((w) => w.title === WORK_TITLE)
  works.forEach((w) => createdWorks.add(w.id))
  const workId = works[0]?.id
  check(workId && (await page.locator('nav a.active').innerText()).includes('作品'), '加入作品：新建作品 + 新建一行，跳到作品页', workId)
  let w = (await api('GET', `/works/${workId}`)).json
  check(w.lines.length === 1 && w.lines[0].picked === idA, '加入作品：新行直接选用该音频', `picked=${w.lines[0]?.picked}`)

  // 加入作品：挂到已有行
  await nav(page, '生成')
  await page.locator(`.candidate[data-id="${idB}"]`).getByRole('button', { name: '加入作品' }).click()
  await page.locator('.modal').getByRole('radio', { name: '挂到已有行' }).click()
  await page.waitForFunction(() => [...document.querySelectorAll('.modal select')].some((s) => s.getAttribute('aria-label') === '目标行' && s.value))
  await page.getByRole('button', { name: '加入并选用' }).click()
  await waitStatus(page, /已挂到该行并选用此音频/)
  w = (await api('GET', `/works/${workId}`)).json
  check(w.lines.length === 1 && w.lines[0].picked === idB && w.lines[0].candidates.length === 2, '加入作品：挂到已有行并选用', `candidates=${w.lines[0].candidates.length}`)

  // review（只打在 zz-test 作品自己的候选上）
  const reviewPosts = (id, n) => {
    let seen = 0
    return page.waitForResponse((r) => r.url().includes(`/audio/${id}/review`) && r.request().method() === 'POST' && ++seen >= n, { timeout: 15_000 })
  }
  await page.getByRole('button', { name: /候选（2）/ }).click()
  const lineCard = page.locator(`.line .candidate[data-id="${idA}"]`)
  // 打分后不等返回立刻加标签：两次写入串行合并，互不覆盖
  const twoWrites = reviewPosts(idA, 2)
  await lineCard.getByRole('button', { name: '打 4 分' }).click()
  await lineCard.getByRole('button', { name: '问题标签' }).click()
  await lineCard.getByRole('menuitemcheckbox', { name: /吞字/ }).click()
  await twoWrites
  let meta = (await api('GET', `/audio/${idA}/meta`)).json
  check(meta.review?.rating === 4 && meta.review?.tags?.includes('吞字'), 'review：就地打分 + 问题标签（连续操作不互相覆盖）', JSON.stringify(meta.review))
  await page.keyboard.press('Escape')
  await nav(page, '生成')
  await page.locator(`.candidate[data-id="${idA}"] .candidate-text`).click()
  const oneWrite = reviewPosts(idA, 1)
  await page.keyboard.press('5')
  await oneWrite
  await waitStatus(page, /评价已保存/)
  meta = (await api('GET', `/audio/${idA}/meta`)).json
  check(meta.review?.rating === 5 && meta.review?.tags?.includes('吞字'), '快捷键：数字 5 评分并保留已有标签', JSON.stringify(meta.review))
  check((await page.locator(`.candidate[data-id="${idA}"] .star.on`).count()) === 5, 'review：生成页卡片同步显示新评分')

  // ───────── 作品
  await nav(page, '作品')
  await page.getByRole('button', { name: '＋ 添加行' }).click()
  await page.locator('.line').nth(1).getByLabel('台词').fill('テストです。よろしくね。')
  check(await page.locator('.warn', { hasText: '未保存' }).count() === 1, '作品：编辑后标记未保存')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await page.waitForFunction(() => !document.querySelector('.editor .warn'))
  w = (await api('GET', `/works/${workId}`)).json
  check(w.lines.length === 2 && w.lines[1].text === 'テストです。よろしくね。', '作品：新增行保存（整行 PATCH）')
  // 拖动排序：第 2 行拖到第 1 行
  const h2 = await page.locator('.line').nth(1).locator('.handle').boundingBox()
  const h1 = await page.locator('.line').nth(0).locator('.handle').boundingBox()
  await page.mouse.move(h2.x + h2.width / 2, h2.y + h2.height / 2)
  await page.mouse.down()
  for (let i = 1; i <= 12; i++) await page.mouse.move(h2.x + h2.width / 2, h2.y + h2.height / 2 - ((h2.y - h1.y + 20) * i) / 12)
  await page.mouse.up()
  await page.waitForTimeout(400)
  check((await page.locator('.line').nth(0).getByLabel('台词').inputValue()) === 'テストです。よろしくね。', '作品：拖动手柄重排行')
  await page.locator('.line').nth(1).getByLabel('停顿秒数').fill('0.8')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await page.waitForFunction(() => !document.querySelector('.editor .warn'))
  w = (await api('GET', `/works/${workId}`)).json
  check(w.lines[0].text === 'テストです。よろしくね。' && w.lines[1].pause_after === 0.8 && w.lines[1].picked === idB, '作品：顺序与停顿保存，已有行保留 id 与选择')

  // 生成选中行；503（训练中）→ 按 Retry-After 重试。候选提交已改走算力 planner（不自动重试 503），作品生成仍自动重试
  let first503 = true
  await page.route('**/works/*/generate', (route) => {
    if (first503 && route.request().method() === 'POST') { first503 = false; return route.fulfill({ status: 503, headers: { 'Retry-After': '2' }, json: { detail: 'GPU 正在训练' } }) }
    return route.continue()
  })
  const mockHealth = (route) => route.fulfill({ json: { status: 'ok', time: '', training_job: 'train-zz-test-mock', workers: {}, version: '', commit: '' } })
  await page.route('**/health', mockHealth)
  await page.evaluate(() => {
    window.__statuses = new Set()
    new MutationObserver(() => window.__statuses.add(document.querySelector('.status-text')?.textContent ?? ''))
      .observe(document.body, { subtree: true, childList: true, characterData: true })
  })
  await page.locator('.line').nth(0).getByLabel('选中行').check()
  await page.getByRole('button', { name: '生成选中行' }).click()
  await page.waitForSelector('.job-progress', { timeout: 30_000 })
  await page.unroute('**/health', mockHealth)
  await page.unroute('**/works/*/generate')
  const seenStatus = await page.evaluate(() => [...window.__statuses])
  const retryMsg = seenStatus.find((s) => s.includes('正在训练：train-zz-test-mock') && /\d+ 秒后重试/.test(s))
  check(!first503 && retryMsg, '503：训练中显示原因与等待秒数并自动重试（作品生成）', retryMsg ?? seenStatus.join(' / '))
  await shot(page, 'desktop-works-progress')
  await waitStatus(page, /作品候选已生成/)
  w = (await api('GET', `/works/${workId}`)).json
  check(w.lines[0].candidates.length === 3 && w.lines[0].picked, '作品：生成选中行（SSE 跟踪到完成）', `candidates=${w.lines[0].candidates.length}`)
  // pick
  await page.locator('.line').nth(0).getByRole('button', { name: /候选（3）/ }).click()
  const pickBtn = page.locator('.line').nth(0).locator('.candidate').getByRole('button', { name: '选用', exact: true }).first()
  const pickId = await pickBtn.locator(`xpath=ancestor::div[contains(concat(' ',@class,' '),' candidate ')][1]`).getAttribute('data-id')
  await pickBtn.click()
  await waitStatus(page, /已选用/)
  w = (await api('GET', `/works/${workId}`)).json
  check(w.lines[0].picked === pickId, '作品：手动 pick 候选', pickId)
  // 改台词：旧候选只可试听；单行重新生成后新候选可选
  const unsaved = () => page.waitForFunction(() => ![...document.querySelectorAll('.editor .warn')].some((e) => e.textContent === '未保存'))
  await page.locator('.line').nth(1).getByLabel('台词').fill('おはよう。今日はいい天気だね！')
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await unsaved()
  check(await page.locator('.line').nth(1).locator('.warn', { hasText: '请重新生成此行' }).count() === 1, '作品：改台词后提示需重新生成')
  await page.locator('.line').nth(1).getByRole('button', { name: /候选（2）/ }).click()
  const hints = () => page.locator('.line').nth(1).getByText('旧台词或声音设置，仅供试听').count()
  const oldPickable = await page.locator('.line').nth(1).getByRole('button', { name: '选用', exact: true }).evaluateAll((bs) => bs.filter((b) => !b.disabled).length)
  check(await hints() === 2 && oldPickable === 0 && !(await page.getByRole('button', { name: '渲染', exact: true }).isEnabled()), '作品：旧候选仅供试听且不能渲染', `提示 ${await hints()} 条`)
  await page.locator('.line').nth(1).getByRole('button', { name: '重新生成' }).click()
  await waitStatus(page, /该行已重新生成/)
  w = (await api('GET', `/works/${workId}`)).json
  check(w.lines[1].candidates.length === 5 && await hints() === 2, '作品：单行重新生成追加候选，旧候选仍不可选', `candidates=${w.lines[1].candidates.length}`)
  // 渲染
  await page.getByRole('button', { name: '渲染', exact: true }).click()
  await waitStatus(page, /渲染完成/)
  await page.waitForSelector('.render-bar')
  check(true, '作品：渲染完成，显示整轨区')
  await page.getByRole('button', { name: '▶ 播放整轨' }).click()
  await page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('暂停'), null, { timeout: 15_000 })
  check(true, '作品：播放整轨')
  await page.getByRole('button', { name: '查看 SRT' }).click()
  await page.waitForSelector('.modal pre')
  check((await page.locator('.modal pre').innerText()).includes('-->'), '作品：查看 SRT')
  await page.keyboard.press('Escape')
  for (const ext of ['mp3', 'wav', 'srt', 'lrc']) {
    await page.locator('.render-bar summary').click()
    const [dl] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: `${ext.toUpperCase()}…` }).click()])
    const path = `/tmp/tts-dl/${dl.suggestedFilename()}`
    await dl.saveAs(path)
    check(dl.suggestedFilename() === `${WORK_TITLE}.${ext}` && statSync(path).size > 0, `作品：下载 ${ext}`, `${dl.suggestedFilename()} ${statSync(path).size} 字节`)
  }
  await page.locator('.statusbar').getByRole('button', { name: /暂停/ }).click().catch(() => {})
  while (await page.locator('.disclosure-btn', { hasText: '▾' }).count()) await page.locator('.disclosure-btn', { hasText: '▾' }).first().click()
  await page.locator('.content').evaluate((e) => e.scrollTo(0, 0))
  await shot(page, 'desktop-works')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.editor').scrollIntoViewIfNeeded()
  await shot(page, 'mobile-works')
  await page.setViewportSize({ width: 1440, height: 900 })
  // 生成页（带候选）的手机截图
  await nav(page, '生成')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.candidates-col').scrollIntoViewIfNeeded()
  await shot(page, 'mobile-generate')
  await page.setViewportSize({ width: 1440, height: 900 })

  // 删除作品（UI，带确认）
  await nav(page, '作品')
  await page.getByRole('button', { name: '删除作品' }).click()
  await page.getByRole('button', { name: '删除作品' }).last().click()
  await waitStatus(page, /作品已删除/)
  check((await api('GET', `/works/${workId}`)).status === 404, '作品：确认后 DELETE 删除')
  createdWorks.delete(workId)

  // ───────── 参考库 / Tag
  await nav(page, '参考库 / Tag')
  await page.waitForSelector('.ref-row')
  await page.getByLabel('搜索原文或 ID').fill('alice-000001')
  const rows = page.locator('.ref-row')
  const refIds = await rows.evaluateAll((els) => els.slice(0, 3).map((e) => e.getAttribute('data-id')))
  const allRefs = (await api('GET', '/refs?character=alice')).json.refs
  // zz-test 只是测试标签：记原值时去掉它，另一个同时在跑的验收留下的 zz-test 不会被当成原值写回
  for (const id of refIds) refsToRestore.set(id, allRefs.find((r) => r.id === id).tags.filter((t) => t !== 'zz-test'))
  await page.locator('summary', { hasText: '自定义快捷键' }).click()
  await page.getByLabel('按键（逗号分隔）').fill('q,w,e,r,t,z')
  await page.getByLabel('对应标签（逗号分隔）').fill('开心,生气,害羞,低落,平静,zz-test')
  await rows.nth(0).locator('.ref-main').click()
  await page.waitForFunction((id) => document.querySelector(`.ref-row[data-id="${id}"] .play`)?.textContent === '❚❚', refIds[0], { timeout: 15_000 })
  check(true, '参考库：选中一条自动播放', refIds[0])
  await page.keyboard.press('z')
  await page.keyboard.press('z')
  await page.waitForFunction((id) => document.querySelector(`.ref-row[data-id="${id}"]`)?.classList.contains('selected'), refIds[2])
  await page.waitForFunction((id) => document.querySelector(`.ref-row[data-id="${id}"] .play`)?.textContent === '❚❚', refIds[2], { timeout: 15_000 })
  check(true, '参考库：连按两次打标后跳到第三条并自动播放')
  await page.waitForTimeout(1500)
  let after = (await api('GET', '/refs?character=alice')).json.refs
  const t0 = after.find((r) => r.id === refIds[0]).tags
  const t1 = after.find((r) => r.id === refIds[1]).tags
  check(t0.includes('zz-test') && t1.includes('zz-test') && refsToRestore.get(refIds[0]).every((t) => t0.includes(t)),
    '参考库：快捷键打标（整体替换，保留旧标签，串行写入）', `${refIds[0]}=${t0} ${refIds[1]}=${t1}`)
  await shot(page, 'desktop-refs')
  await page.setViewportSize({ width: 390, height: 844 })
  await shot(page, 'mobile-refs')
  await page.setViewportSize({ width: 1440, height: 900 })
  await rows.nth(0).getByRole('button', { name: '移除标签 zz-test' }).click()
  await page.waitForTimeout(1500)
  after = (await api('GET', '/refs?character=alice')).json.refs
  check(!after.find((r) => r.id === refIds[0]).tags.includes('zz-test'), '参考库：点标签移除')

  // ───────── 历史
  await nav(page, '历史')
  await page.waitForSelector('.scroll-list .candidate')
  const pagerText = () => page.locator('.pager').innerText()
  check((await page.locator('.scroll-list .candidate').count()) === 30, '历史：每页 30 条', await pagerText())
  await page.getByRole('button', { name: '下一页' }).click()
  await page.waitForFunction(() => document.querySelector('.pager')?.textContent?.includes('31–'))
  check(true, '历史：下一页', await pagerText())
  await page.getByRole('button', { name: '上一页' }).click()
  await page.waitForFunction(() => document.querySelector('.pager')?.textContent?.includes('· 1–'))
  const reviewsReq = page.waitForRequest((r) => r.url().includes('/reviews?') && r.url().includes('min_rating=4'))
  await page.getByLabel('最低分').selectOption('4')
  await reviewsReq
  check(await page.locator('select[aria-label="评价"]').isDisabled(), '历史：最低分改用 /reviews 并禁用评价筛选')
  await page.getByLabel('最低分').selectOption('0')
  const searchReq = page.waitForRequest((r) => r.url().includes('/history?') && r.url().includes('q='))
  await page.getByLabel('搜索台词').fill('おはよう')
  await searchReq
  await page.waitForTimeout(800)
  await page.locator('.scroll-list .candidate').first().getByRole('button', { name: '全部参数' }).click()
  await page.waitForSelector('.modal pre')
  check((await page.locator('.modal pre').innerText()).includes('"temperature"'), '历史：全部参数')
  await page.keyboard.press('Escape')
  await shot(page, 'desktop-history')
  await page.setViewportSize({ width: 390, height: 844 })
  await shot(page, 'mobile-history')
  await page.setViewportSize({ width: 1440, height: 900 })

  // ───────── 统计
  await nav(page, '统计')
  await page.waitForSelector('.summary')
  await page.getByLabel('比较维度').selectOption('character')
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length >= 1, null, { timeout: 10_000 }).catch(() => {})
  check((await page.locator('tbody tr').count()) >= 1, '统计：汇总、好评率图、维度切换与表格')
  await shot(page, 'desktop-stats')
  await page.setViewportSize({ width: 390, height: 844 })
  await shot(page, 'mobile-stats')
  await page.setViewportSize({ width: 1440, height: 900 })

  // ───────── 训练（提交被拦截，不会真的训练）
  await nav(page, '训练')
  await page.waitForSelector('.metrics.two')
  const before = await page.locator('.metrics.two').innerText()
  const previewRes = page.waitForResponse((r) => r.url().includes('/preview?') && r.url().includes('max_sec=8'), { timeout: 20_000 })
  await page.getByLabel('最长秒').fill('8')
  await previewRes
  await page.waitForSelector('.metrics.two')
  await page.waitForTimeout(300)
  check((await page.locator('.metrics.two').innerText()) !== before, '训练：过滤预览随条件更新', (await page.locator('.metrics.two').innerText()).replace(/\n/g, ' '))
  await page.waitForFunction(() => /^自动：在 \S+ 上训练 · 约 \d+ (分钟|小时)/.test(document.querySelector('.where .auto-sum')?.textContent ?? ''), null, { timeout: 30_000 })
  check(/^自动：在 studio-host 上训练 · 约 \d+ (分钟|小时)/.test(await summaryText(page)), '训练：预览就绪后显示算力摘要', await summaryText(page))
  // 「任务」分段：训练 / 扫参
  const taskSeg = page.getByRole('radiogroup', { name: '任务' })
  await taskSeg.getByRole('radio', { name: '扫参' }).click()
  await page.getByRole('button', { name: '提交扫参' }).waitFor()
  check(await page.getByRole('group', { name: '角色' }).isVisible() && await page.getByRole('button', { name: '提交扫参' }).isDisabled(), '训练：「任务」切到扫参，未选角色和模型时不能提交')
  await taskSeg.getByRole('radio', { name: '训练' }).click()
  await page.waitForSelector('.metrics.two')
  // 提交训练不再二次确认：直接经算力 planner POST /train（请求在浏览器里被拦下）
  if ((await page.getByLabel('最长秒').inputValue()) !== '8') {
    const again = page.waitForResponse((r) => r.url().includes('/preview?') && r.url().includes('max_sec=8'), { timeout: 20_000 })
    await page.getByLabel('最长秒').fill('8')
    await again
  }
  await page.getByLabel('角色 slug').fill('zz-test-noop')
  const trainReq = page.waitForRequest(isTrainPost, { timeout: 15_000 })
  await page.getByRole('button', { name: '⚙ 提交训练' }).click()
  await trainReq
  await page.waitForSelector('.error-bar', { timeout: 15_000 })
  const tb = trainPosts[0]?.body ?? {}
  check(trainPosts.length === 1 && trainPosts[0].path === '/train' && tb.slug === 'zz-test-noop' && tb.max_sec === 8 && tb.min_sec === 1 && !!tb.dataset_id && tb.engine === 'v2pro' && tb.approved === false && !('compute' in tb),
    '训练：点「⚙ 提交训练」直接 POST /train（无确认窗，请求体正确，已拦截）', JSON.stringify({ slug: tb.slug, dataset_id: tb.dataset_id, engine: tb.engine, min_sec: tb.min_sec, max_sec: tb.max_sec, approved: tb.approved }))
  check((await page.locator('.modal.confirm').count()) === 0 && (await page.locator('.sheet').count()) === 0, '训练：提交时没有二次确认 / 花费确认窗')
  const leaked = (await api('GET', '/jobs?kind=train&limit=500')).json.jobs.filter((j) => j.target === 'zz-test-noop')
  check(leaked.length === 0, '训练：服务端没有 zz-test-noop 训练任务', `${leaked.length} 个`)
  await page.getByRole('button', { name: '收起错误' }).click()
  await shot(page, 'desktop-training')
  await page.setViewportSize({ width: 390, height: 844 })
  await shot(page, 'mobile-training')
  await page.setViewportSize({ width: 1440, height: 900 })
  await ctx.close()

  // 训练任务卡片：开发库里没有训练任务，用模拟的 /jobs?kind=train（一个已完成、一个进行中）看日志尾部、取消确认、评测表和试用
  {
    const ctx2 = await browser.newContext({ viewport: { width: 1440, height: 900 } })
    const p2 = await ctx2.newPage()
    const posts2 = await blockTraining(p2)
    const t0 = '2026-11-20T10:00:00'
    const compute = { device: 'local', name: 'studio-host', type: 'local', estimated_sec: 3600, units: null, reason_code: 'fastest', reason: '' }
    const doneJob = { id: 'train-zz-test-done', kind: 'train', target: 'alice', status: 'succeeded', progress: 1, stage: '完成', created: t0, started: t0, finished: t0, error: null, log_tail: ['zz-test 模拟日志 · GPT 第 15/15 轮', '训练完成'], completed: [], compute }
    let cancelledMock = false
    let cancelPosts = 0
    const runningJob = () => ({ id: 'train-zz-test-running', kind: 'train', target: 'zz-test-mock', status: cancelledMock ? 'cancelled' : 'running', progress: 0.42, stage: cancelledMock ? '已取消' : 'GPT 第 3/15 轮', created: t0, started: t0, finished: cancelledMock ? t0 : null, error: null, log_tail: ['zz-test 模拟日志'], completed: [], compute })
    const combos = [
      { gpt: 'alice-e10.ckpt', sovits: 'alice_e4_s1384.pth', n: 9, cer_median: 0.05, spk_sim_median: 0.71, score_median: 97.2 },
      { gpt: 'alice-e15.ckpt', sovits: 'alice_e8_s2768.pth', n: 9, cer_median: 0.0, spk_sim_median: 0.78, score_median: 96.6 },
    ]
    const evalMock = { job_id: doneJob.id, engine: 'v2pro', character: 'alice', ref_id: 'alice-00000010', eval_set: 'mini', sentences: ['zz-test'], candidates_per_sentence: 3, combos, best: combos[0] }
    await p2.route((u) => u.pathname === '/jobs' && u.searchParams.get('kind') === 'train', (route) => route.fulfill({ json: { jobs: [runningJob(), doneJob], total: 2 } }))
    await p2.route('**/jobs/train-zz-test-running/events', (route) => route.fulfill({ status: 200, headers: { 'Content-Type': 'text/event-stream' }, body: `event: job\ndata: ${JSON.stringify(runningJob())}\n\n` }))
    await p2.route('**/jobs/train-zz-test-running', (route) => route.fulfill({ json: runningJob() }))
    await p2.route('**/jobs/train-zz-test-running/cancel', (route) => { cancelPosts++; cancelledMock = true; return route.fulfill({ json: runningJob() }) })
    await p2.route('**/train/train-zz-test-done/eval', (route) => route.fulfill({ json: evalMock }))
    await p2.goto(BASE + 'training')
    await waitConnected(p2)
    const runningCard = p2.locator('.card', { hasText: 'zz-test-mock' })
    await runningCard.waitFor({ timeout: 20_000 })
    const doneCard = p2.locator('.card', { has: p2.getByRole('button', { name: '查看评测' }) })
    await doneCard.locator('summary', { hasText: '日志尾部' }).click()
    check((await doneCard.locator('pre.log').innerText()).includes('训练完成'), '训练：任务卡片的日志尾部')
    // 取消任务…：先「继续训练」不取消，再「取消训练」
    await runningCard.getByRole('button', { name: '取消任务…' }).click()
    const confirm = p2.locator('.modal.confirm')
    const confirmText = await confirm.innerText()
    check(confirmText.includes('取消训练任务？') && confirmText.includes('zz-test-mock') && await confirm.getByRole('button', { name: '继续训练' }).isVisible() && await confirm.getByRole('button', { name: '取消训练' }).isVisible(),
      '训练：「取消任务…」弹出确认（取消训练 / 继续训练）', confirmText.split('\n')[0])
    await confirm.getByRole('button', { name: '继续训练' }).click()
    await p2.waitForTimeout(500)
    check(cancelPosts === 0 && (await p2.locator('.modal.confirm').count()) === 0 && await runningCard.getByRole('button', { name: '取消任务…' }).isVisible(), '训练：选「继续训练」不发取消请求')
    await runningCard.getByRole('button', { name: '取消任务…' }).click()
    await p2.locator('.modal.confirm').getByRole('button', { name: '取消训练' }).click()
    await p2.waitForFunction(() => [...document.querySelectorAll('.card')].some((c) => c.textContent.includes('zz-test-mock') && c.textContent.includes('已取消') && !c.textContent.includes('取消任务…')), null, { timeout: 15_000 })
    check(cancelPosts === 1, '训练：选「取消训练」后 POST /jobs/{id}/cancel，卡片显示已取消', `cancel ${cancelPosts} 次`)
    // 评测表：模型版本 / 评分 / 读音准确 / 音色相似 / 试用，最佳行按钮「★ 最佳」
    await doneCard.getByRole('button', { name: '查看评测' }).click()
    await p2.waitForSelector('.best-row')
    const heads = await p2.locator('.best-row').locator('xpath=ancestor::table[1]//th').allInnerTexts()
    const bestRow = await p2.locator('.best-row td').allInnerTexts()
    check(heads.join('/') === '模型版本/评分/读音准确/音色相似/试用' && bestRow[4] === '★ 最佳' && bestRow[1] === '97' && bestRow[2] === '95%' && bestRow[3] === '71%',
      '训练：评测结果表（模型版本 / 评分 / 读音准确 / 音色相似 / 试用）并标出最佳', bestRow.join(' | '))
    await p2.locator('.best-row').getByRole('button', { name: '★ 最佳' }).click()
    await p2.waitForFunction(() => location.pathname === '/ui/')
    await p2.getByRole('button', { name: /更多设置/ }).click()
    await p2.waitForTimeout(800)
    const more = p2.locator('.more-settings')
    const gpt = await more.getByLabel('GPT').inputValue()
    const sovits = await more.getByLabel('SoVITS').inputValue()
    const versionSel = p2.locator('select[aria-label="模型版本"]')
    const versionLabel = await versionSel.locator('option:checked').innerText()
    const refMode = await p2.getByRole('radiogroup', { name: '参考' }).locator('[aria-checked="true"]').innerText()
    check(gpt === 'alice-e10.ckpt' && sovits === 'alice_e4_s1384.pth' && versionLabel === '更多设置中的自定组合' && refMode === '参考库'
      && await p2.locator('select[aria-label="参考"]').inputValue() === 'alice-00000010',
      '训练：「试用」带入 GPT / SoVITS 自定组合和评测参考，跳到生成页', `${gpt} + ${sovits} · ${versionLabel} · ${refMode}`)
    check(posts2.length === 0, '训练：模拟任务页没有提交训练')
    await ctx2.close()
  }

  // ───────── 手机：触屏出候选 + 播放 + 评分按钮尺寸
  {
    const m = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
    const mp = await m.newPage()
    await blockTraining(mp)
    await mp.goto(BASE)
    await waitConnected(mp)
    for (let i = 0; i < 5; i++) await mp.getByRole('button', { name: '减少候选' }).tap()
    await mp.getByRole('button', { name: '出候选' }).tap()
    await waitStatus(mp, /已生成 1 个候选/)
    await mp.locator('.candidate .play').first().tap()
    await mp.waitForFunction(() => document.querySelector('.candidate .play')?.textContent === '❚❚', null, { timeout: 15_000 })
    const h = (await mp.locator('.candidate .star').first().boundingBox()).height
    check(h >= 40, '手机：触屏出候选与播放，评分按钮足够大', `星标高度 ${h}px`)
    // 没有 debug 记录的候选：「⋯ → 生成详情」打开空状态
    await mp.locator('.candidate').first().getByRole('button', { name: /的更多操作$/ }).tap()
    await mp.getByRole('menuitem', { name: '生成详情' }).tap()
    await mp.waitForSelector('.modal')
    const empty = await mp.locator('.modal .empty').innerText().catch(() => '')
    check(empty.includes('没有采样轨迹') && (await mp.locator('.modal').innerText()).includes('本条没有 mel 频谱'), '手机：普通候选的「生成详情」显示没有采样轨迹', empty.replace(/\n/g, ' '))
    check(!empty.includes('记录 Debug'), '生成详情：空状态提示不再指向已移除的「记录 Debug」开关', empty.replace(/\n/g, ' '))
    await mp.locator('.modal').getByRole('button', { name: '完成' }).tap()
    await m.close()
  }
  const unexpected = errors.filter((e) => !e.text.includes('status of 503')   // 503 是上面故意模拟的
    && !(e.text.includes('ERR_FAILED') && /\/train(\/sweep)?$/.test(e.url))   // 被拦截的 POST /train
    && !e.text.includes('Failed to fetch'))
  check(unexpected.length === 0, '桌面流程无页面错误', unexpected.map((e) => e.text).join(' | '))
} catch (e) {
  log(false, '流程中断', e.stack?.split('\n').slice(0, 3).join(' '))
  await globalThis.mainPage?.screenshot({ path: '/tmp/tts-fail.png' }).catch(() => {})
} finally {
  for (const id of reviewedAudio) { const r = await api('DELETE', `/audio/${id}/review`); console.log('cleanup review', id, r.status) }
  for (const id of createdWorks) { const r = await api('DELETE', `/works/${id}`); console.log('cleanup DELETE', id, r.status) }
  for (const [id, tags] of refsToRestore) {
    await api('PUT', `/refs/${id}`, { tags })
    const now = (await api('GET', '/refs?character=alice')).json.refs.find((r) => r.id === id).tags
    log(JSON.stringify(now) === JSON.stringify(tags), `清理：恢复 ${id} 的原标签`, JSON.stringify(now))
  }
  const left = (await api('GET', '/works')).json.works.filter((x) => x.title === WORK_TITLE)
  log(left.length === 0, '清理：本轮 zz-test 作品已删除')
  const trainLeft = (await api('GET', '/jobs?kind=train&limit=500')).json.jobs.filter((j) => j.target.startsWith('zz-test'))
  log(trainLeft.length === 0, '清理：没有 zz-test 训练任务', `${trainLeft.length} 个`)
  await browser.close()
  const failed = results.filter((r) => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} 通过 · 用时 ${Math.round((Date.now() - started) / 1000)} 秒`)
  process.exitCode = failed.length ? 1 : 0
}
