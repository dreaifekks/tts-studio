// 只读冒烟：逐页打开、收集控制台错误并截图。用法：node e2e/smoke.mjs <base> <outdir>
import { chromium } from '@playwright/test'
const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const out = process.argv[3] ?? '/tmp/tts-shots'
const browser = await chromium.launch()
for (const [name, viewport] of [['desktop', { width: 1440, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 2, isMobile: name === 'mobile', hasTouch: name === 'mobile' })
  page.on('console', (m) => { if (m.type() === 'error') console.log(`[${name}] console:`, m.text()) })
  page.on('pageerror', (e) => console.log(`[${name}] pageerror:`, e.message))
  for (const p of ['', 'refs', 'history', 'stats', 'works', 'training', 'models', 'blind', 'settings/general', 'settings/library', 'settings/compute', 'settings/cloud']) {
    await page.goto(base + p)
    await page.waitForTimeout(2500)
    await page.screenshot({ path: `${out}/${name}-${(p || 'generate').replace('/', '-')}.png` })
    console.log(name, p || 'generate', 'ok', (await page.locator('.statusbar').innerText()).replace(/\n/g, ' | '))
  }
  await page.close()
}
await browser.close()
