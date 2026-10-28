# AutoCanvas V2

新版 Canvas 视频接入、独立本地转写与 Slides 抽取，以及外层自动化控制。

登录鉴权、Canvas 数据查询、视频地址解析、媒体读取、ASR、Slides 各自独立。没有插件加载、Gateway 或 Job 注册机制。ASR 不知道视频 URL，Slides 不知道课程 ID，功能模块不访问执行数据库。

## 环境与启动

Python 3.11+，系统需要 `ffmpeg` 和 `ffprobe`。可使用虚拟环境或 conda；模型从本地 Hugging Face 缓存读取，不自动下载。

```sh
cd /path/to/AutoCanvas
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m autocanvas --help
```

安装到其他环境：`python -m pip install -e '.[asr,slides]'`。仅登录、查询与 HTTP 功能可以只安装基础依赖；ASR 和 Slides 的重依赖延迟加载。

```sh
# 首次创建私有 runtime；已有 runtime 不需要重新初始化。
python -m autocanvas init
# 可在首次 init 时附加 --import-session /path/to/canvas_session.json
python -m autocanvas login
python -m autocanvas sync --assignments
python -m autocanvas serve
```

已有登录会话时无需重复登录。服务默认绑定 `127.0.0.1:8080`。前台运行时 Ctrl+C 清理媒体子进程、保存进度；需要后台驻留可用 `tmux new -s auto-canvas-v2 'conda run --no-capture-output -n auto-canvas python -m autocanvas serve'`。

### 自动登录

将 `credentials.example.yml` 复制到 `runtime/auth/credentials.yml`，直接填写：

```yaml
enabled: true
username: '你的 jAccount 用户名'
password: '你的密码'
```

这是本机明文配置，支持注释；密码中的单引号写成两个单引号。使用 `--root` 时文件位于该运行目录的 `auth/credentials.yml`。文件不进入版本管理，也不会通过 WebUI 文件接口公开；程序写入时设置 POSIX 权限 0600，Windows 使用所在目录的访问权限。配置修改会自动重新读取，无需重启。

认证依次尝试现有 Canvas Cookie、JAAuthCookie 静默刷新、保存的账号密码完整登录。完整登录沿 Canvas OIDC 跳转建立会话 Cookie，不依赖独立 OAuth refresh token。验证码图片发送到用户指定脚本使用的 `https://geek.sjtu.edu.cn/captcha-solver/`，独立请求不携带学校 Cookie 或账号密码。

验证码错误每轮最多重试 3 次；网络或识别服务故障按 60/300/900/3600 秒退避，重启保留退避状态。密码被拒绝或出现额外身份验证时停止自动提交，修改 YAML 后恢复尝试。服务每分钟检查认证并恢复符合条件的 `needs_login` 执行；暂停和取消语义保持不变，视频任务需额外验证课程视频访问。退出登录会同时停用自动登录，避免立即重新登录。

WebUI 登录页提供状态和“验证全新登录”按钮；它不复用现有 Cookie，成功后才替换会话文件，失败保留旧会话。也可在服务运行时调用 `POST /api/auth/automatic-test`，状态通过 `GET /api/auth/automatic` 查看，均不返回密码。

通常无需手动更新会话，但学校登录流程变化、账号锁定或新增短信验证仍可能需要人工处理。认证模块不依赖 macOS 专属能力；Windows/Linux 的 ASR 设备需按实际环境配置（例如 `cpu` 或 `cuda`）。

配置：复制 `config.example.toml` 为本地 `config.toml`，运行 `python -m autocanvas --config config.toml serve`。也可用全局 `--root /private/path` 指定独立运行数据。`config.toml` 与 `runtime/` 都不进入版本管理。

## 独立命令

```sh
# 同步当前课程和视频；没有新版教学班映射的课程会明确显示 video_unavailable。
python -m autocanvas sync
python -m autocanvas sync --course 10001
python -m autocanvas assignments 10001
python -m autocanvas list courses
python -m autocanvas list lectures
python -m autocanvas list executions

# 查询来源编号，默认不输出私人播放地址。
python -m autocanvas sources 10001 20001
# 确实需要地址时显式附加 --show-urls。

# 不依赖 Canvas 登录、数据库或服务的本地处理。
python -m autocanvas transcribe /path/to/audio.wav --output /path/to/transcript
python -m autocanvas slides /path/to/video.mp4 --output /path/to/slides

# 同一课次的 ASR 和 Slides 独立记录结果。
python -m autocanvas process 10001 20001 --kind both
python -m autocanvas process 10001 20001 --kind vod_asr
python -m autocanvas process 10001 20001 --kind vod_slides --view 5
python -m autocanvas process 10001 20001 --kind vod_asr --retry

# 短片段验证写入 samples，不会把完整课次标为已完成。
python -m autocanvas process 10001 20001 --duration 30
```

