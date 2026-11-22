// 第二轮验收：模型页、引擎补全、排序、清除评价。
// 用法：node e2e/acceptance-r2.mjs [pre|post] [base] [shotDir]
//   pre  ：不需要推理，训练期间可跑
//   post ：训练结束后：用快速评测（mini）评测一个组合、v2ProPlus 零样本出候选、清除评价
// 改动的权重元数据和默认组合测完恢复原值；不测删除权重；POST /train、/train/sweep 一律拦截。
import { chromium } from '@playwright/test'
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const args = process.argv.slice(2)
const PHASE = args.find((a) => a === 'pre' || a === 'post') ?? 'pre'
const BASE = args.find((a) => /^https?:\/\//.test(a)) ?? 'http://127.0.0.1:5173/ui/'
const ORIGIN = new URL(BASE).origin
const SHOTS = (args.find((a) => a.startsWith('/')) ?? mkdtempSync(join(tmpdir(), 'tts-studio-screens-'))).replace(/\/?$/, '/')
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
const weight = async (id) => (await api('GET', `/weights/${encodeURIComponent(id)}`)).json
const erinDefaults = async () => {
  const c = (await api('GET', '/models')).json.characters.find((x) => x.slug === 'erin').engines.v2pro
  return `${c.default_gpt} + ${c.default_sovits}`
}
const erinVersions = async () => (await api('GET', '/characters/erin/versions')).json
/** 新 core：「设为默认组合」记成手动默认（版本列表里叫「我设的默认」）；没有手动默认时自动用评分最高的组合。 */
const erinManual = async () => {
  const v = (await erinVersions()).find((x) => x.engine === 'v2pro' && x.label === '我设的默认')
  return v ? `${v.gpt} + ${v.sovits}` : null
}
/** 去掉本轮留下的手动默认（PATCH is_default=false 只清掉和这一半相同的手动记录），回到自动默认。 */
async function clearErinManual() {
  const own = (await api('GET', '/weights?character=erin&engine=v2pro')).json.weights
  for (const w of own) await api('PATCH', `/weights/${encodeURIComponent(w.id)}`, { is_default: false, character: 'erin' })
}
const displayEngine = (e) => ({ v2pro: 'v2Pro', v2proplus: 'v2ProPlus' })[e] ?? e
const parseSize = (t) => { const [n, u] = t.trim().split(/\s+/); return Number(n) * (u === 'GB' ? 1024 : 1) }

// 测试对象：鏡（erin）v2pro 的随附模型。原默认组合和它是手动还是自动，开跑时读下来，结束时按原样恢复
const E5 = 'v2pro:erin-e5.ckpt'
const ORIGINAL_DEFAULT = await erinDefaults()
const ORIGINAL_MANUAL = await erinManual()

const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
const trainPosts = []
for (const p of ['**/train', '**/train/sweep']) await page.route(p, (route) => { trainPosts.push(route.request().url()); return route.abort() })   // 绝不提交训练
await page.route('**/weights/*', (route) => (route.request().method() === 'DELETE' ? route.abort() : route.continue()))   // 不测删除
const nav = (t) => page.locator('nav a', { hasText: t }).click()
const shot = (name) => page.screenshot({ path: `${SHOTS}${name}.png` })
const waitStatus = (re, timeout = 300_000) =>
  page.waitForFunction((src) => new RegExp(src).test(document.querySelector('.statusbar')?.textContent ?? ''), re.source, { timeout })
const waitConnected = () => page.waitForFunction(() => document.querySelector('.statusbar')?.textContent?.includes('已连接'), null, { timeout: 60_000 })
/** 模型页的操作结果显示在表格上方的提示行里（不再写状态栏）。 */
const waitNotice = (re, timeout = 30_000) =>
  page.waitForFunction((src) => new RegExp(src).test(document.querySelector('.notice')?.textContent ?? ''), re.source, { timeout })
const patched = () => page.waitForResponse((r) => r.url().includes('/weights/') && r.request().method() === 'PATCH', { timeout: 15_000 })
const editor = () => page.locator('.weight-editor')
const pairPanel = () => page.locator('.pair-panel')
const filter = (label) => page.locator(`.toolbar select[aria-label="${label}"]`)
const row = (name) => page.locator('tr.clickable', { has: page.locator(`td[title="${name}"]`) })
const scoreSet = () => page.getByRole('radiogroup', { name: '评分评测集' })
const reviewed = new Set()

try {
  await page.goto(BASE)
  await waitConnected()

  if (PHASE === 'pre') {
    // ───────── 引擎补全：引擎只读显示，选择收进「更多设置」
    const models = (await api('GET', '/models')).json
    const engines = models.engines
    await page.getByRole('button', { name: /更多设置/ }).click()
    const more = page.locator('.more-settings')
    await page.waitForFunction(() => [...document.querySelectorAll('.more-settings select[aria-label="引擎"] option')].some((o) => o.textContent.includes('零样本')))
    const engineOpts = await more.locator('select[aria-label="引擎"] option').allInnerTexts()
    const expected = engines.map((e) => [displayEngine(e.id), e.zero_shot && '零样本', e.trainable && '可训练'].filter(Boolean).join(' · '))
    check(JSON.stringify(engineOpts) === JSON.stringify(expected) && engineOpts.includes('v2ProPlus · 零样本 · 可训练'), '生成：「更多设置」的引擎列表来自 /models，带零样本 / 可训练标记', engineOpts.join(' | '))
    const versionSel = page.locator('select[aria-label="模型版本"]')
    // 版本列表在 /models 之后才读：等它选上（生产构建加载快，引擎列表出来时版本可能还没回来）
    await page.waitForFunction(() => document.querySelector('select[aria-label="模型版本"]')?.value, null, { timeout: 15_000 }).catch(() => {})
    check((await page.locator('.field-value').first().innerText()).startsWith('v2Pro') && (await versionSel.inputValue()) !== '', '生成：引擎只读显示，跟着所选模型版本', await page.locator('.field-value').first().innerText())
    const gptOptions = async () => more.locator('select[aria-label="GPT"] option').evaluateAll((os) => os.map((o) => ({ value: o.value, text: o.textContent })))
    const names = async (q) => (await api('GET', `/weights?hidden=false&${q}`)).json.weights.filter((w) => w.type === 'gpt').map((w) => w.name).sort()
    const weightsReq = (engine) => page.waitForResponse((r) => r.url().includes('/weights?') && r.url().includes('source=pretrained') && r.url().includes(`engine=${engine}&`))
    let wr = weightsReq('v2proplus')
    await more.locator('select[aria-label="引擎"]').selectOption('v2proplus')
    await wr
    await page.waitForTimeout(500)
    let opts = await gptOptions()
    const ownPlus = await names('character=alice&engine=v2proplus')
    const basePlus = await names('source=pretrained&engine=v2proplus')
    check(ownPlus.length === 0 && opts[0].value === '' && opts[0].text === '默认组合' && JSON.stringify(opts.slice(1).map((o) => o.value).sort()) === JSON.stringify(basePlus)
      && (await versionSel.locator('option:checked').innerText()) === '更多设置中的自定组合' && (await page.locator('.field-value').first().innerText()).startsWith('v2ProPlus'),
    '生成：alice · v2ProPlus 没有自己的模型时只列底模，模型版本显示「更多设置中的自定组合」', opts.map((o) => o.text).join(' | '))
    wr = weightsReq('v2pro')
    await more.locator('select[aria-label="引擎"]').selectOption('v2pro')
    await wr
    await page.waitForTimeout(500)
    opts = await gptOptions()
    const v2proNames = [...new Set([...(await names('character=alice&engine=v2pro')), ...(await names('source=pretrained&engine=v2pro'))])].sort()
    check(opts[0].text === '默认组合' && JSON.stringify(opts.slice(1).map((o) => o.value).sort()) === JSON.stringify(v2proNames) && opts.some((o) => /GPT 第 \d+ 轮/.test(o.text)),
      '生成：v2Pro 的 GPT 选项来自 /weights（本角色 + 底模），首项「默认组合」，带训练轮数', opts.map((o) => o.text).join(' | '))
    await nav('训练')
    await page.waitForSelector('select[aria-label="引擎"]')
    const trainable = engines.filter((e) => e.trainable).map((e) => e.id)
    const trainOpts = await page.locator('select[aria-label="引擎"]').locator('option').evaluateAll((os) => os.map((o) => o.value))
    check(JSON.stringify(trainOpts) === JSON.stringify(trainable), '训练：引擎只列 trainable', trainOpts.join(','))

    // ───────── 模型页：排序、过滤
    await nav('模型')
    await page.waitForSelector('tr.clickable')
    const sizeHeader = page.locator('th.sortable button', { hasText: '大小' })
    const sortReq = page.waitForRequest((r) => r.url().includes('/weights?') && r.url().includes('sort=size') && r.url().includes('order=desc'))
    await sizeHeader.click()
    await sortReq
    const ascReq = page.waitForRequest((r) => r.url().includes('/weights?') && r.url().includes('sort=size') && r.url().includes('order=asc'))
    await sizeHeader.click()
    await ascReq
    await page.waitForTimeout(500)
    const sizes = (await page.locator('tr.clickable td:nth-child(9)').allInnerTexts()).map(parseSize)
    check(sizes.length > 3 && sizes.every((v, i) => i === 0 || v >= sizes[i - 1]) && sizes.at(-1) > sizes[0] && (await page.locator('th.sortable.active').innerText()).includes('大小 ↑'),
      '模型：点表头排序（大小升序）', `${sizes.length} 行，${sizes[0]} MB → ${sizes.at(-1)} MB`)
    const epochRes = page.waitForResponse((r) => r.url().includes('/weights?') && r.url().includes('sort=epoch') && r.url().includes('order=asc'))
    await page.locator('select[aria-label="排序"]').selectOption('epoch')
    const epochs = (await (await epochRes).json()).weights.map((w) => w.epoch).filter((e) => e != null)
    check(epochs.length > 3 && epochs.every((v, i) => i === 0 || v >= epochs[i - 1]), '模型：排序菜单（训练轮数 升序，请求带 sort=epoch）', epochs.slice(0, 8).join(','))
    await page.locator('select[aria-label="排序"]').selectOption('created_at')
    await filter('引擎').selectOption('v2pro')
    await filter('来源').selectOption('july')
    await filter('角色').selectOption('erin')
    await page.waitForFunction(() => document.querySelectorAll('tr.clickable').length === 5)
    const cells = await page.locator('tr.clickable').evaluateAll((rs) => rs.map((r) => [r.children[1].textContent, r.children[2].textContent, r.children[4].textContent]))
    check(cells.every(([c, e, s]) => c === '鏡' && e === 'v2Pro' && s === '随附模型'), '模型：按角色、引擎、来源过滤', `${cells.length} 行`)

    // ───────── 改名 / 备注（测完改回）
    const before = await weight(E5)
    await row('erin-e5.ckpt').click()
    await editor().getByLabel('显示名').fill('zz-test-鏡e5')
    await editor().getByLabel('备注').fill('zz-test 备注')
    let saved = patched()
    await editor().getByRole('button', { name: '保存名称 / 备注' }).click()
    await saved
    await waitNotice(/模型信息已保存/)
    let w = await weight(E5)
    check(w.display_name === 'zz-test-鏡e5' && w.note === 'zz-test 备注', '模型：改显示名、写备注', `${w.display_name} / ${w.note}`)
    await page.waitForSelector('tr.clickable td[title="erin-e5.ckpt"]:has-text("zz-test-鏡e5")')
    check(true, '模型：表格显示新的显示名')
    await shot('desktop-models-detail')

    // ───────── 隐藏 / 取消隐藏
    saved = patched()
    await editor().getByRole('button', { name: '隐藏', exact: true }).click()
    await saved
    await waitNotice(/^已隐藏$/)
    await page.waitForFunction(() => !document.querySelector('tr.clickable td[title="erin-e5.ckpt"]'))
    check((await weight(E5)).hidden === true, '模型：隐藏后从默认列表消失')
    await page.getByLabel('显示已隐藏').check()
    await page.waitForSelector('tr.clickable.dim td[title="erin-e5.ckpt"]')
    await row('erin-e5.ckpt').click()
    saved = patched()
    await editor().getByRole('button', { name: '取消隐藏' }).click()
    await saved
    await waitNotice(/已取消隐藏/)
    check((await weight(E5)).hidden === false, '模型：「显示已隐藏」里取消隐藏')
    await page.getByLabel('显示已隐藏').uncheck()

    // ───────── 设默认（成对）后改回：在「默认 / 评测组合」里，点一行定 GPT，「更多设置」里挑 SoVITS
    const [origGpt, origSovits] = ORIGINAL_DEFAULT.split(' + ')
    const testGpt = ['erin-e10.ckpt', 'erin-e5.ckpt', 'erin-e15.ckpt'].find((n) => n !== origGpt)
    const testSovits = ['erin_e4_s948.pth', 'erin_e8_s1896.pth'].find((n) => n !== origSovits)
    check(/^erin-e\d+\.ckpt \+ erin_e\d+_s\d+\.pth$/.test(ORIGINAL_DEFAULT), '模型：鏡 v2pro 原默认组合', `${ORIGINAL_DEFAULT}（${ORIGINAL_MANUAL ? '手动设的' : '自动'}）`)
    await pairPanel().locator('summary', { hasText: '更多设置' }).click()
    const setPair = async (gptRow, sovitsId) => {
      await row(gptRow).click()
      await page.waitForFunction((id) => document.querySelector('.pair-panel select[aria-label="GPT"]')?.value === id, `v2pro:${gptRow}`, { timeout: 15_000 })
      await pairPanel().locator('select[aria-label="SoVITS"]').selectOption(sovitsId)
    }
    await setPair(testGpt, `v2pro:${testSovits}`)
    check(await pairPanel().locator('select[aria-label="模型版本"]').inputValue() === '' && (await pairPanel().locator('.field-value').innerText()) === 'v2Pro',
      '模型：点一行后「默认 / 评测组合」切成自定组合（鏡 · v2Pro）')
    await pairPanel().getByRole('button', { name: '设为默认组合' }).click()
    await waitNotice(/已设为 鏡 \/ v2Pro 的默认组合/)
    check(await erinDefaults() === `${testGpt} + ${testSovits}` && await erinManual() === `${testGpt} + ${testSovits}`, '模型：成对设为默认组合（记为手动默认「我设的默认」）', await erinDefaults())
    const defaultCell = (names) => page.evaluate((ns) => [...document.querySelectorAll('tr.clickable')].filter((r) => ns.includes(r.children[0].title)).map((r) => `${r.children[0].title}=${r.children[9].textContent}`).sort().join(' '), names)
    await page.waitForFunction((n) => [...document.querySelectorAll('tr.clickable')].some((r) => r.children[0].title === n && r.children[9].textContent === '是'), testGpt, { timeout: 15_000 }).catch(() => {})
    check(await defaultCell([testGpt, origGpt]) === [`${testGpt}=是`, `${origGpt}=—`].sort().join(' '), '模型：表格「默认」列跟着更新', await defaultCell([testGpt, origGpt]))
    await setPair(origGpt, `v2pro:${origSovits}`)
    await pairPanel().getByRole('button', { name: '设为默认组合' }).click()
    for (let i = 0; i < 40 && await erinDefaults() !== ORIGINAL_DEFAULT; i++) await page.waitForTimeout(250)
    check(await erinDefaults() === ORIGINAL_DEFAULT, '模型：默认组合改回原值', await erinDefaults())
    // 「设为默认组合」总是记成手动默认：原来是自动默认的话，把手动记录清掉
    if (!ORIGINAL_MANUAL) await clearErinManual()
    check(await erinDefaults() === ORIGINAL_DEFAULT && await erinManual() === ORIGINAL_MANUAL, '清理：鏡 v2pro 回到原来的默认（手动 / 自动状态也恢复）', `${await erinDefaults()}（${(await erinManual()) ? '手动' : '自动'}）`)

    // 分数对比：同角色同引擎的版本评测（开发库里蓮 · v2ProPlus 有快速评测）
    await filter('来源').selectOption('')
    await filter('角色').selectOption('dave')
    await filter('引擎').selectOption('v2proplus')
    await scoreSet().getByRole('radio', { name: '快速评测' }).click()
    await page.waitForFunction(() => document.querySelector('.weights-table th.sortable.num')?.textContent?.includes('快速评测') && document.querySelectorAll('tr.clickable').length > 0)
    await page.locator('tr.clickable').first().click()
    const cmp = pairPanel().locator('table')
    await cmp.locator('tbody tr').first().waitFor({ timeout: 15_000 })
    const cmpHead = await cmp.locator('th').allInnerTexts()
    const cmpRow = await cmp.locator('tbody tr').first().innerText()
    check(cmpHead.join('/') === '模型版本/评分 · 快速评测/读音准确/音色相似' && /\d+\s+\d+%\s+\d+%/.test(cmpRow), '模型：同角色同引擎的分数对比（标明评测集）', cmpRow.replace(/\s+/g, ' '))
    await scoreSet().getByRole('radio', { name: '难句评测' }).click()
    await page.setViewportSize({ width: 390, height: 844 })
    await page.waitForSelector('.weight-card')
    if (await page.locator('.modal').count()) await page.keyboard.press('Escape')   // 桌面选中的行在手机上以弹窗打开
    await shot('mobile-models')
    await page.locator('.weight-card').first().click()
    await page.waitForSelector('.modal .weight-editor')
    await shot('mobile-models-detail')
    await page.keyboard.press('Escape')
    const mobileSort = page.waitForRequest((r) => r.url().includes('/weights?') && r.url().includes('sort=size'))
    await page.locator('select[aria-label="排序"]').selectOption('size')
    await mobileSort
    check((await page.locator('.modal').count()) === 0, '模型：手机卡片列表、详情弹窗 + 排序菜单')
    await page.locator('select[aria-label="排序"]').selectOption('created_at')
    await page.setViewportSize({ width: 1440, height: 900 })
    await filter('角色').selectOption('')
    await filter('引擎').selectOption('')
    await page.waitForTimeout(800)
    await shot('desktop-models')

    // 在界面上把显示名清空：应发空字符串，服务端才会清除
    await filter('引擎').selectOption('v2pro')
    await filter('角色').selectOption('erin')
    await page.waitForFunction(() => document.querySelectorAll('tr.clickable').length === 5)
    await row('erin-e5.ckpt').click()
    await editor().getByLabel('显示名').fill('')
    await editor().getByLabel('备注').fill('')
    const clearReq = page.waitForRequest((r) => r.url().includes('/weights/') && r.method() === 'PATCH')
    saved = patched()
    await editor().getByRole('button', { name: '保存名称 / 备注' }).click()
    const clearBody = (await clearReq).postDataJSON()
    await saved
    for (let i = 0; i < 20 && (await weight(E5)).display_name; i++) await page.waitForTimeout(250)
    check(!(await weight(E5)).display_name && clearBody.display_name === '', '模型：清空显示名（发送空字符串）', JSON.stringify(clearBody))
    await filter('角色').selectOption('')
    await filter('引擎').selectOption('')
    // 恢复名称和备注
    await api('PATCH', `/weights/${encodeURIComponent(E5)}`, { display_name: before.display_name ?? '', note: before.note })
    w = await weight(E5)
    check((w.display_name || null) === (before.display_name || null) && w.note === before.note && !w.hidden, '清理：鏡 e5 的显示名、备注、隐藏恢复原值')

    // ───────── 各列表排序（记住选择）
    const sortCase = async (navTitle, sortValue, urlPart, name) => {
      await nav(navTitle)
      const req = page.waitForRequest((r) => r.url().includes(urlPart) && r.url().includes(`sort=${sortValue}`), { timeout: 20_000 })
      await page.locator('select[aria-label="排序"]').first().selectOption(sortValue)
      await req
      const orderReq = page.waitForRequest((r) => r.url().includes(urlPart) && r.url().includes(`sort=${sortValue}`) && r.url().includes('order='), { timeout: 20_000 })
      await page.locator('.order-btn').first().click()
      await orderReq
      await page.reload()
      await waitConnected()
      await page.waitForSelector('select[aria-label="排序"]')
      check(await page.locator('select[aria-label="排序"]').first().inputValue() === sortValue, `排序：${name}（请求带 sort/order，刷新后仍记住）`)
    }
    await sortCase('历史', 'score', '/history?', '历史')
    await sortCase('参考库 / Tag', 'duration', '/refs?', '参考库')
    await sortCase('作品', 'title', '/works?', '作品列表')
    await sortCase('训练', 'status', '/jobs?', '训练任务（/jobs?kind=train）')
    await nav('历史')
    const reviewsReq = page.waitForRequest((r) => r.url().includes('/reviews?') && r.url().includes('sort=reviewed_at'))
    await page.locator('select[aria-label="最低分"]').selectOption('1')
    await page.locator('select[aria-label="排序"]').selectOption('reviewed_at')
    await reviewsReq
    check(true, '排序：review 列表（/reviews）')
    await page.locator('.order-btn').click()   // 倒序：最近评价的在前
    await page.waitForTimeout(800)
    if ((await page.locator('.order-btn').innerText()).includes('升序')) { await page.locator('.order-btn').click(); await page.waitForTimeout(800) }

    await shot('desktop-history')
    await page.setViewportSize({ width: 390, height: 844 })
    await shot('mobile-history')
    await page.setViewportSize({ width: 1440, height: 900 })
    // 恢复默认排序（本浏览器上下文内）
    await page.evaluate(() => { for (const k of ['history', 'refs', 'works', 'training', 'models']) localStorage.removeItem(`sort:${k}`) })
  } else {
    // ───────── 训练结束后：v2ProPlus 零样本出候选（「更多设置」里换引擎，GPT / SoVITS 用默认组合）
    const health = (await api('GET', '/health')).json
    check(!health.training_job, '训练已结束', String(health.training_job))
    const plus = (await api('GET', '/models')).json.characters.find((c) => c.slug === 'alice').engines.v2proplus
    await page.getByRole('button', { name: /更多设置/ }).click()
    await page.locator('.more-settings select[aria-label="引擎"]').selectOption('v2proplus')
    await page.waitForTimeout(1200)
    for (let i = 0; i < 4; i++) await page.getByRole('button', { name: '减少候选' }).click()
    const genReq = page.waitForRequest((r) => r.url().endsWith('/tts/candidates/jobs') && r.method() === 'POST')
    await page.getByRole('button', { name: '出候选' }).click()
    const genBody = (await genReq).postDataJSON()
    await waitStatus(/已生成 2 个候选/)
    const items = await page.locator('.candidate').count()
    const engineText = await page.locator('.candidate').first().innerText()
    const [c1, c2] = await page.locator('.candidate').evaluateAll((els) => els.map((e) => e.getAttribute('data-id')))
    const m1 = (await api('GET', `/audio/${c1}/meta`)).json
    check(items === 2 && engineText.includes('alice · v2ProPlus') && genBody.engine === 'v2proplus' && !genBody.version && !genBody.gpt && !genBody.sovits && m1.gpt === plus.default_gpt && m1.sovits === plus.default_sovits,
      'v2ProPlus 零样本出候选（默认组合 = 底模）', `${engineText.split('\n').find((l) => l.includes('v2ProPlus'))} · ${m1.gpt} + ${m1.sovits}`)
    // 清除评价：先打分再清除，生成页和历史各清一次，不留数据
    for (const id of [c1, c2]) {
      reviewed.add(id)
      await page.locator(`.candidate[data-id="${id}"]`).getByRole('button', { name: '打 2 分' }).click()
      await page.waitForFunction((x) => document.querySelectorAll(`.candidate[data-id="${x}"] .star.on`).length === 2, id)
    }
    await page.waitForTimeout(800)
    const cleared = (id) => page.waitForResponse((r) => r.url().includes(`/audio/${id}/review`) && r.request().method() === 'DELETE', { timeout: 15_000 })
    let del = cleared(c1)
    await page.locator(`.candidate[data-id="${c1}"]`).getByRole('button', { name: '清除评价' }).click()
    await del
    await waitStatus(/评价已清除/)
    check(!(await api('GET', `/audio/${c1}/meta`)).json.review && await page.locator(`.candidate[data-id="${c1}"] .star.on`).count() === 0, '清除评价：生成页候选', c1)
    await nav('历史')
    await page.locator('select[aria-label="最低分"]').selectOption('0')
    await page.locator('select[aria-label="评价"]').selectOption('true')
    const hcard = page.locator(`.candidate[data-id="${c2}"]`)
    await hcard.waitFor({ timeout: 20_000 })
    del = cleared(c2)
    await hcard.getByRole('button', { name: '清除评价' }).click()
    await del
    check(!(await api('GET', `/audio/${c2}/meta`)).json.review, '清除评价：历史卡片', c2)
    await page.locator('select[aria-label="评价"]').selectOption('')
    await nav('生成')
    await shot('desktop-generate-zeroshot')
    await page.setViewportSize({ width: 390, height: 844 })
    await page.locator('.candidates-col').scrollIntoViewIfNeeded()
    await shot('mobile-generate-zeroshot')
    await page.setViewportSize({ width: 1440, height: 900 })
    // 清掉零样本候选不需要：没有 review、不在作品里的音频按 7 天规则自动清理

    // ───────── 对一个组合跑评测（鏡 v2pro e10 + e4，快速评测，结果写回表格）
    await nav('模型')
    await filter('角色').selectOption('erin')
    await filter('引擎').selectOption('v2pro')
    await scoreSet().getByRole('radio', { name: '快速评测' }).click()
    await page.waitForFunction(() => document.querySelectorAll('tr.clickable').length === 5 && document.querySelector('.weights-table th.sortable.num')?.textContent?.includes('快速评测'))
    await pairPanel().locator('summary', { hasText: '更多设置' }).click()
    await row('erin-e10.ckpt').click()
    await page.waitForFunction(() => document.querySelector('.pair-panel select[aria-label="GPT"]')?.value === 'v2pro:erin-e10.ckpt', null, { timeout: 15_000 })
    await pairPanel().locator('select[aria-label="SoVITS"]').selectOption('v2pro:erin_e4_s948.pth')
    await pairPanel().locator('select[aria-label="评测使用"]').selectOption('mini')
    await page.waitForFunction(() => /^自动：在 \S+ 上评测 · 约 \d+ 分钟/.test(document.querySelector('.pair-panel .auto-sum')?.textContent ?? ''), null, { timeout: 30_000 })
    const evalSummary = await pairPanel().locator('.auto-sum').innerText()
    check(/^自动：在 studio-host 上评测 · 约 \d+ 分钟/.test(evalSummary) && await pairPanel().locator('select[aria-label="模型版本"]').inputValue() === '', '模型：评测前显示算力摘要（自定组合，快速评测）', evalSummary)
    const manualBeforeEval = await erinManual()
    const defaultBeforeEval = await erinDefaults()
    // 同一组合重复评测会替换旧记录，按评测时间判断是否写回
    const evalBefore =(await weight('v2pro:erin-e10.ckpt')).evals.find((e) => e.sovits === 'erin_e4_s948.pth' && e.eval_set === 'mini')?.evaluated_at ?? ''
    const evalReq = page.waitForRequest((r) => new URL(r.url()).pathname === '/weights/eval' && r.method() === 'POST')
    await pairPanel().getByRole('button', { name: '评测', exact: true }).click()
    const evalBody = (await evalReq).postDataJSON()
    check(evalBody.character === 'erin' && evalBody.engine === 'v2pro' && evalBody.gpt === 'erin-e10.ckpt' && evalBody.sovits === 'erin_e4_s948.pth' && evalBody.eval_set === 'mini' && evalBody.version === null && !('compute' in evalBody),
      '模型：评测请求体（自定组合、快速评测、自动选设备）', JSON.stringify(evalBody))
    await pairPanel().locator('.job-progress').waitFor({ timeout: 30_000 })
    check((await page.locator('.sheet').count()) === 0, '模型：只有本机时评测不弹花费确认窗')
    await page.waitForTimeout(4000)
    await shot('desktop-models-eval')
    await waitNotice(/快速评测 组合评测完成/, 540_000)
    const after = await weight('v2pro:erin-e10.ckpt')
    const fresh = after.evals.find((e) => e.sovits === 'erin_e4_s948.pth' && e.eval_set === 'mini')
    check(fresh && fresh.evaluated_at > evalBefore, '模型：组合评测（SSE 进度，结果写回）', JSON.stringify(fresh && { score: fresh.score_median, cer: fresh.cer_median, sim: fresh.spk_sim_median }))
    await page.waitForFunction(() => !document.querySelector('tr.clickable td[title="erin-e10.ckpt"]')?.parentElement?.children[5]?.textContent?.startsWith('—'), null, { timeout: 15_000 })
    const scoreCell = await row('erin-e10.ckpt').locator('td:nth-child(6)').innerText()
    check(!scoreCell.startsWith('—'), '模型：表格显示新评测分（评分 · 快速评测）', scoreCell)
    // 分数对比随评测完成刷新（异步重新取 /weights 和版本）。这一行按模型版本命名：组合是某个版本时叫版本名，否则拼「GPT … 第 10 轮 · SoVITS … 第 4 轮」
    const asVersion = (await erinVersions()).find((v) => v.gpt === 'erin-e10.ckpt' && v.sovits === 'erin_e4_s948.pth')
    const expectLabel = asVersion ? `${asVersion.label} · v2Pro` : null
    const expectScore = String(Math.round(fresh?.score_median ?? NaN))
    const rowMatches = ([label, score]) => [...document.querySelectorAll('.pair-panel table tbody tr')].some((r) => {
      const [name, s] = [...r.children].map((c) => c.textContent)
      return s === score && (label ? name === label : name.includes('第 10 轮') && name.includes('第 4 轮'))
    })
    await page.waitForFunction(rowMatches, [expectLabel, expectScore], { timeout: 20_000 }).catch(() => {})
    const cmpRows = (await pairPanel().locator('table tbody tr').allInnerTexts()).map((t) => t.replace(/\s+/g, ' '))
    check(await page.evaluate(rowMatches, [expectLabel, expectScore]),
      '模型：分数对比里出现新组合（按模型版本命名，分数一致）', `期望「${expectLabel ?? 'GPT … 第 10 轮 · SoVITS … 第 4 轮'}」${expectScore}；实际 ${cmpRows.join(' / ')}`)
    await pairPanel().locator('table').scrollIntoViewIfNeeded().catch(() => {})
    await shot('desktop-models-eval-done')
    // 新 core：没有手动默认时，默认组合自动取评分最高的组合，评测可能让它变化；评测本身不会设手动默认，也不会改已有的手动默认
    const manualAfterEval = await erinManual()
    const defaultAfterEval = await erinDefaults()
    check(manualAfterEval === manualBeforeEval && (!manualBeforeEval || defaultAfterEval === defaultBeforeEval),
      '评测没有设成 / 改动手动默认组合', `评测前 ${defaultBeforeEval}（${manualBeforeEval ? '手动' : '自动'}）→ 评测后 ${defaultAfterEval}（${manualAfterEval ? '手动' : '自动'}）`)
  }
  check(trainPosts.length === 0, '没有提交训练', trainPosts.join(', '))
  check(errors.length === 0, '无页面错误', errors.join(' | '))
} catch (e) {
  log(false, '流程中断', e.stack?.split('\n').slice(0, 3).join(' '))
  await page.screenshot({ path: '/tmp/tts-r2-fail.png' }).catch(() => {})
} finally {
  // 兜底恢复：鏡的默认组合（含手动 / 自动状态）与 e5 元数据、本轮打过的分。post 的评测会改评分，没有手动默认时自动默认可能随之变化，不算残留
  if (PHASE === 'pre' && (await erinManual() !== ORIGINAL_MANUAL || await erinDefaults() !== ORIGINAL_DEFAULT)) {
    if (ORIGINAL_MANUAL) {
      const [g, s] = ORIGINAL_MANUAL.split(' + ')
      await api('PATCH', `/weights/${encodeURIComponent(`v2pro:${g}`)}`, { is_default: true, character: 'erin' })
      await api('PATCH', `/weights/${encodeURIComponent(`v2pro:${s}`)}`, { is_default: true, character: 'erin' })
    } else await clearErinManual()
    log(await erinDefaults() === ORIGINAL_DEFAULT && await erinManual() === ORIGINAL_MANUAL, '清理：恢复鏡的默认组合', await erinDefaults())
  }
  const e5 = await weight(E5)
  if (e5.display_name || e5.note || e5.hidden) {
    await api('PATCH', `/weights/${encodeURIComponent(E5)}`, { display_name: '', note: '', hidden: false })
    log(true, '清理：恢复鏡 e5 元数据')
  }
  for (const id of reviewed) {
    if ((await api('GET', `/audio/${id}/meta`)).json?.review) log((await api('DELETE', `/audio/${id}/review`)).status < 300, `清理：删除 ${id} 的评价`)
  }
  await browser.close()
  const failed = results.filter((r) => !r.ok)
  console.log(`\n${PHASE}：${results.length - failed.length}/${results.length} 通过 · 用时 ${Math.round((Date.now() - started) / 1000)} 秒`)
  process.exitCode = failed.length ? 1 : 0
}
