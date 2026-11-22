# TTS Studio Web

TTS Studio 的浏览器客户端，适用于桌面和移动端。主控 Core 在 `/ui/` 提供网页，页面通过同源 API 访问主控的资料库、角色和任务。

## 本地开发

使用 Node.js 22.12 或更新版本、npm 和 Git。以下命令从仓库根目录开始。

```sh
cd web
npm ci
npm run dev
```

打开 `http://localhost:5173/ui/`。Vite 将 API 请求转发到 `http://127.0.0.1:9982`，可用环境变量指定开发主控。

```sh
TTS_CORE=http://127.0.0.1:9990 npm run dev
```

连接真实主控时，生成、评价与编辑操作会写入该主控的资料库。开发时可使用独立的 Core 和测试资料库。

## 构建

```sh
npm run typecheck
npm run lint
npm run build
```

构建产物位于 `dist/`，路径前缀为 `/ui/`，可交给主控 Core 提供。深链接（例如 `/ui/settings/compute`）由 Core 回退到 `index.html`。Web 和 [Mac 客户端](../mac/README.md) 共用 Git 版本号。API 类型来自 [OpenAPI 契约](../api/openapi.json)，契约更新后用 `npm run gen:api` 重新生成。

## 页面

- **合成**。选择角色、模型版本和参考音频，生成、比较、试听和评价候选。
- **作品**。编辑并排序台词，逐行或整篇生成，挑选候选并导出。
- **角色**。管理模型版本和默认组合，试听和标注参考音频，进行盲听，查看评价统计。
- **训练**。预览数据集过滤结果，提交训练或扫参，查看进度和评测结果。
- **设置**。查看版本和主控连接信息、资料库状态，管理局域网算力、Tailscale 设备及云端 GPU 账号。

任务界面会估算运行设备、耗时和费用。可以为本次任务更换设备，报价超出提醒线时需要确认。⌘↩ 或 Ctrl+↩ 提交合成，⌘⌥1–4 或 Ctrl+Alt+1–4 切换主页面。

## 测试

```sh
npm run test:blind
npx playwright install chromium
```

保持 Vite 开发服务器运行，再从 `web/` 用另一个终端执行浏览器回归。下列脚本在浏览器内模拟 Core API。

```sh
node e2e/versions.mjs
node e2e/blind-nullable.mjs
node e2e/keyboard.mjs
node e2e/planner.mjs
node e2e/races.mjs
node e2e/lan-wizard.mjs
node e2e/cloud-wizard.mjs
node e2e/stage4.mjs
```

脚本默认访问 `http://127.0.0.1:5173/ui/`，也可将其他本地开发地址作为第一个参数。