`--view` 显式覆盖选流。默认 ASR 选择有声来源；Slides 优先分辨率，再使用较低码率作为静态屏幕的启发式。不会硬编码 view 1/5 的含义。

操作同一 runtime 的服务和处理 CLI 使用进程锁，防止双重模型加载与执行。服务运行时使用 HTTP 提交请求。纯本地处理无需常驻服务。

## 自动化与直播

服务启动后同步课程、视频、作业；课表默认每周更新，视频与作业每小时更新，调度每分钟检查。新回放默认自动转写、抽取 Slides，二者独立排队、独立重试。单个课程没有录播映射不影响其作业同步或其他课程。

直播课次到开始前十分钟时创建一个独立监听协程。协程获取最新地址、启动 ffmpeg、持续接收音频，将音频放入有界队列交给 ASR，保存转写并检测关键词。断流后重新获取地址并连接；未开播持续重试至课程结束。队列满时丢弃最老待识别块并记录音频缺口。ASR 在独立子进程内单实例串行执行，直播块优先于待处理回放块，正在推理的块不会被强行中断。PyTorch 后端默认 `asr_dtype = "auto"` 保留模型文件精度（原始 Qwen3-ASR 为 BF16），避免隐式扩展到 FP32。连续 `asr_idle_seconds = 120` 秒没有音频请求后退出模型进程，回收模型、Metal 缓存和原生堆；下一段非静音音频会重新加载模型，因此下一次首块有冷启动延迟（耗时取决于设备和模型）。可在设置页调整精度与空闲时长。`/health` 的 `asr` 字段提供工作进程 PID、实际精度、最近一块完成时的 MPS 张量和驱动分配字节数，进程退出后清除这些指标。

到结束时间停止拉流并消化已收音频。手动停止或服务退出时最多额外等待 30 秒消化队列，未处理部分记录为缺口。ffmpeg 始终回收。直播结束安排回放检查，回放生成延迟由后续小时同步补齐。

重启后，中断执行恢复为待处理；回放从已保存音频时间位置继续，Slides 重新生成后替换结果。仍在上课的直播重新连接，已结束的监听记录标为 expired 并触发回放同步。显式取消不会被周期同步重新启动，需显式重试。

自动化暂停只停止新的自动派发，正在执行的任务继续；取消正在执行的任务需使用取消接口。人工请求可在暂停期间执行。

直播目前按官方前端 `liveDay=0` 查询可见课次；真实直播流仍需在有正在直播的课程时验收。不能据此保证尚未被平台返回的未来课次会提前十分钟发现。接口与限制详见 [验证记录](docs/validation.md)。

## HTTP

所有 `/api` 端点都是明确功能入口；耗时请求返回 `202` 和 `execution_id`。

| 方法与路径 | 功能 |
| --- | --- |
| `GET /health` | 服务、鉴权阻塞、后台循环健康状态 |
| `GET /api/courses`、`/api/lectures`、`/api/assignments`、`/api/sync` | 已同步的数据与同步状态，可用 `?course_id=` 过滤 |
| `POST /api/sync` | 同步全部或 `{"course_id":"10001"}` 指定课程，并安排作业同步 |
| `POST /api/process/vod_asr`、`vod_slides`、`live` | 输入 course_id、lecture_id，可选 view、retry |
| `GET /api/executions`、`/api/executions/{id}` | 查看结果、错误类型、产物位置 |
| `POST /api/executions/{id}/cancel`、`retry` | 取消、重试 |
| `GET/POST /api/automation` | 查看或设置 `{"paused":true}` |
| `GET/PATCH /api/courses/{id}/rules` | 按课程覆盖 `asr`、`slides`、`live` 开关 |

```sh
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/api/process/vod_slides \
  -H 'Content-Type: application/json' \
  -d '{"course_id":"10001","lecture_id":"20001"}'
```

不存在上传代码、动态注册任务或操作模型内部状态的接口。重新登录后会自动恢复符合条件的 `needs_login` 执行，也可手动 retry；常驻进程不会等待交互式密码或验证码。

## 数据与验证

