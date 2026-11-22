import { execFileSync } from 'node:child_process'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 构建产物由主控的 core 在 /ui/ 下提供，接口走同源（/health、/plan …）。
// 开发时 /ui/ 以外的路径都转发到一台开发 core（默认 127.0.0.1:9982，用 TTS_CORE 换）。
const core = process.env.TTS_CORE ?? 'http://127.0.0.1:9982'
const proxy = { '^/(?!ui(/|$))': { target: core, changeOrigin: true } }

export default defineConfig({
  base: '/ui/',
  define: { __TTS_VERSION__: JSON.stringify(execFileSync('bash', ['../mac/script/version.sh'], { encoding: 'utf8' }).trim()) },
  plugins: [react()],
  server: { host: true, proxy },
  preview: { proxy },
})
