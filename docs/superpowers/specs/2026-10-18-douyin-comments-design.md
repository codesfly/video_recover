# VideoRecover 评论快照设计说明

日期：2026-10-18  
状态：已批准（2026-10-18）  
依据：[匿名拉评论 spike](../spikes/2026-10-18-anonymous-comments-spike.md)

## 1. 目标

给已归档的抖音视频拉取一级评论，按时间点存成快照，供人在 Web 上看，也供 Agent 通过 MCP 读取。典型用途：

- 拆对标视频时，把热评当作观众反馈证据（dy-note）；
- 自己作品 T+3 复盘时重拉一次快照，对比评论区变化（cheat-retro）。

评论区一直在变（spike 中同一视频隔一天 441 → 417 条），所以每次拉取都是独立快照，不覆盖、不合并。

## 2. 已确认的产品决定

- **只拉一级评论，不拉楼中楼回复。** 匿名状态下回复接口会被页面 SDK 扣住，必须登录才能拿；首版只保留每条评论的回复数。
- **只走匿名，不使用 Cookie。** vault 里的 Cookie 不注入评论浏览器，避免账号承担风控。
- **评论任务与视频任务分离。** 不新增 `TaskStatus`，评论失败不影响视频任务的 `completed` / `partial`。
- 单次快照默认上限 200 条，可调到最多 1000 条；拿满或 `has_more=0` 即停。
- 不存用户 uid、sec_uid、头像等身份字段，只存昵称和 IP 属地。
- 首版不做定时自动刷新，由用户或 Agent 主动触发。

## 3. 拉取流程

新模块 `comments.py`，在 Docker 控制层内运行，复用 `browser_parser.py` 的匿名浏览器配置（`/usr/bin/chromium`、zh-CN、Asia/Shanghai、Linux UA）。spike 已确认容器内 Chromium 与宿主机 Chrome 行为一致。

1. 把浏览器启动与上下文创建从 `load_detail_with_anonymous_browser` 抽成共享的上下文管理器，视频解析和评论拉取共用，避免两套指纹配置漂移。
2. 打开 `https://www.douyin.com/video/<aweme_id>`，屏蔽 `media` / `font`（与 spike 验证过的配置一致；是否还能屏蔽 image 未验证，首版不屏蔽）。
3. 等待页面自己发出第一个 `/aweme/v1/web/comment/list/` 请求（最多 15s），记下它的 query 作为基准，去掉 `a_bogus`、`msToken`、`X-Bogus`。
4. 由 Python 控制翻页循环，每页调用一次 `page.evaluate`，在页面上下文里 `fetch(comment/list?cursor=N&count=20)`；页面 SDK 会自动补签。
5. **每次 evaluate 都要在 JS 侧用 `Promise.race` 加 20s 超时**。`AbortController` 中断不了被 SDK 扣住的请求，spike 中两次卡死都是这个原因。
6. 每页之间随机等待 0.8–1.6s。
7. 停止条件（满足任一）：
   - `has_more=0` → `completed`；
   - 达到上限 → `completed`（记录 `has_more_remaining=true`）；
   - 单任务总时长超过 180s → 有数据时为 `partial`，否则 `failed`；
   - 出现异常页（见 §8）→ 有数据时为 `partial`，否则 `failed`。
8. 按 `cid` 去重（深翻时约 20% 重复），保留第一次出现的位置作为接口原始顺序。

`total` 字段包含楼中楼，跟一级评论数对不上，只记录不做判断依据。

为了可测，`CommentFetcher` 定义为 Protocol：生产实现跑浏览器，测试注入返回固定页序列的假实现。

## 4. 数据模型

新增一张表，沿用现有 `CREATE TABLE IF NOT EXISTS` 写法，不需要迁移：

```sql
CREATE TABLE IF NOT EXISTS comment_jobs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status TEXT NOT NULL,              -- queued / running / completed / partial / failed
    max_comments INTEGER NOT NULL CHECK(max_comments BETWEEN 1 AND 1000),
    fetched_count INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    has_more_remaining INTEGER NOT NULL DEFAULT 0,
    total_reported INTEGER,
    snapshot_name TEXT,                -- 例如 20261018T031500Z-1a2b3c4d
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS comment_jobs_one_active
    ON comment_jobs(task_id) WHERE status IN ('queued', 'running');
```

- 每个视频同一时间最多一个排队或运行中的评论任务；重复提交时返回已有任务（与 `create_or_get_task` 的语义一致）。
- `aweme_id` 直接由 `canonical_url` 经 `normalize_douyin_url` 取得，不要求视频已解析或下载完成；`cancelled` 的任务拒绝拉取。
- 评论明细不入库。几百到一千条直接读快照文件排序即可；以后需要跨视频查询时再加明细表。

## 5. 持久化布局

```text
downloads/<aweme_id>/comments/
├── 20261018T031500Z-1a2b3c4d.json   # 每次快照一份，不覆盖
├── 20261020T090000Z-5e6f7a8b.json
└── latest.md               # 最近一次成功快照的人读版本
```

快照 JSON：

```json
{
  "aweme_id": "7692767036498596273",
  "fetched_at": "2026-10-18T03:15:00Z",
  "status": "completed",
  "pages": 11,
  "has_more_remaining": true,
  "total_reported": 1131,
  "comments": [
    {
      "cid": "7694475988264682277",
      "text": "夹子哥 这个是真的想学，求出教程[色]",
      "digg_count": 617,
      "reply_count": 136,
      "created_at": "2026-10-03T12:20:40Z",
      "ip_label": "广东",
      "nickname": "某某"
    }
  ]
}
```