`runtime/state.sqlite3` 是唯一执行状态库；`auth/` 保存私有会话，`assignments/` 保存作业，`outputs/<course>/<lecture>/` 保存转写和 Slides，`cache/` 保存中间画面，`logs/` 保存轮转服务日志。完整媒体地址和令牌不写入数据库、转写头或日志。

转写有断点 JSONL、最终 JSON 和 TXT；Slides 有图片、时间清单与联系表；直播有连接、缺口、关键词事件 JSONL。Slides 完成后自动检测二维码并保存独立 qr.json；WebUI「点名与二维码」按课程展示直播关键词与回放二维码记录。日志与产物含个人课程数据，保留在个人目录。向 Public 发布时仅复制源码、测试、文档和配置模板，另行脱敏。

```sh
python -m unittest discover -s tests -v
```

测试覆盖模块依赖边界、SQLite 领取防重、暂停/取消/重启、鉴权故障隔离、直播断流/满载/清理、HTTP 控制和真实 ffmpeg 本地处理。架构说明见 [模块边界](docs/architecture.md)。

## 独立 WebUI

前端在 `webui/`，使用 Vue 3 + TypeScript + Vite + Element Plus。先启动 `python -m autocanvas serve`，再在另一终端执行：

```sh
cd webui
npm ci
npm run dev
```

访问 http://127.0.0.1:5173 。支持全部配置、登录、待执行课程、历史执行记录、按课程组织的图片墙与转写阅读、作业附件及日志。前端仅通过 REST / SSE 调用后端，关闭页面不影响处理流程。

详见 [WebUI 启动与部署](webui/README.md) 和 [API 说明](docs/web-api.md)。

## 作业列表

作业列表默认每 30 分钟从 Canvas 自动同步一次（`assignment_interval = 1800`）。WebUI「立即刷新」或 `POST /api/assignments/sync`（空请求同步全部启用课程，可选 `course_id`）可立即触发同步。自动化暂停时仍可手动刷新。

WebUI「作业」提供 Canvas 作业列表，支持按作业名称或课程搜索、查看截止时间及跳转到 Canvas 作业页面。

「点名与二维码」页面现支持直播自动签到：设置课程范围后独立观察实时画面，使用最新解码结果提交，并显示耗时与学校响应。直播扫码优先 HTTP-FLV、失败回退 HLS；回放二维码不会触发签到。结果不确定或学校要求定位时需在交我办核对 / 完成，不自动重复提交。

## 访问密码与 cloudflared

当前 WebUI 的 4173 端口由独立密码网关提供。访问密码见本地 `dashboard.yml` 的 `password` 字段，修改后重启网关。未登录只加载密码页，所有业务 API、资源与实时流均受保护。cloudflared 指向 `http://127.0.0.1:4173`；不要将内部 8080 或开发端口映射到外网。详见 [部署与会话管理](dashboard_gateway/README.md)。


## Apple Silicon 的 MLX 量化转写

可选安装 `python -m pip install -e '.[asr-mlx]'`，并预先下载模型：

```sh
hf download moona3k/mlx-qwen3-asr-0.6b-8bit
```

设置页同时修改 `asr_backend = "mlx"`、`model = "moona3k/mlx-qwen3-asr-0.6b-8bit"`、`asr_dtype = "auto"`。修改在空闲时生效。MLX 后端只从本地目录或 Hugging Face 缓存加载，不在任务执行时下载权重。`auto` 使用 FP16 激活，权重量化位宽由模型文件决定；把 dtype 改为 float32 不会将 8-bit 权重恢复成原始高精度权重。

4-bit 可用 `moona3k/mlx-qwen3-asr-0.6b-4bit`（解码器 4-bit，音频编码器 8-bit），同样需提前下载。示例采用 8-bit；4-bit 更节省权重内存，但可能影响识别结果，需用自己的音频评估。后端验证范围见 [验证指南](docs/validation.md)。两种后端都保留单实例串行推理、直播优先和空闲 120 秒后回收进程。

回退 PyTorch：同时设置 `asr_backend = "torch"`、`model = "Qwen/Qwen3-ASR-0.6B"`、`device = "mps"`、`asr_dtype = "auto"`。`/health` 的 `asr` 包含后端、工作进程、浮点精度与量化位宽；MLX 的 active/cache/peak 是分配器统计，不等同于 macOS 的完整进程 footprint。

## 公开源码与本地配置

仓库仅包含源码、测试和示例配置。将 `dashboard.example.yml` 复制为 `dashboard.yml` 并设置自己的访问密码。凭据、Cookie、课程数据、媒体、转写和测量产物保存在本地，均不应提交。文档中的课程和课次编号均为示例。
