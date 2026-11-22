// 版本：网页跟着主控更新，设置里只显示版本和「检查更新」，没有更新按钮。全部接口用假数据（mock-core），不碰真实服务。
// 用法：node e2e/versions.mjs [base]
import { chromium, expect } from '@playwright/test'
import assert from 'node:assert/strict'
import { mockCore } from './mock-core.mjs'

const base = process.argv[2] ?? 'http://127.0.0.1:5173/ui/'
const browser = await chromium.launch()
try {
  for (const mobile of [false, true]) {
    const page = await browser.newPage({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 } })
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    let refreshed = 0
    let failing = false
    const core = await mockCore(page, {
      overrides: {
        'GET /system/versions': (req, world, url) => {
          if (url.searchParams.get('refresh')) refreshed++
          return failing ? { status: 502, body: { detail: 'GitHub timed out' } } : { body: world.systemVersions }
        },
      },
    })
    // 主控查到更新的版本：设置旁有红点，「新版本」，提示在主控上更新后刷新网页
    core.world.systemVersions.studio.latest = '9.9.9'
    await page.goto(base + 'settings/general')
    await expect(page.locator('.update-dot')).toBeVisible()
    await expect(page.getByText('新版本 9.9.9', { exact: true })).toBeVisible()
    await expect(page.getByText('网页跟着主控更新：在主控的 Studio 里更新后，刷新这个网页。', { exact: true })).toBeVisible()
    await expect(page.getByRole('link', { name: '更新说明' })).toHaveAttribute('href', 'https://example.com/releases/0.2.0')
    assert.equal(await page.getByRole('button', { name: /更新服务端|更新 Mac|更新 Web|立即更新/ }).count(), 0)
    // 检查更新：带 refresh 重新查；主控上的版本不比网页新时显示「已是最新」，红点消失
    core.world.systemVersions.studio.latest = '0.0.1'
    await page.getByRole('button', { name: '检查更新', exact: true }).click()
    await expect.poll(() => refreshed).toBe(1)
    await expect(page.getByText('已是最新', { exact: true })).toBeVisible()
    await expect(page.locator('.update-dot')).toHaveCount(0)
    // 查不到：给一句提示，不暴露原话
    failing = true
    await page.getByRole('button', { name: '检查更新', exact: true }).click()
    await expect(page.getByText('暂时查不到新版本', { exact: true })).toBeVisible()
    assert.ok(!(await page.locator('body').innerText()).includes('GitHub timed out'))
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '没有横向滚动')
    assert.deepEqual(errors, [])
    assert.deepEqual(core.unexpected, [])
    console.log(`${mobile ? 'mobile' : 'desktop'}: update-dot / newer / refresh / up-to-date / error / no update buttons PASS`)
    await page.close()
  }
} finally {
  await browser.close()
}
