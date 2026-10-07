# 架构图、图源与校验

三幅图均由经过本项目空白行格式修复的 Archify 3.0.1 局部运行副本，以 `architecture` 类型、`showcase` 质量档生成。它们解释设计与证据边界，**不是本仓库已经运行完整微信助手的证明**。

| 图 | 内容边界 | 图源 | 可打开的产物 |
| --- | --- | --- | --- |
| [现有试验机制](../.archify/architecture-current-mechanism-20261007-144533/current-mechanism.html) | 概括此前私有 Mac 测试中用过的窗口核验、局部截图、本机 OCR、当前对话模型、受控输入及发送观察；没有持续服务或稳定消息 API | [`candidate.json`](../.archify/architecture-current-mechanism-20261007-144533/candidate.json) | [`current-mechanism.html`](../.archify/architecture-current-mechanism-20261007-144533/current-mechanism.html) |
| [目标系统概念](../.archify/architecture-target-system-20261007-144533/target-system.html) | 展示拟建的本机适配器、串行执行、策略/模型接口、outbox 与人工接手；其中大部分仍待实现 | [`candidate.json`](../.archify/architecture-target-system-20261007-144533/candidate.json) | [`target-system.html`](../.archify/architecture-target-system-20261007-144533/target-system.html) |
| [单群渠道设计](../.archify/architecture-single-group-channel-20261007-152801/single-group-channel.html) | 本期目标：分页与确认日志、CLI、现有 Agent、串行发送和结果核验；不是已连通的运行图 | [`candidate.json`](../.archify/architecture-single-group-channel-20261007-152801/candidate.json) | [`single-group-channel.html`](../.archify/architecture-single-group-channel-20261007-152801/single-group-channel.html) |

[脱敏校验清单](diagram-verification.json)保存三组相对路径、字节数、SHA-256、格式修复脚本哈希与四项门禁结果。本地 `finalize` 的 `validate`、`deliver`、`check`、`browser-check` 均通过，零诊断；随后对这些新产物完成严格来源绑定的 `visual-check`，当前机制图 2048 像素浅色截图与目标图 2048 像素深色截图，以及单群图 2048 像素浅色截图已人工查看。原始浏览器回执及截图保留在被忽略的本地资料中，不随公开仓库发布。`scripts/check_diagrams.py` 只比对公开图源、HTML、准备脚本与该清单的静态哈希及元数据；CI **不重新运行 Archify、浏览器或视觉审阅**。

要修改图，先编辑对应 `candidate.json`。Archify 3.0.1 的原始渲染器会在生成 HTML 的空白行留下行尾空格；本项目用 [`scripts/prepare_archify.py`](../scripts/prepare_archify.py) **一次性**将已安装的 3.0.1 运行依赖复制到被忽略的 `.local/archify-runtime/`，只在该副本的 `applyTemplate` 结果写入和哈希计算之前清除纯空白行的空格与制表符。校验器、交付流程和浏览器门禁不改，最终公开 HTML 是完整 `finalize` 四项严格门禁检查过的字节，**不是交付后手改**。源 CLI 与全局技能均不修改、不注册；脚本要求准确的 3.0.1 版本，且已有本地副本时拒绝覆盖。

在全新工作区，从仓库根目录设置 `ARCHIFY_CLI` 为已安装的 Archify 3.0.1 `bin/archify.mjs` 入口，再运行准备命令。下方 `finalize` 使用项目局部副本；改动已有浏览器回执的图时，每图选择一个未使用过的 `--out-dir`，保留旧证据归属。目录名中的时间字符串只作示例，运行前换成当前修订值。

```sh
python3 scripts/prepare_archify.py --cli "$ARCHIFY_CLI"

node .local/archify-runtime/bin/archify.mjs finalize architecture \
  .archify/architecture-current-mechanism-20261007-144533/candidate.json \
  .archify/architecture-current-mechanism-20261007-144533/current-mechanism.html \
  --out-dir .archify/architecture-current-mechanism-20261007-144533/rebuild-YYYYMMDD-HHMMSS \
  --quality showcase --json

node .local/archify-runtime/bin/archify.mjs finalize architecture \
  .archify/architecture-target-system-20261007-144533/candidate.json \
  .archify/architecture-target-system-20261007-144533/target-system.html \
  --out-dir .archify/architecture-target-system-20261007-144533/rebuild-YYYYMMDD-HHMMSS \
  --quality showcase --json

node .local/archify-runtime/bin/archify.mjs finalize architecture \
  .archify/architecture-single-group-channel-20261007-152801/candidate.json \
  .archify/architecture-single-group-channel-20261007-152801/single-group-channel.html \
  --out-dir .archify/architecture-single-group-channel-20261007-152801/rebuild-YYYYMMDD-HHMMSS \
  --quality showcase --json
```

三份图源均未设置 `meta.repository`：现有机制图概括旧私有试验，目标图是概念设计，不把本公开仓库的代码映射为图中已实现证据。原始回执和截图继续留在忽略范围内。

每次改动后，应重新完成本地四项门禁和实际视觉检查，再把新的公开图源、HTML 与脱敏校验清单一并更新。只有旧哈希匹配的 CI 结果不代表新图经过视觉验收。[仓库状态](status.md)另列离线代码与真实微信测试的完成边界。
