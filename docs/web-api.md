# WebUI 管理 API

所有 JSON 操作与 CLI 共用同一服务和运行目录。处理请求返回 `202 {"execution_id":"..."}`，实际工作由现有显式流程执行。ASR 与 Slides 分别有自己的串行执行通道；直播仍独立并发，共享模型推理仲裁。

## 接口

| 方法 | 路径 | 参数 / 内容 |
| --- | --- | --- |
| GET | `/health` | 健康、运行数量、调度是否启动、自动化暂停状态，以及 ASR 队列、工作进程和后端内存指标 |
| GET | `/api/courses`, `/api/lectures`, `/api/assignments`, `/api/sync` | 可选 `course_id` 查询参数 |
| POST | `/api/sync` | 可选 `course_id`，手动同步会刷新课表 |
| POST | `/api/assignments/sync` | 立即同步作业；空请求或 `{}` 同步全部启用课程，可选 `course_id` 指定单门课程。返回 202 和 `execution_ids`，单门同时保留 `execution_id`；通过 `/api/executions/{id}` 查询结果 |
| GET/PATCH | `/api/courses/{id}/rules` | `asr/slides/live` 布尔值；`null` 恢复继承全局设置 |
| GET/POST | `/api/automation` | `paused` 布尔值；恢复时可启动原先未启动的调度器 |
| GET | `/api/sources` | `course_id, lecture_id, kind=vod/live`；`show_urls=1` 显示临时私有地址 |
| POST | `/api/process/{vod_asr,vod_slides,live}` | `course_id, lecture_id, view?, retry?` |
| POST | `/api/sample/{asr,slides}` | `course_id, lecture_id, duration, view?`；时长 0–86400 秒（不含 0） |
| GET | `/api/executions`、`/api/executions/{id}` | 执行记录 |
| GET | `/api/executions/{id}/progress` | 已写入的转写时长、Slides 数量、最近直播事件 |
| POST | `/api/executions/{id}/{cancel,retry}` | 取消或重试；返回 `changed` |
| GET | `/api/events` | SSE `state` 事件，每 2 秒发送执行和自动化状态 |
| GET | `/api/settings` | `current, desired, schema, revision, restart_required, waiting_for_idle` |
| PATCH | `/api/settings` | `{"values":{...},"revision":0}`；校验后原子保存，拒绝旧版本写入 |
| GET | `/api/auth/status` | 校验当前 Canvas 会话，不返回 Cookie |
| POST | `/api/auth/challenge` | 返回 `challenge_id, captcha, expires_in`，5 分钟内单次使用 |
| POST | `/api/auth/login` | `challenge_id, username, password, captcha`；密码不持久化 |
| POST | `/api/auth/import` | `cookies` 数组，验证学校域及会话有效性后保存 |
| GET | `/api/auth/automatic` | 自动登录状态、是否已填写、阻塞原因、下次重试和最近成功时间；不返回账号密码 |
| POST | `/api/auth/automatic` | `username, password`；完整登录验证成功后写入本机 `auth/credentials.yml` 并启用 |
| POST | `/api/auth/automatic-test` | 使用 YAML 中的凭据进行全新登录，成功才替换 Cookie；尊重退避和密码错误停试 |
| POST | `/api/auth/automatic-disable` | 停用自动登录并保留凭据 |
| DELETE | `/api/auth/automatic` | 删除保存的凭据；不清除已有 Canvas 会话 |
| POST | `/api/auth/logout` | 清除本地会话与未完成的登录请求，同时停用自动登录；不注销学校端会话 |
| GET/POST | `/api/uploads` | 列表 / multipart `file`，最大 2 GB，允许音视频扩展名 |
| POST | `/api/local/{asr,slides}` | `upload_id, duration?`，每次请求独立执行 |
| GET | `/api/files` | `outputs/`、`samples/`、`assignments/` 文件列表 |
| GET | `/api/file` | `path` 为运行目录相对路径；`download=1` 下载 |
| GET | `/api/logs` | 最近 64 KB 服务日志 |

文件访问检查解析后的路径，拒绝目录穿越和越界符号链接；会话与配置目录不通过文件接口暴露。HTML / SVG 等非预览类型以附件下载。源地址仅在明确请求时返回，响应不包含请求头凭据。

浏览器请求必须同源，Vite / 生产反向代理保留 Host。JSON 请求上限 64 KB，媒体按块写入并独立执行 2 GB 上限。管理 API 默认供本机单用户使用，远程部署访问控制应由外部代理承担。

`configuration.py` 负责设置校验、持久化与生效时机；`browser_auth.py` 负责登录挑战，不依赖 aiohttp；`web_api.py` 只提供传输适配；`progress.py` 从输出文件只读生成进度。ASR、Slides、视频和媒体模块不引用上述 Web 层。

## 课堂事件

`GET /api/classroom-events` 返回 `{events, scans}`：

- `events` 统一包含 `id`, `type` (`qr` / `keyword`), `course_id`, `lecture_id`, `source` (`vod` / `live`), `start`（课堂内秒数）。二维码附带 `decoded`, `content`, `image`（用于现有 `/api/file` 的相对路径）；关键词附带 `keyword`, `text`。
- `scans` 包含各课次二维码扫描状态（`complete`, `partial`, `failed`, `not_scanned`）、扫描图片数与检测数。未扫描 / 失败不等同于未发现二维码。
- 二维码事件只返回具有非空解码内容的确认记录，兼容过滤旧版报告中的误检候选。
- 只投影正式 `outputs` 下的已发布内容，不混入 samples、pending 或 previous。返回的二维码内容按普通文本处理，不自动请求 URL。

作业自动同步使用独立 `assignment_interval`，默认 1800 秒（30 分钟），按课程保存派发时间；不受视频同步间隔影响。自动化暂停时暂停自动同步，手动请求仍可执行。`GET /api/assignments` 只读取已同步列表。

ASR 设置包含 `asr_backend`（torch/mlx）、`asr_dtype`（auto/float32/float16/bfloat16）和 `asr_idle_seconds`（默认 120 秒）。后端、模型和精度在空闲时切换。模型从本地目录或预先准备的缓存加载；MLX 的量化位宽由模型文件决定。模型进程退出后，健康接口清除对应的进程与内存统计。
