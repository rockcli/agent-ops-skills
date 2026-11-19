# 依赖与 Docker 专项分级

当 `/tmp/storage_dev_cleanup.json` 存在时读取本参考。专项扫描只提供候选证据，不提供删除授权。

## 依赖缓存

- 包管理器自己的 prune/cleanup 命令优先于文件级删除。先执行 `inspect_command` 或 dry-run，再建议 `cleanup_command`。
- 包管理器和 Docker 项在报告中必须是 command-only：即使分为 🟢，也不得生成 `trash_paths`，因此网页不会对它们提供文件级移除按钮。
- `pnpm store prune`、`uv cache prune`、pip 下载缓存和 Homebrew 旧版本清理可列为 🟢 候选，但报告中的释放量仍是上限估算。
- `uv cache prune` 前必须检查 `<uv-cache>/.lock` 的 `lsof` 持有者。存在运行中的 `uv` 服务时降为 🟡/暂缓，列出 PID、命令和父进程；不得建议 `--force`。退出终端或 Agent 可能只结束其中一部分，独立的 uvicorn 等后台服务需单独确认。
- npm 默认先 `npm cache verify`；`npm cache clean --force` 只有空间压力明显时才作为 🟡 建议。
- Cargo、Gradle、Maven、Go 的整个状态根目录不是缓存。扫描器没有选定安全 prune 命令时保持 🟡，不要生成 `trash_paths`。
- `node_modules`、`.venv`、`venv`、`.tox`、`.nox` 即使存在 lockfile 也保持 🟡。lockfile 只是可重建证据之一；还要检查仓库 dirty/worktree 状态、运行中进程、离线/私有源和项目文档。

## Docker

- `protected_by_container`：任何运行中或停止容器仍引用，🔴，不删镜像。
- `dangling_candidate`：无 tag 且无容器引用，可作为 🟢 候选；仍先展示 `docker image prune` 的影响范围。
- `unused_registry_name_unverified`：无容器引用且名称像 registry/Docker Hub 镜像，属于 🟡。本地 `RepoDigests` 只是内容身份，不证明镜像仍在远端；需要按 tag 执行只读 `docker manifest inspect`，确认当前网络、认证、平台与 registry 可用后再建议处理。
- `unused_local_or_unverified`：本地命名或来源未验证，视为本地构建候选，🔴；即使 Docker 生成了本地 `RepoDigest` 也不能证明可从远端下载。本地自建业务镜像（无公开 registry 前缀）默认落在此类。
- Build Cache 可列 🟢，但说明会增加下次构建时间。Containers 和 Volumes 永不自动 prune；volume 默认用户数据。
- `docker system df` 的 reclaimable 是 Docker 的理论上限，不等于本轮建议释放量。共享 layer 会导致逐镜像 size 相加高估，不要据此求和。
- 镜像盘点必须用 container inspect 得到真实 image ID；`docker ps` 的 `.Image` 常是 tag/name，不能拿它与 image ID 直接比较。
- 有 Engine `/system/df` 证据时记录 `Size - SharedSize` 的 unique size，并按仓库/tag 家族分组；没有时明确保持“未知”，不要把虚拟大小求和。
- 删除带标签镜像前，在相关 Compose、环境变量、脚本和运行手册中搜索精确 tag。无容器引用不等于无配置引用。
- 删除镜像后重新运行 `docker system df`：共享层可能转为 build cache，只有复扫后才能给最终实际建议。
- 永远不文件级删除 `Docker.raw`、Docker Desktop VM 数据或 `~/.docker`。

## 报告要求

- 分别给出“立即安全候选”“验证后候选”“明确保护项”，不要把它们合成一个可释放数字。
- Docker 建议必须列出实际被保护的运行/停止容器数量、本地唯一镜像风险和 volume 数量。
- 所有 cleanup 命令只展示；实际执行需要用户对精确类别再次确认。
