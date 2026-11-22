# TTS Studio for macOS

TTS Studio 是 GPT-SoVITS 的原生 SwiftUI 客户端。用它生成和试听语音、编辑多行作品、管理角色与音频资料库，以及安排训练和评测任务。

## 连接方式

- **作为主控**。在这台 Mac 安装并运行 Core。首次启动时可以从 App 安装本机主控，默认地址为 `http://127.0.0.1:9880`。在设置里选择本机或 Google Drive 资料库，管理算力和云端 GPU 账号。
- **连接到另一台**。填写现有主控的 HTTP 或 HTTPS 地址。客户端可以查看同一套角色、资料库和任务，生成与训练由主控安排。资料库文件夹在主控上选择。

## 页面和快捷键

| 页面 | 功能 |
| --- | --- |
| 合成 | 选择角色、模型版本和参考音频，生成并比较候选、试听和评分。 |
| 作品 | 编辑台词、拖动排序、生成整篇或指定行，试听并导出音频与字幕。 |
| 角色 | 管理模型版本与默认组合、试听和标注参考音频、进行盲听和查看评价统计。 |
| 训练 | 预览数据集过滤结果，提交训练和扫参，查看进度、日志与评测结果。 |

⌘↩ 提交当前合成任务，⌘⌥1–4 切换主页面。候选区取得键盘焦点后，空格播放或暂停，↑↓ 切换候选，1–5 评分。参考音频的默认标签键为 Q/W/E/R/T，可在设置中修改。

## 构建和安装

使用 Apple Silicon Mac 和支持 Swift 6 的 Xcode。当前工程的部署目标为 macOS 27.0，请选用包含相应 macOS SDK 的 Xcode。本机主控另需 Git、Xcode Command Line Tools 和 ffmpeg。只有构建可随 App 打包的 Web 界面时才需要 Node.js 和 npm。

从完整的 [TTS Studio Git 仓库](https://github.com/dreaifekks/tts-studio) 检出源码，在 `mac/` 下执行。

```sh
xcodebuild -project TTSStudio.xcodeproj -scheme TTSStudio -configuration Debug -derivedDataPath "$HOME/Library/Caches/TTSStudio/Debug.noindex" -destination 'platform=macOS' build
./script/install.sh
open /Applications/TTSStudio.app
```

也可以在 Xcode 打开 `TTSStudio.xcodeproj`，选择 `TTSStudio` scheme。安装脚本构建 Release 后替换 `/Applications/TTSStudio.app`，如有旧版正在运行，退出后重新打开。安装了 npm 时，脚本也会构建 Web 界面供主控在 `/ui/` 提供。

在「设置 → 通用」可以查看版本和检查更新。源码更新命令为 `./script/update.sh v0.x.y`，其中版本号使用 GitHub Release 的 tag。

## 测试

```sh
xcodebuild -project TTSStudio.xcodeproj -scheme TTSStudio -configuration Debug -derivedDataPath "$HOME/Library/Caches/TTSStudio/Tests.noindex" -destination 'platform=macOS' test
```

默认测试使用 XCTest 宿主，连接真实服务的验收用例需显式启用。Mac 与 [Web 客户端](../web/README.md) 共用 [OpenAPI 契约](../api/openapi.json)。
