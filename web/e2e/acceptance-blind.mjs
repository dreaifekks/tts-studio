// 第三轮验收：盲听全流程 + 模型页评测集切换，桌面和手机宽度各走一遍并截图。
// 用法：node e2e/acceptance-blind.mjs [base] [shotDir] [desktop|mobile]   （不给模式时两种都跑）
// 每遍新建一次标题 zz-test-blind-* 的盲听（alice；A = 推荐的模型版本，B = 「更多设置」里的 v2ProPlus 默认组合（底模）；快速评测；重复 1），
// 结束（含失败）时删除本轮建的盲听，并确认 GET /blind 没有 zz-test 残留。不写 review，不改权重。
// 另用模拟数据（只在浏览器里，不写服务端）检查：自动标题的显示、生成失败后的「补生成」。
import { chromium } from '@playwright/test'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const args = process.argv.slice(2)
const BASE = args.find((a) => /^https?:\/\//.test(a)) ?? 'http://127.0.0.1:5173/ui/'
const ORIGIN = new URL(BASE).origin
const SHOTS = (args.find((a) => a.startsWith('/')) ?? mkdtempSync(join(tmpdir(), 'tts-studio-screens-'))).replace(/\/?$/, '/')
const MODES = args.filter((a) => a === 'desktop' || a === 'mobile')
mkdirSync(SHOTS, { recursive: true })
const started = Date.now()
const results = []
const log = (ok, name, detail = '') => { results.push({ ok, name, detail }); console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ' — ' + detail : ''}`) }
const check = (cond, name, detail) => log(!!cond, name, detail)
async function api(method, path, body) {
  const res = await fetch(ORIGIN + path, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined })
  const text = await res.text()
  return { status: res.status, json: text ? JSON.parse(text) : null }
}
const displayEngine = (e) => ({ v2pro: 'v2Pro', v2proplus: 'v2ProPlus' })[e] ?? e

// A：alice推荐的模型版本；B：v2ProPlus 的默认组合（alice没有自己的 v2ProPlus 模型，是底模）
const models = (await api('GET', '/models')).json
const alice = models.characters.find((c) => c.slug === 'alice')
const versions = (await api('GET', '/characters/alice/versions')).json
const recommended = versions.find((v) => v.recommended) ?? versions[0]
const bDefaultVersion = versions.find((v) => v.id !== recommended.id) ?? versions[0]
const named = (engine, names) => [...alice.engines[engine].gpt, ...alice.engines[engine].sovits].filter((x) => names.includes(x.name) && x.display_name).map((x) => x.display_name)
const A = { engine: recommended.engine, gpt: recommended.gpt, sovits: recommended.sovits, version: recommended.id }
const plus = alice.engines.v2proplus
const B = { engine: 'v2proplus', gpt: plus.default_gpt, sovits: plus.default_sovits }
// 揭晓前不能出现在页面上的字样：两边的引擎（原名和界面叫法）、权重文件名、显示名、模型版本名
const forbidden = [...new Set([
  A.engine, displayEngine(A.engine), A.gpt, A.sovits, ...named(A.engine, [A.gpt, A.sovits]), recommended.label,
  B.engine, displayEngine(B.engine), B.gpt, B.sovits, ...named(B.engine, [B.gpt, B.sovits]),
])]
console.log('揭晓前禁止出现：', forbidden.join(' | '))

const created = new Set()
const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })
const blockTraining = async (page) => { for (const p of ['**/train', '**/train/sweep']) await page.route(p, (route) => route.abort()) }

async function run(mode) {
  const mobile = mode === 'mobile'
  const ctx = await browser.newContext(mobile
    ? { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true }
    : { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 })
  // 记录每次真正的 play()（静音解锁片段除外），用来确认没有自动播放
  await ctx.addInitScript(() => {
    window.__plays = []
    const orig = HTMLMediaElement.prototype.play
    HTMLMediaElement.prototype.play = function () {
      if (!this.src.startsWith('data:')) window.__plays.push(this.src)
      return orig.call(this)
    }
  })
  const page = await ctx.newPage()
  await blockTraining(page)
  const errors = []
  const metaCalls = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('request', (r) => { if (/\/audio\/[^/]+\/meta/.test(r.url())) metaCalls.push(r.url()) })
  const shot = async (name, tall = false) => {
    if (tall) await page.setViewportSize({ width: mobile ? 390 : 1440, height: mobile ? 3200 : 2000 })
    await page.screenshot({ path: `${SHOTS}${mode}-${name}.png` })
    if (tall) await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 })
    return `${SHOTS}${mode}-${name}.png`
  }
  const tap = (loc) => (mobile ? loc.tap() : loc.click())
  const plays = () => page.evaluate(() => window.__plays.length)
  const lastPlay = () => page.evaluate(() => window.__plays.at(-1) ?? '')
  const pageText = () => page.evaluate(() => document.body.innerText)
  const noLeak = async (label) => {
    const text = await pageText()
    const hits = forbidden.filter((w) => text.includes(w))
    check(!hits.length, `${mode}：${label}没有模型信息`, hits.join(', '))
  }
  const nav = (t) => tap(page.locator('nav a', { hasText: t }))
  const card = () => page.locator('.blind-card')

  await page.goto(BASE)
  await page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('已连接'), null, { timeout: 120_000 })
  await page.waitForTimeout(1500)

  // ───────── 模型页：评分评测集切换（难句评测 = hard / 快速评测 = mini）
  await nav('模型')
  await page.waitForSelector(mobile ? '.weight-card' : '.weights-table tbody tr')
  const switcher = page.getByRole('radiogroup', { name: '评分评测集' })
  check(await switcher.locator('[aria-checked="true"]').innerText() === '难句评测', `${mode}：模型页默认按难句评测`)
  await shot('models-eval-hard')
  const miniReq = page.waitForRequest((r) => r.url().includes('/weights?') && r.url().includes('eval_set=mini'))
  await tap(switcher.getByRole('radio', { name: '快速评测' }))
  await miniReq
  await page.waitForTimeout(800)
  check((await pageText()).includes('评分 · 快速评测'), `${mode}：切到快速评测后标明评测集并用 ?eval_set=mini 重新取`)
  await shot('models-eval-mini')
  // 选中alice v2Pro 默认 GPT：下面「默认 / 评测组合」切到这一行，看评测集选择、耗时和分数对比
  if (mobile) await tap(page.locator('.weight-card').filter({ hasText: 'alice · v2Pro · GPT · 随附模型' }).filter({ has: page.locator('.badge', { hasText: '默认' }) }).first())
  else await tap(page.locator(`tr.clickable:has(td[title="${A.gpt}"])`))
  if (mobile) await page.waitForSelector('.modal .weight-editor')
  const panel = page.locator('.pair-panel')
  await page.waitForFunction(() => /在 \S+ 上评测 · 约 \d+ 分钟/.test(document.querySelector('.pair-panel .auto-sum')?.textContent ?? ''), null, { timeout: 30_000 })
  const evalOpts = await panel.locator('select[aria-label="评测使用"] option').allTextContents()
  const evalSummary = await panel.locator('.auto-sum').innerText()
  check(evalOpts.join('/') === '难句评测/快速评测' && /^自动：在 \S+ 上评测 · 约 \d+ 分钟/.test(evalSummary), `${mode}：手动评测处有评测集选择和耗时提示`, `${evalOpts.join('/')} · ${evalSummary}`)
  await page.waitForFunction(() => document.querySelectorAll('.pair-panel table tbody tr').length > 0, null, { timeout: 15_000 }).catch(() => {})
  const cmpHead = await panel.locator('table th').allInnerTexts()
  const cmpRow = (await panel.locator('table tbody tr').first().innerText().catch(() => '')).replace(/\s+/g, ' ')
  check(cmpHead.join('/') === '模型版本/评分 · 快速评测/读音准确/音色相似', `${mode}：分数对比标明评测集`, cmpHead.join(' / '))
  check(/ \d+ \d+% \d+%$/.test(cmpRow), `${mode}：分数对比显示评分、读音准确、音色相似`, cmpRow)
  if (mobile) await page.setViewportSize({ width: 390, height: 1600 })
  await shot('models-detail-evalset')
  if (mobile) { await page.setViewportSize({ width: 390, height: 844 }); await tap(page.locator('.modal').getByRole('button', { name: '完成' })) }

  // ───────── 盲听列表与新建
  await nav('盲听')
  await page.waitForSelector('text=新建盲听')
  await page.waitForTimeout(1200)
  await shot('blind-list')
  await tap(page.getByRole('button', { name: '＋ 新建盲听' }))
  await page.waitForSelector('text=组合 A')
  await page.locator('select[aria-label="角色"]').selectOption('alice')
  await page.waitForFunction(() => document.querySelectorAll('.blind-side select[aria-label="模型版本"] optgroup').length > 0, null, { timeout: 15_000 })
  const side = (label) => page.locator('.blind-side', { hasText: `组合 ${label}` })
  const sideValue = (label, select) => side(label).locator(`select[aria-label="${select}"]`).inputValue()
  check(await sideValue('A', '模型版本') === recommended.id && await sideValue('A', '引擎') === A.engine, `${mode}：A 默认选推荐的模型版本`, `${recommended.label} · ${displayEngine(recommended.engine)}`)
  check(await sideValue('A', 'GPT') === A.gpt && await sideValue('A', 'SoVITS') === A.sovits && `${A.gpt} + ${A.sovits}` === `${alice.engines[A.engine].default_gpt} + ${alice.engines[A.engine].default_sovits}`,
    `${mode}：A 的两半是 alice ${A.engine} 默认组合`, `${A.gpt} + ${A.sovits}`)
  check(await sideValue('B', '模型版本') === bDefaultVersion.id, `${mode}：B 默认选另一个模型版本（只有一个时与 A 相同）`, `${bDefaultVersion.label} · ${versions.length} 个版本`)
  const groups = await side('A').locator('select[aria-label="模型版本"] optgroup').evaluateAll((gs) => gs.map((g) => `${g.label}: ${[...g.children].map((o) => o.textContent).join(', ')}`))
  const scoreNote = (await side('A').locator('.version-picker .field-note').first().innerText().catch(() => '')).trim()
  const setName = { hard: '难句评测', mini: '快速评测' }[recommended.score_set] ?? '尚未评测'
  check(groups.some((g) => g.startsWith(`${setName}: `) && g.includes(`${recommended.label} · ${displayEngine(recommended.engine)} · 推荐`))
    && (recommended.score == null || scoreNote === `${setName} · 评分 ${Math.round(recommended.score)}`),
  `${mode}：模型版本按评测集分组，标出推荐和评分`, `${groups.join(' | ')} · ${scoreNote}`)
  const gptOpts = await side('A').locator('select[aria-label="GPT"] option').allTextContents()
  check(gptOpts.some((t) => /GPT 第 \d+ 轮/.test(t)), `${mode}：「更多设置」的权重下拉带训练轮数`, gptOpts.join(' | '))
  // B 改成 v2ProPlus：「更多设置」里换引擎，再挑 GPT / SoVITS（底模）
  await tap(side('B').locator('summary', { hasText: '更多设置' }))
  await side('B').locator('select[aria-label="引擎"]').selectOption(B.engine)
  await side('B').locator('select[aria-label="GPT"]').selectOption(B.gpt)
  await side('B').locator('select[aria-label="SoVITS"]').selectOption(B.sovits)
  check(await sideValue('B', '模型版本') === '' && await side('B').locator('select[aria-label="模型版本"] option:checked').innerText() === '更多设置中的自定组合',
    `${mode}：B 在「更多设置」里换成 v2ProPlus 后显示自定组合`, `${B.gpt} + ${B.sovits}`)
  const evalSel = page.locator('.blind-wrap select[aria-label="评测集"]')
  check(await evalSel.inputValue() === 'hard' && (await evalSel.locator('option:checked').innerText()).startsWith('难句评测'), `${mode}：评测集默认取 default=true 的难句评测`)
  await evalSel.selectOption('mini')
  const title = `zz-test-blind-${mode}-${Date.now().toString(36)}`
  await page.getByPlaceholder('可选标题').fill(title)
  check((await pageText()).includes('1 次 · 共 3 题'), `${mode}：快速评测 × 1 = 3 题`)
  await page.waitForFunction(() => /^自动：在 \S+ 上出题 · 约 \d+ 分钟/.test(document.querySelector('.blind-wrap .auto-sum')?.textContent ?? ''), null, { timeout: 30_000 })
  const blindSummary = await page.locator('.blind-wrap .auto-sum').innerText()
  check(/^自动：在 studio-host 上出题 · 约 \d+ 分钟/.test(blindSummary) && await page.getByRole('button', { name: '取消', exact: true }).isVisible(), `${mode}：新建页有算力摘要、「取消」和「创建并开始生成」`, blindSummary)
  await shot('blind-new', mobile)
  const createRes = page.waitForResponse((r) => r.url().endsWith('/blind') && r.request().method() === 'POST')
  await tap(page.getByRole('button', { name: '创建并开始生成' }))
  const res = await createRes
  const test = await res.json()
  if (test.id) created.add(test.id)
  check(res.ok() && test.status === 'generating' && test.questions?.length === 3, `${mode}：创建成功并进入生成`, test.id ?? JSON.stringify(test))
  const sent = JSON.parse(res.request().postData() ?? '{}')
  check(sent.a.engine === A.engine && sent.a.version === A.version && sent.b.engine === B.engine && sent.b.gpt === B.gpt && sent.b.sovits === B.sovits && !sent.b.version
    && sent.eval_set === 'mini' && sent.repeats === 1 && sent.character === 'alice' && sent.title === title && sent.approved === false && !('compute' in sent),
  `${mode}：请求体正确`, JSON.stringify(sent))
  check((await page.locator('.sheet').count()) === 0, `${mode}：只有本机时不弹花费确认窗`)
  const q = (i) => test.questions[i].id

  // ───────── 做题：生成中
  await card().waitFor()
  check(await card().innerText().then((t) => t.includes('第 1 / 3 题')), `${mode}：进入做题，停在第 1 题`)
  const generatingVisible = await page.locator('.blind-card .warn').isVisible()
  if (!mobile) {
    // 状态栏任务菜单显示「盲听生成」
    const jobsBtn = page.locator('.jobs-menu > button')
    await page.waitForFunction(() => /^[1-9]\d* 个任务运行中/.test(document.querySelector('.jobs-menu > button')?.textContent ?? ''), null, { timeout: 20_000 })
    await jobsBtn.click()
    check((await page.locator('.jobs-menu .menu-list').innerText()).includes('盲听生成'), 'desktop：状态栏任务列表显示「盲听生成」', (await page.locator('.jobs-menu .menu-list').innerText()).replace(/\n/g, ' | '))
  }
  check(generatingVisible, `${mode}：未就绪的题显示「生成中」`)
  check(await card().getByRole('button', { name: '▶ 播放左' }).isDisabled(), `${mode}：未就绪时播放按钮禁用`)
  await noLeak('生成中界面')
  await shot('blind-generating')
  if (!mobile) await page.locator('.jobs-menu > button').click()

  // 等第 1 题就绪：不能自动出声
  const playsBefore = await plays()
  await page.waitForFunction(() => !document.querySelector('.blind-card .warn'), null, { timeout: 480_000 })
  await page.waitForTimeout(3500)
  check(await plays() === playsBefore, `${mode}：生成完成不自动播放`)
  await noLeak('可做题界面')
  await shot('blind-ready')

  // 播放
  await tap(card().getByRole('button', { name: '▶ 播放左' }))
  await page.waitForTimeout(600)
  check((await lastPlay()).includes(`/blind/${test.id}/questions/${q(0)}/left`) && (await lastPlay()).includes('format=mp3'), `${mode}：点「播放左」播放左边 mp3`, await lastPlay())
  if (!mobile) {
    await page.keyboard.press('2')
    await page.waitForTimeout(400)
    check((await lastPlay()).includes(`/${q(0)}/right`), 'desktop：按 2 播右')
    await page.keyboard.press('ArrowLeft')
    await page.waitForTimeout(400)
    check((await lastPlay()).includes(`/${q(0)}/left`), 'desktop：按 ← 播左')
    await page.keyboard.press('ArrowRight')
    await page.waitForTimeout(400)
    check((await lastPlay()).includes(`/${q(0)}/right`), 'desktop：按 → 播右')
  } else {
    await tap(card().getByRole('button', { name: '▶ 播放右' }))
    await page.waitForTimeout(400)
    check((await lastPlay()).includes(`/${q(0)}/right`), 'mobile：点「播放右」')
  }

  // 投票：第 1 题左更好 → 自动跳到第 2 题，不播放
  const p1 = await plays()
  await tap(card().getByRole('button', { name: /^左更好/ }))
  await page.waitForFunction(() => document.querySelector('.blind-card')?.textContent?.includes('第 2 / 3 题'))
  await page.waitForTimeout(800)
  check(await plays() === p1, `${mode}：投票后跳到下一题，不自动播放`)
  check((await api('GET', `/blind/${test.id}`)).json.questions[0].vote === 'left', `${mode}：第 1 题已记为 left`)

  // 回到第 1 题看已投状态，再改票
  if (mobile) await tap(card().getByRole('button', { name: '上一题' }))
  else await page.keyboard.press('[')
  await page.waitForFunction(() => document.querySelector('.blind-card')?.textContent?.includes('第 1 / 3 题'))
  check((await card().innerText()).includes('已投：左更好'), `${mode}：已投的题显示当前选择`)
  await noLeak('已投界面')
  await shot('blind-voted')
  if (mobile) await tap(card().getByRole('button', { name: /^差不多/ }))
  else await page.keyboard.press('s')
  await page.waitForFunction(() => document.querySelector('.blind-card')?.textContent?.includes('第 2 / 3 题'))
  check((await api('GET', `/blind/${test.id}`)).json.questions[0].vote === 'tie', `${mode}：第 1 题改票为 tie`)

  // 第 2 题：备注框聚焦时按键只打字；带备注投右
  await page.waitForFunction(() => !document.querySelector('.blind-card .warn'), null, { timeout: 480_000 })
  const noteBox = card().getByPlaceholder('备注（可选，200 字内）')
  if (!mobile) {
    await noteBox.click()
    await page.keyboard.type('ads 尾音')
    check(await noteBox.inputValue() === 'ads 尾音', 'desktop：备注框获得焦点时 A/S/D 正常输入')
    check((await api('GET', `/blind/${test.id}`)).json.questions[1].vote === null, 'desktop：备注框里按键不投票')
    await card().locator('.blind-text').click()
    await page.keyboard.press('d')
  } else {
    await noteBox.fill('ads 尾音')
    await tap(card().getByRole('button', { name: /^右更好/ }))
  }
  await page.waitForFunction(() => document.querySelector('.blind-card')?.textContent?.includes('第 3 / 3 题'))
  const q2 = (await api('GET', `/blind/${test.id}`)).json.questions[1]
  check(q2.vote === 'right' && q2.note === 'ads 尾音', `${mode}：第 2 题带备注投 right`, JSON.stringify({ vote: q2.vote, note: q2.note }))

  // 第 3 题不投，提前揭晓
  await tap(page.getByRole('button', { name: '提前揭晓…' }))
  await page.waitForSelector('.modal.confirm')
  await shot('blind-reveal-confirm')
  await tap(page.locator('.modal.confirm').getByRole('button', { name: '揭晓' }))
  await page.waitForSelector('.blind-cards')
  await page.waitForTimeout(800)
  const resultText = await pageText()
  check(resultText.includes(displayEngine(A.engine)) && resultText.includes(displayEngine(B.engine)), `${mode}：揭晓后显示 A、B 的引擎`)
  check(/胜率/.test(resultText) && /自动分均值/.test(resultText), `${mode}：结果卡显示胜率和自动分均值`)
  check(resultText.includes('左边是'), `${mode}：逐题标出左边是 A 还是 B`)
  const result = (await api('GET', `/blind/${test.id}/result`)).json
  check(result.n_voted === 2, `${mode}：提前揭晓时已投 2 题`)

  // 结果页改票：给第 3 题投票，结果跟着变（第 3 题还在生成时按钮先禁用，等它就绪）
  const q3 = page.locator('.blind-rq').nth(2)
  const q3Left = q3.locator('.segmented').getByRole('button', { name: '左更好' })
  await q3Left.scrollIntoViewIfNeeded()
  await page.waitForFunction(() => { const b = [...document.querySelectorAll('.blind-rq')][2]?.querySelector('.segmented button'); return b && !b.disabled }, null, { timeout: 480_000 })
  await tap(q3Left)
  await page.waitForFunction(() => document.body.innerText.includes('已投 3/3'), null, { timeout: 20_000 })
  check((await api('GET', `/blind/${test.id}/result`)).json.n_voted === 3, `${mode}：结果页改票后结果更新为 3 题`)
  const pr = await plays()
  await tap(page.locator('.blind-rq').first().getByRole('button', { name: '▶ 播放 A' }))
  await page.waitForTimeout(500)
  check(await plays() === pr + 1 && (await lastPlay()).includes('format=mp3'), `${mode}：结果页可分别播放 A/B`, await lastPlay())
  await page.evaluate(() => document.querySelector('.content').scrollTo(0, 0))
  await shot('blind-result', true)

  // 回列表看揭晓后的摘要，然后删除
  await tap(page.getByRole('button', { name: '返回列表' }))
  const item = page.locator('.blind-item', { hasText: title })
  await item.waitFor()
  await page.waitForFunction((t) => [...document.querySelectorAll('.blind-item')].some((e) => e.textContent.includes(t) && e.textContent.includes('A 胜')), title, { timeout: 20_000 })
  const itemText = await item.innerText()
  check(itemText.includes('已揭晓') && /A 胜 \d+ 负 \d+ 平 \d+/.test(itemText) && itemText.includes(`A：${displayEngine(A.engine)} · `) && itemText.includes(`B：${displayEngine(B.engine)} · `) && itemText.includes('alice · 快速评测 · '),
    `${mode}：列表显示揭晓结果与 A/B 组合、评测集`, itemText.replace(/\n/g, ' | '))
  await shot('blind-list-revealed')
  await tap(item.getByRole('button', { name: '删除' }))
  await tap(page.locator('.modal.confirm').getByRole('button', { name: '删除' }))
  await item.waitFor({ state: 'detached' })
  check((await api('GET', `/blind/${test.id}`)).status === 404, `${mode}：删除后 GET /blind/{id} 返回 404`)
  created.delete(test.id)

  check(!metaCalls.length, `${mode}：全程没有调用 /audio/{id}/meta`, metaCalls.join(', '))
  check(!errors.length, `${mode}：没有页面异常`, errors.join(' / '))
  await ctx.close()
}

/** 只在浏览器里模拟的盲听（不写服务端）：列表里自动标题的显示；生成失败时的「补生成」。 */
async function mocked() {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  const page = await ctx.newPage()
  await blockTraining(page)
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  const base = { created: '2026-10-24T15:00:00', character: 'alice', eval_set: 'mini', repeats: 1, ref_id: 'alice-00000010', a: null, b: null, n_voted: 0, revealed: false }
  const failed = { ...base, id: 'zz-test-mock-failed', title: 'zz-test-mock-failed', status: 'failed', job_id: 'blind-zz-test-mock-1', n_questions: 3, n_ready: 1 }
  const legacy = { ...base, id: 'zz-test-mock-legacy', title: 'alice v2pro:secret-gpt.ckpt+secret-sovits.pth vs v2proplus:s1v3.ckpt+s2Gv2ProPlus.pth', status: 'ready', job_id: 'j1', n_questions: 3, n_ready: 3 }
  // core 不填标题时自动起的标题（core/ttsapi/blind.py：f"{character} · {eval_set} · {%Y-%m-%d %H:%M}"）
  const auto = { ...base, id: 'zz-test-mock-auto', title: 'alice · mini · 2026-10-24 15:00', status: 'ready', job_id: 'j2', n_questions: 3, n_ready: 3 }
  const empty = { ...base, id: 'zz-test-mock-empty', title: '', status: 'ready', job_id: 'j3', n_questions: 3, n_ready: 3 }
  const questions = [0, 1, 2].map((i) => ({ id: `q0${i + 1}`, index: i, text: `模拟题目 ${i + 1}`, tests: [], ready: i === 0, left_url: i === 0 ? '/blind/zz-test-mock-failed/questions/q01/left' : null, right_url: i === 0 ? '/blind/zz-test-mock-failed/questions/q01/right' : null, vote: null, note: '' }))
  let regenerated = 0
  const json = (route, body) => route.fulfill({ json: body })
  await page.route((u) => u.pathname === '/blind', (route) => (route.request().method() === 'GET' ? json(route, { total: 4, tests: [failed, legacy, auto, empty] }) : route.abort()))
  await page.route('**/blind/zz-test-mock-failed', (route) => json(route, { ...failed, ...(regenerated ? { status: 'generating', job_id: 'blind-zz-test-mock-2' } : {}), questions }))
  await page.route('**/blind/zz-test-mock-failed/generate', (route) => { regenerated++; return json(route, { ...failed, status: 'generating', job_id: 'blind-zz-test-mock-2', questions }) })
  await page.route('**/jobs/blind-zz-test-mock-2', (route) => json(route, { id: 'blind-zz-test-mock-2', kind: 'blind', target: failed.id, status: 'succeeded', progress: 1, stage: '', created: base.created, log_tail: [], completed: [] }))
  await page.goto(BASE + 'blind')
  await page.waitForSelector('.blind-item')
  const titles = await page.locator('.blind-item .strong').allInnerTexts()
  console.log('模拟列表标题：', titles.join(' | '))
  check(titles[1] === 'alice · 快速评测 · 2026-10-24' && !(await page.locator('body').innerText()).includes('secret-gpt'), '标题（模拟）：旧版自动标题（含组合）改显示「alice · 快速评测 · 日期」，不泄露模型', titles[1])
  check(titles[3] === 'alice · 快速评测 · 2026-10-24', '标题（模拟）：没有标题时显示「alice · 快速评测 · 日期」', titles[3])
  check(/^alice · 快速评测 · 2026-10-24( 15:00)?$/.test(titles[2]), '标题（模拟）：core 自动起的标题也按「alice · 快速评测 · 日期」显示', `core 标题 ${auto.title} → 显示 ${titles[2]}`)
  // 生成失败：能听已生成的题，提供「补生成」
  await page.locator('.blind-item', { hasText: 'zz-test-mock-failed' }).click()
  await page.waitForSelector('.blind-card')
  const failText = await page.locator('.blind-wrap').innerText()
  check(failText.includes('生成失败') && failText.includes('点「补生成」补齐剩下的') && await page.getByRole('button', { name: '补生成' }).isVisible(), '补生成（模拟）：生成失败时显示原因和「补生成」')
  await page.getByRole('button', { name: '补生成' }).click()
  await page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('已开始补生成'), null, { timeout: 10_000 })
  await page.waitForFunction(() => document.querySelector('.blind-head .badge')?.textContent?.startsWith('生成中'), null, { timeout: 10_000 })
  check(regenerated === 1 && (await page.getByRole('button', { name: '补生成' }).count()) === 0, '补生成（模拟）：点击后 POST /blind/{id}/generate 一次，状态变为生成中', `POST ${regenerated} 次`)
  await page.screenshot({ path: `${SHOTS}desktop-blind-regenerate.png` })
  check(!errors.length, '模拟：没有页面异常', errors.join(' / '))
  await ctx.close()
}

try {
  for (const mode of MODES.length ? MODES : ['desktop', 'mobile']) await run(mode)
  if (!MODES.length || MODES.includes('desktop')) await mocked()
} catch (e) {
  log(false, '流程异常', e.stack ?? String(e))
} finally {
  for (const id of created) {
    const res = await api('DELETE', `/blind/${id}`)
    console.log(`清理 ${id}: HTTP ${res.status}`)
  }
  const left = (await api('GET', '/blind?limit=500')).json.tests.filter((t) => t.title.startsWith('zz-test'))
  check(!left.length, 'GET /blind 没有 zz-test 残留', left.map((t) => t.id).join(', '))
  await browser.close()
}
const failedChecks = results.filter((r) => !r.ok)
console.log(`\n${results.length - failedChecks.length}/${results.length} 通过 · 用时 ${Math.round((Date.now() - started) / 1000)} 秒`)
process.exit(failedChecks.length ? 1 : 0)
