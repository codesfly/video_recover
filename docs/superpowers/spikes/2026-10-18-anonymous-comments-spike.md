# Spike：匿名拉抖音评论（2026-10-17 ~ 10-18）

目的：在定评论功能方案前，验证不登录能拿多少评论、会不会触发验证码、回复能不能拿。

环境：本机 Mac，Playwright + 系统 Google Chrome（headless，与 `browser_parser.py` 同款上下文：zh-CN / Asia/Shanghai / 去 webdriver 特征），无 cookie。
复现脚本：[2026-10-18-comments-probe.py](2026-10-18-comments-probe.py)（`python probe.py <aweme_id> <max_pages> <top_reply>`）。

## 样本

从匿名打开 `/jingxuan` 的 feed 响应里挑评论数最高的三条：

| aweme_id | 点赞 | 页面 total 字段 | 说明 |
|---|---|---|---|
| 7692009263434986097 | 38 万 | 3136 → 3140 | 测深翻 |
| 7692767036498596273 | 2.6 万 | 1128 | 翻到底 + 回复 |
| 7693908219689571707 | 5.8 万 | 1055 | 翻到底 |

## 结果

### 1. 界面滚动：约 10 条后登录墙

打开视频页后，页面自己发出 `comment/list` cursor=0、5 两页（每页 5 条），再往下滚动，评论区底部出现「登录后可查看更多评论」并弹出登录框，不再发新请求。**纯滚动方案不可行。**

### 2. 页内 fetch：匿名能翻到底，无验证码

在页面上下文里 `fetch('/aweme/v1/web/comment/list/?...cursor=N&count=20')`（去掉原 URL 的 `a_bogus` / `msToken`），页面里的安全 SDK 会**自动给请求补签 `a_bogus`**（102/102 个请求均带签名），服务端正常返回。

| aweme_id | 页数 | 返回条数 | 去重后 | 停止原因 | 耗时 |
|---|---|---|---|---|---|
| 7692767036498596273 | 23 | 441 | 441 | `has_more=0` | 35s |
| 7693908219689571707 | 27 | 522 | 492 | `has_more=0` | 42s |
| 7692009263434986097 | 100 | 1908 | 1530 | 达到上限 100 页（仍 `has_more=1`） | 150s |

- 三条全程 `http 200 / status_code 0`，**没有出现验证码、空页或异常码**；节奏是每页随机间隔 0.8–1.6s。
- 100 页（1500+ 条）内没碰到匿名翻页上限。
- `total` 字段含楼中楼回复，跟一级评论数对不上（1128 vs 441），不能用它判断是否拉全，要以 `has_more` 为准。
- **翻页会出现重复**（深翻时约 20% 重复），必须按 `cid` 去重。
- 每条都带 `ip_label`；`user` 对象有几十个字段（uid、sec_uid、头像等），落盘时应裁剪。
- 页面加载时会拉一个 `rmc-nocaptcha` 的 iframe，这是静默风控探针，每次都有，不是验证码挑战。

### 3. 回复（楼中楼）：匿名拿不到

- 界面点「展开 N 条回复」会直接弹登录框，网络层**一个 `comment/list/reply/` 请求都没发出**。
- 页内 fetch / XHR 调 `comment/list/reply/`（`item_id` + `comment_id`，带或不带 `item_type`）都**永久挂起**（15s 超时仍无响应，网络层也看不到请求），应该是 SDK 在页内就把匿名的 reply 请求扣下了。
- 注意：`AbortController` 也中断不了这类请求，实现时必须在 JS 侧用 `Promise.race` 加超时，否则 worker 会被卡死（spike 中两次卡死都是这个原因）。

## 结论

1. **一级评论：匿名可行**。复用现有匿名 Playwright 浏览器打开视频页，在页内 fetch 翻页即可，不需要逆向 `a_bogus`，也不需要 cookie。默认上限 200–500 条已经覆盖热评；按 `cid` 去重、按 `has_more` 停止。
2. **回复：需要登录**。如果要拉，只能把 vault 里的 cookie 注入浏览器上下文，账号会承担风控风险。建议第一版不做回复，只保留每条评论的 `reply_comment_total` 数字。
3. 这条路径的风险集中在「页面 SDK 自动签名」这一点：抖音改 SDK 后，可能变成不再补签或补签后拒绝。实现时要把「签名缺失 / status_code 非 0 / 挂起超时」都映射成明确的错误码，并保留一份响应 fixture 做解析测试。
4. 未验证：高频使用（同一 IP 一天拉几十条视频）后会不会出现验证码；10 万+ 评论量视频在 100 页之后的表现。（容器内 Chromium 已补测，见下节。）

## 补测：Docker 容器内的 /usr/bin/chromium（2026-10-18）

环境（脚本 [2026-10-18-comments-probe-container.py](2026-10-18-comments-probe-container.py)）：用现有的 `video-recover:local` 镜像起一次性容器（`docker run --rm -i --entrypoint python video-recover:local - <args> < probe.py`），不碰常驻的 app 容器。浏览器启动方式与 `browser_parser.py` 在容器里一致：`executable_path=/usr/bin/chromium`，加 `--no-sandbox` 和 `--disable-dev-shm-usage`，UA 用 Linux。Chromium 版本 151.0.7922.137（aarch64），出口和宿主机是同一个 IP。

| aweme_id | 页数 | 去重后 | 停止原因 | 耗时 |
|---|---|---|---|---|
| 7692767036498596273 | 23 | 417 | `has_more=0` | 38s |
| 7692009263434986097 | 40 | — | 达到上限 40 页（仍 `has_more=1`） | 63s |

- **结论与宿主机 Chrome 一致**：页面 SDK 同样自动补签 `a_bogus`（25/25 个请求带签名），全程 `http 200 / status_code 0`，没有验证码和异常。
- 同一条视频，宿主机去重后 441 条、容器 417 条（相隔约一天）。评论区排序和条数本来就会变，这也说明评论要按快照来存。
- 回复接口在容器内同样挂起：40 页翻完后进入 reply 阶段，约 5 分钟无响应，手动停掉容器。
- 结论：控制面容器可以直接承担拉评论，不需要回到宿主机跑浏览器。

## 对方案的影响

在原设计的基础上：

- 拉取方式从「滚动截获」改为「打开页面 → 页内 fetch 翻页」。
- 一级评论匿名可用；回复延后，等确实需要再接 cookie 开关。
- 所有页内请求加 JS 侧超时；单任务总时长设上限（例如 3 分钟）。
- ~~实现前先补一次容器内 Chromium 的验证。~~ 已验证可行（见上节）。
