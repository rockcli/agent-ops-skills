# GitLab 评论与邮件发送

仅在当前任务需要发布/发送时读取。先完成内容与接收方核对，已有明确授权不重复询问。

## GitLab：精确定位、去重、回读

优先可用的 GitLab connector 或已认证的 `glab api`，无需读取 token。先确认 hostname/project，读取：

- `projects/<encoded-project-path>/merge_requests/<iid>`：获取数字项目 ID、作者、最新 `sha` 与 `diff_refs`。
- `projects/<id>/merge_requests/<iid>/discussions`：分页读完已有评论，识别同一 finding 是否已发出。

发布使用 JSON 文件，不把多行 Markdown、反引号或 `$()` 插入 shell 字符串。示例 payload：

```json
{
  "body": "**R1 · P1：具体问题**\n\n触发条件、影响、归因、证据及验收。\n\n<!-- mr-review:project-iid:head:R1 -->",
  "position": {
    "position_type": "text",
    "base_sha": "取自当前 diff_refs",
    "start_sha": "取自当前 diff_refs",
    "head_sha": "已复核的当前 head",
    "old_path": "old/repository/path",
    "new_path": "new/repository/path",
    "new_line": 123
  }
}
```

核实评论行确实在此 MR diff；重命名要用正确 old/new path。旧侧删除行使用 `old_line`。无法可靠定位时发布带固定 SHA 源码链接的普通 discussion，不伪造行号。

```sh
glab api --hostname <host> projects/<id>/merge_requests/<iid>/discussions \
  --method POST --header 'Content-Type: application/json' --input <payload.json>
```

`--input` 时显式指定 JSON Content-Type；否则可能返回 HTTP 415。总评同样用 discussions 接口，省略 `position`。建议总評最后发出并关联逐条讨论。

每次写入前重新读 MR head，确认仍是已审查版本。每条使用与项目/MR/head/finding 绑定的 marker；发送前读完已有讨论查重，保存 note/discussion ID 及内容摘要到本次任务 evidence。已存在但内容不一致时先对账，不自动重复发送或覆盖作者之后的讨论。网络错误后先回读，不因本地没有收到 ID 就再次 POST。

发布后回读正文、position/head/new_line，返回 `MR_URL#note_<note-id>`。只 claim 实际发出的条数，不把 HTTP 成功、queued 或本地文件生成当成评论已发布。

私有 GitLab 请求出现代理 EOF 时，先检查单次请求的访问路径。若直连适用，可仅对这次调用移除 HTTP(S)/ALL_PROXY；保留 NO_PROXY/no_proxy，不改全局网络配置。

## 邮件：草稿与身份

收件人按本次指令确定。MR 作者公开邮箱、候选提交作者邮箱可作佐证，但提交 author/committer 可能与 MR 发起人不同；地址冲突时询问，不按名字或公司域名猜测。没有收件人也可先发已授权的 MR 评论。

写入本次任务的 `draft.json`，不放进技能目录，也不写凭据：

```json
{
  "to": "verified-author@example.com",
  "subject": "后续类似功能开发的几点通用原则",
  "body": "称呼、简短背景、少量通用原则及结束语。"
}
```

邮件 helper 使用 Python 标准库，发送账户配置只从当前进程环境读取：

| 环境变量 | 用途 |
|---|---|
| `ali_mail_from` | From 与 SMTP 登录账户 |
| `ali_mail_key` | SMTP 密码/授权码；只在实际发送时读取 |
| `ali_mail_smtp_host` | SMTP 主机 |
| `ali_mail_smtp_port` | 465 使用隐式 TLS，其余端口要求 STARTTLS |

检查时只输出变量名和 set/empty；发件人、主机、端口可用于核对，绝不输出 key，不打印完整环境，不把 key 写进脚本、命令参数、配置或报告。不设置 SMTP debug 日志。若当前环境没有这些变量，报告缺失名称；不要无授权转去扫描别处的凭据文件。

```sh
# 仅预览，不连接 SMTP，不需要 key。
python3 <skill-dir>/scripts/send_mail.py --draft <draft.json>

# 仅在本次用户已明确授权发送时使用；max-chars 默认 500，遵从用户其他限制。
python3 <skill-dir>/scripts/send_mail.py --draft <draft.json> \
  --send --receipt <task-evidence>/email-receipt.json
```

helper 将收件人、主题、内容摘要、Message-ID 和阶段状态写入回执，不含 key 或完整正文。首次创建回执是互斥的；已存在时拒绝再次发信。始终为同一次逻辑发送复用同一路径，不通过换文件名绕过保护。

- `smtp_accepted`：SMTP 已接受；不等于收件箱送达或已读。退出连接失败也不能触发重发。
- `failed_before_send` / `rejected`：未进入发送或得到明确拒绝；检查原因并确认没有接受后，才可准备新的有记录尝试。
- `sending` / `outcome_unknown`：可能已发出；保留 Message-ID，先核对服务器/已发送记录。无法核对时告诉用户发送结果未知，不自动重发。

技能开发和测试时只使用 fake SMTP/mocked transport。创建技能不授权再向任何真实作者发送演示邮件。
