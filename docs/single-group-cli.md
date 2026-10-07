# 单群 CLI（当前仅合成测试）

CLI 供已有 Codex 或其他 agent 调用，不启动模型服务。命令结果为一行 JSON；失败返回 `ok:false`、错误 `code` 且退出码非零。以下全部是合成名称、标签和 UUID，不能作为真实微信配置。当前未随仓库提供已校准的桌面 `layout.json`；只执行 `init` 不会让真实 `read` 或 `send` 自动可用。

```sh
PYTHONPATH=src python3.11 -m wechat_desktop_agent.cli \
  --state-dir .local/wechat-desktop-agent init \
  --group-title 'Synthetic Team' \
  --account-binding-id 'local-test-account' \
  --session-id 'synthetic-agent-session'

PYTHONPATH=src python3.11 -m wechat_desktop_agent.cli \
  --state-dir .local/wechat-desktop-agent status

PYTHONPATH=src python3.11 -m wechat_desktop_agent.cli \
  --state-dir .local/wechat-desktop-agent read \
  --session-id 'synthetic-agent-session'

PYTHONPATH=src python3.11 -m wechat_desktop_agent.cli \
  --state-dir .local/wechat-desktop-agent page \
  --session-id 'synthetic-agent-session' --delta -1
```

`init` 只允许一个群绑定，生成稳定的本地 `channel_id`，并以暂停状态开始。`account_binding_id` 是本地配置标签，不能证明微信实际登录账号；`session-id` 必须与初始化时提供的一致。配置位于权限受限的 `.local` 状态目录，不应提交到仓库。`read` 不解除暂停，也**默认不激活微信**；CLI 未暴露原生驱动的显式激活选项，微信须已在前台。`page` 只接受 `-3,-2,-1,1,2,3`，在当前已核验聊天正文区域滚动一次，同样默认不激活应用。两者的结果只有 `coverage:"viewport_only"`、原始 OCR `lines`、观察时间、窗口身份和视口哈希；行的方向为 `unknown`。它们不返回已确认消息、历史游标、订阅或完整上下文，也不把文字哈希视为消息 ID。输出可能包含聊天文字，只交给授权的本地调用者，不贴入公开日志。

`parse-copy` 是单独的**离线文本入口**：调用者先自行核对准确群、可见选中条数及剪贴板确实变化，再把这批文本送入标准输入。下面仅用合成记录演示格式；CLI 不会读取系统剪贴板或操作微信。

```sh
PYTHONPATH=src python3.11 -m wechat_desktop_agent.cli \
  --state-dir .local/wechat-desktop-agent parse-copy \
  --session-id 'synthetic-agent-session' --expected-count 2 <<'COPIED'
Alex
2026年10月07日 12:30
First synthetic request

Blair
2026年10月07日 12:31
Second synthetic request
COPIED
```

命令最多接受 20,000 字符，并核对本地单群绑定、session 和声明的条数。输出记录含 `sender_label`、`time_label`、`text` 与原顺序；还明确 `direction:"unknown"`、`native_message_id_available:false`、`source_verified:false`、`coverage:"provided_copy_only"`、`history_complete:false`。这只是解析调用者提供的一批文本，不核对复制来源、发言方向或时区，不生成事件 ID、推进游标或写入已确认的长期记录，也不会触发自动回复。错误只返回代码，不回显正文或群名；成功输出则包含所给文本，必须留在授权的本地私有环境。

只有在专用测试群的前台窗口、屏幕状态、精确标题、局部截图、草稿和发送控件均能现场核验时，才应考虑合成发送。`resume` 仅切换本地自动模式，不证明这些桌面条件已满足。`send` 从标准输入读取候选文本，必须提供调用方稳定的 UUID `request-id` 与 `--synthetic-test`；可见测试文本会自动加上该 UUID 前缀。**这不是未来客户回复的文本格式。**

```sh
PYTHONPATH=src python3.11 -m wechat_desktop_agent.cli \
  --state-dir .local/wechat-desktop-agent resume \
  --session-id 'synthetic-agent-session'

printf '%s' 'Fixed synthetic reply' | \
  PYTHONPATH=src python3.11 -m wechat_desktop_agent.cli \
    --state-dir .local/wechat-desktop-agent send \
    --session-id 'synthetic-agent-session' \
    --request-id '5d814faa-e0fe-4fe6-a6bc-e882b527da48' \
    --synthetic-test

PYTHONPATH=src python3.11 -m wechat_desktop_agent.cli \
  --state-dir .local/wechat-desktop-agent pause
```

相同 `request-id` 和正文只返回已记录的旧状态，不会再次派发；同 ID 配不同正文会拒绝。输入或点击可能已经发生但证据不足时返回不确定/需人工核对，不自动重复输入或发送。`pause` 保留已有尝试并令旧的发送授权失效。CLI 的本机界面结果不是收件端送达证明。

当前代码和离线测试尚未完成本轮真实群读写验收；原生 `layout.json`、辅助功能与屏幕录制许可、未锁屏前台窗口和客户端结构都需在授权测试环境独立核对。不要从这个示例推断可直接在客户群运行。

**显示器与读取路径：**此前本仓库原生截图路径在副屏限定标题区域取得全零像素；短暂移到主屏后虽有非零像素与一行 OCR，精确测试群标题仍未通过。新的单窗先裁剪再解码实现尚待真实标题探测。受监督 Computer Use 工具曾在副屏识别准确群标题、气泡与空草稿；用户手工框选四条合成记录后，本地复制取得原文、显示标签、分钟时间和顺序，私有样本解析通过，但程序自动框选和跨页复制未实现。这些都不是 CLI `read`、`page` 的真实验收；最新 CUA 画面又出现全白帧，已停止 GUI 动作。此前程序提交经红色失败标志及另一身份“未收到”确认为失败；用户之后手动重发，只观察到本机失败标志消失、时间变化，收件端未再次确认。当前停止新发送与程序重试，仅做只读核验。不要把任一显示器当作已校准的 CLI 运行环境；需逐项验证截图健康、窗口/标题和输入控件。详见[状态记录](status.md)。

监督试验曾用私有原生框选脚本加复制快捷键取得五条已知测试原文，并以标准输入传给 `parse-copy`；这与本命令自动读取微信是两回事。桥接系统字符串需在可信的 OS 边界转成普通 Python 字符串，再交给严格解析器。该试验预期四条却取得五条，不能据此声称选择范围已校准；也不能从复制时间头自动推定方向或完整历史。