- `text` 原样保存（含 `[捂脸]` 这类表情占位符），不清洗、不改写。
- 文件名由 UTC 时间戳加评论任务 ID 前 8 位组成，读取时按正则校验；写入沿用 `_atomic_text`。
- `latest.md`：按点赞降序列出，每条一行 `👍617 · 广东 · 10-03  正文`，开头写快照时间、条数、是否拉全。
- 删除视频任务时，评论目录随 `downloads/<aweme_id>` 一起删除；视频未下载（`output_dir` 为空）时也要删除。有评论任务在 `running` 时拒绝删除。

## 6. 调度

- `JobRunner.run_once` 的优先级：视频流水线 → 评论任务 → CPU 转写回退。评论任务一次要跑 30s 到 3 分钟，不能挡住下载。
- 并发为 1；浏览器用完即关，不常驻。
- 启动恢复：`running` 的评论任务改回 `queued`，与 `recover_interrupted_pipeline` 一起执行。
- 新配置项（`VIDEO_RECOVER_` 前缀）：
  - `comments_default_max: int = 200`
  - `comments_job_timeout_seconds: int = 180`
  - `comments_page_timeout_seconds: int = 20`

## 7. 对外接口

### 7.1 REST

- `POST /api/tasks/{task_id}/comments`，body `{"max_comments": 200}` → `202`，返回评论任务（已有活跃任务时返回该任务）。
- `GET /api/tasks/{task_id}/comments` → 该视频所有评论任务，按时间倒序。
- `GET /api/tasks/{task_id}/comments/{job_id}?sort=likes|time&limit=100` → 某次快照的评论。
- `GET /api/tasks/{task_id}/comments/latest.md` → 下载最近一次快照的 Markdown。不放进 `ARTIFACTS`，因为那里依赖 `output_dir`，而评论可以早于视频下载。

### 7.2 MCP

- `fetch_comments(task_id, max_comments=200)`：创建异步评论任务，返回任务状态；instructions 说明它是异步的，要先提交再轮询。
- `get_comments(task_id, job_id=None, sort="likes", limit=50)`：默认取最近一次成功快照，返回压缩文本（每条一行：点赞、日期、属地、正文），外加快照元信息；`limit` 上限 200，控制 token。
- `submit_video` 新增 `fetch_comments: bool = False`：视频解析成功后自动排一个评论任务。
- 两个新工具设置正确的 readOnly / 写入 annotations。

### 7.3 Web

视频详情在「语音文案」之后新增「评论」区块（现有详情区没有页签结构，沿用区块写法）：

- 「拉取评论」/「刷新快照」按钮，显示进行中的状态；
- 快照下拉框（时间 + 条数 + 是否拉全）；
- 列表按点赞或时间排序，带「下载 Markdown」。

## 8. 错误处理

在 `errors.py` 新增：

| 错误 | code | 触发 | retryable |
|---|---|---|---|
| `CommentsUnavailable` | `comments_unavailable` | 页面 15s 内没有发出首个评论请求（评论关闭、视频不可见） | 否 |
| `CommentSignMissing` | `comment_sign_missing` | 页内请求未带 `a_bogus`，或返回非 JSON | 是 |
| `CommentFetchStalled` | `comment_fetch_stalled` | 单页 evaluate 超时 | 是 |
| `CommentRateLimited` | `comment_rate_limited` | HTTP 403/429、`status_code≠0`，或出现验证码文档（verifycenter captcha） | 是 |

- `comment_sign_missing` 意味着抖音改了 SDK，是这条路径最主要的风险，日志里要标出来，方便第一时间发现。
- 视频本身没有评论（首页就是 `has_more=0` 且 0 条）算 `completed`，不算错误。
- 错误信息不带签名参数、完整 URL 或内部路径。

## 9. 测试与验收

自动测试：

- 单元测试：页数据映射与身份字段裁剪、`cid` 去重、四种停止条件、`latest.md` 渲染。fixture 从 spike 响应里截取并替换昵称。
- Repository：评论任务生命周期、同一视频只能有一个活跃任务、启动恢复、级联删除。
- Runner：视频任务优先于评论任务。
- API / MCP：契约测试，覆盖 `limit` 上限、未解析任务被拒、`cancelled` 被拒。

真实链接验收（e2e，默认跳过，沿用 `tests/e2e/test_live_douyin.py` 的开关）：

- 一条热门视频拉 3 页：全部带签名、`status_code=0`、去重后 ≥40 条；
- 在 Docker 容器内跑同一用例。

## 10. 非目标

- 楼中楼回复、Cookie 登录模式；
- 定时自动刷新快照；
- 评论明细入库与跨视频搜索；
- 情感分析、关键词聚类（交给下游 Agent）；
- 直播、图文作品的评论。

## 11. 风险与待定

- **SDK 变更**：抖音不再自动补签时整条路径失效。降级方案是接入 Cookie 模式或逆向签名，均不在首版范围内。
- **高频风控**：未测同一 IP 一天拉几十条视频的表现。首版靠并发 1 + 翻页间隔控制；如出现验证码，再加按小时的任务配额。
- 已定：默认上限 200；`submit_video` 默认 `fetch_comments=false`。
