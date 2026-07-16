# VideoRecover 设计说明

日期：2026-07-17  
状态：已批准  
目标仓库：`codesfly/video_recover`

## 1. 目标

构建一个在 Apple Silicon Mac 上长期运行的本地抖音视频归档工具。用户可以通过 Web、REST API、Codex 或 Claude Desktop 提交单个公开抖音视频链接；系统解析最高可用画质并下载到本地，同时保存发布描述，并从音频生成 TXT、SRT 和按段落整理的 Markdown 文案。

系统以 Docker 作为常驻控制层，并通过 macOS 原生 MLX Worker 使用 Metal 加速 Whisper。全部任务、文件和配置都必须在重启后保留。用户只应用它下载自己拥有权利或获准保存的内容。

## 2. 已确认的产品决定

- 支持公开链接；遇到登录或风控限制时允许用户在 Web 中粘贴 Cookie 后重试。
- 文案包含发布标题/描述和语音识别结果。
- 输出页面文本、TXT、SRT 和 Markdown。Markdown 只整理断句与段落，不改写原意。
- 目标机器为 Apple M5、32 GB 内存、ARM64 macOS。
- 选择“Docker 控制层 + macOS 原生 MLX Worker”架构。
- Codex 使用 Streamable HTTP MCP；Claude Desktop 使用本地 stdio 桥接。
- Cookie 设置、读取和文件删除不暴露给 Agent。
- 默认只监听 `127.0.0.1`，不向局域网开放。

## 3. 系统架构

### 3.1 Docker 控制层

一个 ARM64 兼容镜像承载以下组件：

- FastAPI Web 与 REST API；
- Streamable HTTP MCP 和 stdio MCP 入口；
- SQLite 任务与配置存储；
- 单并发持久任务调度器；
- 抖音解析器适配层；
- 断点续传下载器；
- 容器 CPU Whisper 回退；
- 静态管理界面。

Docker Compose 将 Web/MCP 映射到 `127.0.0.1:8787`，将数据目录映射到 Mac 上的绝对目录，并使用 `restart: unless-stopped`。

### 3.2 macOS 原生 MLX Worker

原生 Worker 以用户级 LaunchAgent 运行，不开放监听端口。它通过带随机令牌的本机 HTTP 长轮询主动领取转写租约，读取与 Docker 共享的下载文件，使用量化 `large-v3-turbo` 模型转写，并提交分段和时间戳结果。

Worker 遵循以下资源策略：

- 同时只转写一个任务；
- 首个任务到达时才加载模型；
- 空闲 10 分钟后卸载模型；
- 使用后台进程优先级和低优先级 I/O；
- 睡眠、崩溃或退出后由租约超时回收任务；
- 原生 Worker 长时间离线后允许容器 CPU 回退。

### 3.3 客户端入口

- Web：人类管理、Cookie、重试、删除、预览和复制文案。
- REST：Web 与自动化共用的稳定接口。
- Codex：`http://127.0.0.1:8787/mcp` 的 Streamable HTTP MCP。
- Claude Desktop：本地 stdio 进程通过 `docker compose exec -T` 使用同一业务层。

所有入口共享同一个任务数据库，不创建平行状态。

## 4. 模块边界

### 4.1 Domain

定义视频任务、状态转换、解析结果、转写分段和错误分类。Domain 不依赖 FastAPI、SQLite、yt-dlp 或 MLX。

### 4.2 Repository

负责 SQLite 模式、事务、去重、租约、心跳、重启恢复、设置与 Cookie 密文。数据库使用 WAL 和外键。

### 4.3 Parser adapters

统一接口接收标准化抖音 URL 和可选 Cookie，返回视频 ID、作者、描述、时长、封面和候选媒体地址。

解析顺序：

1. `yt-dlp` 提取器；
2. 专用抖音解析适配器；
3. 两者都失败时映射为可操作错误。

解析器升级不得影响任务、Web 或 MCP 接口。

### 4.4 Downloader

流式写入 `.part`，支持 Range/断点续传、超时、大小上限和原子改名。下载完成后再推进任务状态。

### 4.5 Transcription

转写提供统一 Provider 接口：MLX 远程租约 Provider 和容器 CPU Provider。两者输出相同的时间分段数据，由独立 formatter 生成 TXT、SRT 和 Markdown。

### 4.6 Application service

编排提交、解析、下载、转写、重试、删除和查询。Web、REST、MCP 和 Worker API 只能调用该服务，不能直接操作数据库或文件。

## 5. 数据流与状态机

标准状态：

`queued → resolving → downloading → awaiting_transcription → transcribing → completed`

附加终态或控制状态：

- `partial`：视频和元数据已完成，但转写失败；
- `failed`：下载前失败或输出不完整；
- `cancelled`：用户在 Web 中取消；
- `deleting`：原子删除过程中的内部状态。

规则：

- 同一视频 ID 的重复提交默认返回已有任务；失败任务可以重试。
- 应用启动时，将遗留的 `resolving`、`downloading` 和无有效租约的 `transcribing` 恢复到可重试队列。
- 转写失败不得删除视频、元数据或发布描述。
- 删除只允许 Web/REST 的显式确认操作，且不作为 MCP 工具。

## 6. 持久化布局

```text
data/
├── db/video_recover.sqlite3
├── secrets/app.key
├── cache/                 # 容器模型与解析缓存
└── downloads/
    └── <aweme_id>/
        ├── video.mp4
        ├── metadata.json
        ├── description.txt
        ├── transcript.txt
        ├── transcript.srt
        └── transcript.md
```

