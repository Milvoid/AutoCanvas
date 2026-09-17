# AutoCanvas V2

新版 Canvas 视频接入、独立本地转写与 Slides 抽取，以及外层自动化控制。

登录鉴权、Canvas 数据查询、视频地址解析、媒体读取、ASR、Slides 各自独立。没有插件加载、Gateway 或 Job 注册机制。ASR 不知道视频 URL，Slides 不知道课程 ID，功能模块不访问执行数据库。

## 环境与启动

Python 3.11+，系统需要 `ffmpeg` 和 `ffprobe`。本机沿用 `auto-canvas` conda 环境；模型从本地 Hugging Face 缓存读取，不自动下载。

```sh
conda activate auto-canvas
cd /path/to/AutoCanvas
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

配置：复制 `config.example.toml` 为本地 `config.toml`，运行 `python -m autocanvas --config config.toml serve`。也可用全局 `--root /private/path` 指定独立运行数据。`config.toml` 与 `runtime/` 都不进入版本管理。

## 独立命令

以下课程 ID `12345` 和课次 ID `67890` 均为示例，请替换为自己账号查询到的 ID。

```sh
# 同步当前课程和视频；没有新版教学班映射的课程会明确显示 video_unavailable。
python -m autocanvas sync
python -m autocanvas sync --course 12345
python -m autocanvas assignments 12345
python -m autocanvas list courses
python -m autocanvas list lectures
python -m autocanvas list executions

# 查询来源编号，默认不输出私人播放地址。
python -m autocanvas sources 12345 67890
# 确实需要地址时显式附加 --show-urls。

# 不依赖 Canvas 登录、数据库或服务的本地处理。
python -m autocanvas transcribe /path/to/audio.wav --output /path/to/transcript
python -m autocanvas slides /path/to/video.mp4 --output /path/to/slides

# 同一课次的 ASR 和 Slides 独立记录结果。
python -m autocanvas process 12345 67890 --kind both
python -m autocanvas process 12345 67890 --kind vod_asr
python -m autocanvas process 12345 67890 --kind vod_slides --view 5
python -m autocanvas process 12345 67890 --kind vod_asr --retry

# 短片段验证写入 samples，不会把完整课次标为已完成。
python -m autocanvas process 12345 67890 --duration 30
```

`--view` 显式覆盖选流。默认 ASR 选择有声来源；Slides 优先分辨率，再使用较低码率作为静态屏幕的启发式。不会硬编码 view 1/5 的含义。

操作同一 runtime 的服务和处理 CLI 使用进程锁，防止双重模型加载与执行。服务运行时使用 HTTP 提交请求。纯本地处理无需常驻服务。

## 自动化与直播

服务启动后同步课程、视频、作业；课表默认每周更新，视频与作业每小时更新，调度每分钟检查。新回放默认自动转写、抽取 Slides，二者独立排队、独立重试。单个课程没有录播映射不影响其作业同步或其他课程。

直播课次到开始前十分钟时创建一个独立监听协程。协程获取最新地址、启动 ffmpeg、持续接收音频，将音频放入有界队列交给 ASR，保存转写并检测关键词。断流后重新获取地址并连接；未开播持续重试至课程结束。队列满时丢弃最老待识别块并记录音频缺口。ASR 单实例串行执行，直播块优先于待处理回放块，正在推理的块不会被强行中断。

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
| `POST /api/sync` | 同步全部或 `{"course_id":"12345"}` 指定课程，并安排作业同步 |
| `POST /api/process/vod_asr`、`vod_slides`、`live` | 输入 course_id、lecture_id，可选 view、retry |
| `GET /api/executions`、`/api/executions/{id}` | 查看结果、错误类型、产物位置 |
| `POST /api/executions/{id}/cancel`、`retry` | 取消、重试 |
| `GET/POST /api/automation` | 查看或设置 `{"paused":true}` |
| `GET/PATCH /api/courses/{id}/rules` | 按课程覆盖 `asr`、`slides`、`live` 开关 |

```sh
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/api/process/vod_slides \
  -H 'Content-Type: application/json' \
  -d '{"course_id":"12345","lecture_id":"67890"}'
```

不存在上传代码、动态注册任务或操作模型内部状态的接口。重新登录后可对 `needs_login` 执行调用 retry；常驻进程不会等待交互式密码或验证码。

## 数据与验证

`runtime/state.sqlite3` 是唯一执行状态库；`auth/` 保存私有会话，`assignments/` 保存作业，`outputs/<course>/<lecture>/` 保存转写和 Slides，`cache/` 保存中间画面，`logs/` 保存轮转服务日志。完整媒体地址和令牌不写入数据库、转写头或日志。

转写有断点 JSONL、最终 JSON 和 TXT；Slides 有图片、时间清单与联系表；直播有连接、缺口、关键词事件 JSONL。日志与产物含个人课程数据，保留在个人目录。发布时仅包含源码、测试、文档和配置模板，不包含个人运行数据。

```sh
python -m unittest discover -s tests -v
```

测试覆盖模块依赖边界、SQLite 领取防重、暂停/取消/重启、鉴权故障隔离、直播断流/满载/清理、HTTP 控制和真实 ffmpeg 本地处理。架构说明见 [模块边界](docs/architecture.md)。
