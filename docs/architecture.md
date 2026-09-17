# V2 模块与控制边界

## 单向依赖

```text
CLI / HTTP
    → Service：外层自动化、执行控制、状态记录
        → CatalogSync / AssignmentSync / Replay / LiveMonitor：具体业务流程
            → Auth / Canvas / Video / Media / Recognizer / Slides：独立能力
```

`bootstrap` 仅组装具体实现和配置。不将它或 Service 传给能力模块。数据库 Store 只被应用层调用；`types` 只有数据类型及异常。测试通过 AST 检查能力模块不能反向导入流程、数据库或服务。

## 独立调用约定

- `Auth.session(interactive=False)` 返回独立 Canvas Session；`Auth.video(course_id)` 返回带独立 Session 的课程域令牌。调用方负责关闭。鉴权模块不查询课程或视频。
- `Canvas(session_factory)` 提供课程与作业分页查询。`Video(credential)` 提供教学班映射、课次查询和媒体来源解析，不自动登录或保存结果。
- `MediaSource(location, headers, view)` 表示地址或本地文件。地址与请求头不参与 repr；业务只持久化课次 ID，使用前重新解析地址。
- `media.audio(source)` 产生 PCM `AudioChunk`；`media.sample_frames(source, folder)` 产生等间隔采样画面。二者对 Canvas 无任何依赖。
- `Recognizer.transcribe(AudioChunk)` 返回 `TranscriptSegment`。模型实例持有自己的锁，延迟加载 Qwen3-ASR；推理配置是构造参数。
- `slides.extract(frames_dir, output_dir, sample_every, thresholds)` 处理采样画面并返回页面元数据。V1 的感知哈希、灰度、边缘、墨迹和网格差异算法保留，阈值通过参数传入，没有会话、视频客户端、队列或课程状态。
- `assignments.export(session, assignment, folder)` 提取正文并下载附件；每个附件独立保存状态和 SHA-256，失败可重试，成功附件不重复下载。

## 三种容易混淆的执行概念

1. Python `asyncio.Task`：让一个协程持续运行，是运行语言提供的工具。
2. 数据库 execution：记录某个具体处理动作的进度、错误、重试时间及产物，用于重启恢复和防重。
3. V1 的 JobRegistry/Plugin：动态注册、继承任务类型、传递整个 Gateway 的扩展框架。V2 没有这一层。

V2 只有固定的同步、作业、回放 ASR、回放 Slides、直播流程。Service 显式调用这些流程，没有动态函数导入、注册回调表、任务类继承或插件生命周期。

## 直播控制

每个正在监听的课次有一个父协程、一个音频生产协程和一个识别消费协程。父协程负责清理与停止，生产协程负责连接与重连，消费协程调用独立 ASR 并写入转写。队列上限默认 100 个音频块，超载丢最老块并写入缺口事件。

推理仲裁位于应用层，直播优先，回放在块之间让出。模型不认识优先级或课次。直播控制也不直接登录：它调用注入的 `resolve_sources(lecture)`，由组合层连接鉴权与视频客户端。

处理错误不打印可能含签名地址的异常字符串。HTTP/执行记录记录异常类型；会话失效进入 needs_login，无映射的课程标为 video_unavailable。网络/媒体暂时失败最多额外重试三次，间隔 5、15、60 秒。

## 持久化与恢复

SQLite WAL 和事务领取保证同一动作只有一个执行者。唯一键为动作类型、Canvas 课程 ID、课次 ID；三个处理动作互不占用同一状态。服务 runtime 文件锁避免多进程同时恢复同一数据库。

回放音频逐块保存实际起止时间，不将不足一块的结尾补成固定长度。重试从最后完整 JSONL 记录继续，丢弃损坏尾行。Slides 保留旧成功结果，生成完新的图片与清单后再切换结果目录。手动取消保持终态，服务退出恢复待处理；CLI 的短片段测试使用 samples 目录，不污染完整处理记录。

可继续改进的点：直播实时环境验证、未来课表覆盖范围、长时运行性能数据。它们不要求 ASR 或 Slides 依赖调度器。