MLX 模型缓存在 macOS 应用数据目录，由安装脚本创建并跨升级保留。

## 7. Web 体验

采用已批准的“本地媒体档案台”视觉方向：暖纸色底、墨色结构、红色状态强调和酸黄色主要动作。桌面使用左侧窄栏、主任务列表和右侧详情；移动端改为单列，不隐藏核心功能。

首页包括：

- 单链接输入与“开始解析”；
- MCP、MLX Worker 和 Cookie 状态；
- 任务历史、当前阶段和进度；
- 视频预览；
- 发布描述、TXT、SRT、Markdown 标签页；
- 复制、下载、打开目录、重试和删除；
- Cookie 设置和失效提示。

浏览器以低频轮询或服务器事件刷新状态，不阻塞提交请求。

## 8. MCP 契约

首版工具：

- `submit_video(url, transcribe=true)`：创建异步任务并返回任务 ID；
- `get_task(task_id)`：读取状态、阶段、进度和错误；
- `list_videos(status?, limit?)`：列出历史；
- `get_metadata(task_id)`：读取结构化元数据；
- `get_transcript(task_id, format)`：读取 `txt`、`srt` 或 `markdown`；
- `retry_task(task_id)`：重试失败或 partial 任务；
- `get_service_status()`：检查解析器、Worker、存储和 Cookie 状态。

MCP Server instructions 明确说明任务为异步、应先提交再轮询、不得假设已经完成。只读与写入工具设置正确的 MCP annotations。Cookie 与删除不进入 MCP。

## 9. 安全与隐私

- 只接受 `douyin.com` 及明确列入白名单的短链域名，解析重定向后再次校验，防止 SSRF。
- HTTP 与 MCP 默认绑定 `127.0.0.1`，校验 Origin。
- Worker API 使用高熵随机 Bearer Token，并限制为内部路径。
- Cookie 使用持久随机密钥加密；响应、异常和日志全部脱敏。
- 文件名由视频 ID 和受控固定名称组成，不使用未清洗的远端标题。
- 下载设超时、响应大小和可用磁盘阈值。
- MCP 不暴露任意路径读取、任意 URL 下载、Cookie 或删除能力。

## 10. 错误处理

用户可见错误分类：

- 无效或不支持的链接；
- 需要 Cookie / Cookie 已失效；
- 平台限流；
- 视频不存在、已删除或无权限；
- 解析器失配；
- 网络中断；
- 磁盘空间不足；
- Worker 离线；
- 转写或输出生成失败。

限流和暂时网络错误使用有限次数的指数退避。错误必须包含下一步动作，不显示堆栈、Cookie、签名参数或内部路径。每次阶段推进和失败都写入事件记录，方便 Web 和测试核对。

## 11. 性能策略

- Docker 与原生 Worker 都以 ARM64 原生架构运行，不使用 amd64 模拟。
- 下载和转写并发各为 1，避免抢占 Mac。
- 数据库、缓存和临时文件使用 Docker volume 或 VM 内文件系统；最终下载目录使用宿主 bind mount。
- 原生 MLX 为默认路径，容器 CPU 仅在 Worker 超时后回退。
- 模型延迟加载并在空闲后卸载；Web/MCP 常驻进程不加载模型。
- Docker 资源限制通过 `.env` 可调整，并为 32 GB M5 提供保守默认值。

## 12. 测试与验收

### 12.1 自动测试

- URL 标准化、短链重定向白名单与 SSRF 防护；
- 状态机合法/非法转换和重启恢复；
- SQLite 去重、租约、心跳和过期回收；
- Cookie 加密、解密与日志脱敏；
- 解析器成功、回退和错误映射；
- 下载断点、临时文件和原子完成；
- TXT、SRT、Markdown 格式与中文断句；
- MLX Worker 领取、心跳、提交和离线回退；
- REST 与 MCP 工具返回一致；
- 删除不在 MCP 中出现。

### 12.2 Docker 验收

- ARM64 镜像成功构建；
- `docker compose up -d` 后容器为 healthy；
- `/healthz`、Web 首页和 `/mcp` 初始化成功；
- 容器重启后任务、Cookie 状态和文件仍存在；
- Codex 能通过 Streamable HTTP 创建任务并读取结果；
- Claude Desktop stdio 配置能列出并调用同一组工具。

### 12.3 真实链接验收

使用 `https://www.douyin.com/video/7662212894569811235`：

- 下载的视频能在本地播放；
- 发布描述正确保存；
- 原生 MLX 生成 TXT、SRT 和 Markdown；
- Web 可预览、复制和下载；
- MCP 可查询任务并读取文案；
- 若公开请求受风控，Web 设置 Cookie 后重试成功；
- 日志中不出现 Cookie 或敏感令牌。

## 13. 非目标

- 不做账号主页批量抓取、评论、收藏或直播下载；
- 不做画面 OCR；
- 不做 AI 改写、摘要或内容生成；
- 不向公网或局域网提供多用户服务；
- 不绕过付费、私密或未获授权的内容限制；
- 不在 MCP 中提供删除或 Cookie 管理。

## 14. 发布要求

仓库包含源代码、测试、Dockerfile、Compose、一键启动/停止/检查脚本、macOS Worker 安装/卸载脚本、Codex/Claude Desktop MCP 配置说明和故障排查。只有在自动测试、Docker 验收和真实链接测试通过后，才能提交并推送到 `codesfly/video_recover`。
