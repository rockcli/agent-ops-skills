# 来源

上游：[ARIS Claude Review MCP](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep/tree/43a5daf6f8a2db7b87b48cb51d45c6ddb3c9a6d3/mcp-servers/claude-review)。
固定提交 `43a5daf6f8a2db7b87b48cb51d45c6ddb3c9a6d3`，MIT 许可证随 vendor/LICENSE 保存。
vendor/aris_review.py 是未修改的上游源码，本地适配器复用其 Claude JSON 解析、模型报告和 MCP framing。

本地 scripts/opus.py 补齐：冻结 Git 范围和源码、effort、文件锁与原子状态、PID 身份核对及取消、
worker 丢失检测、通知去重标记、CLI 备用入口、实施任务入口、无提示词协议日志和 restricted/safe-mode 工具边界。
自动化由 Codex 原生工具创建，运行器不直接修改应用数据库或注入聊天消息。
这是固定上游基础上的本地适配版本，不声称全部功能均为上游原生能力；不会自动更新上游。
