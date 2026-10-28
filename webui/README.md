# AutoCanvas WebUI

独立的 Vue 3 / TypeScript 应用，使用 Vite、Element Plus、Vue Router 和 TanStack Vue Query。所有数据经 REST / SSE 访问。前端不读取 Python 包、SQLite 或运行目录；后端不依赖 Node，不负责托管前端。

## 本地启动

在项目根目录启动 API（已安装 Python 依赖）：

```sh
python -m autocanvas serve
```

在另一个终端启动 WebUI（Node 22.12+，推荐当前 LTS）：

```sh
cd webui
npm ci
npm run dev
```

打开 http://127.0.0.1:5173 。仅手动处理时，后端使用 `python -m autocanvas serve --no-automation`；界面仍能手动发起任务，也可以通过自动化开关启动调度。

API 默认地址为 `http://127.0.0.1:8080`。如有更改，在 `webui/.env.local` 中设置：

```dotenv
AUTOCANVAS_API=http://127.0.0.1:8081
```

修改代理地址后重启 Vite。此变量只用于开发 / 预览代理，不会把地址编译进浏览器代码。

## 构建与部署

```sh
npm run build
npm run preview
```

`build` 先执行严格 TypeScript / Vue 模板检查，再输出 `dist/`。`preview` 仅监听本机端口 4174，并代理 API，可通过 `http://127.0.0.1:4174/` 访问。允许的主机名在 `vite.config.ts` 的 `preview.allowedHosts` 中配置。

长期运行时，可用独立的静态服务器托管 `dist/`：

- SPA 路由回退到 `index.html`。
- 同域 `/api/*` 和 `/health` 反向代理到 Python 服务，保留原始 Host。
- SSE 路由 `/api/events` 关闭代理缓冲，并允许长连接。
- `/_dashboard/*` 代理到现有密码入口（默认 `127.0.0.1:4173`，可设置 `DASHBOARD_API`）。作业列表直接读取 `/api/assignments`。
- 上传请求体上限至少 2 GB。

Python API 默认仅监听本机，WebUI 预览服务仅监听本机并转发 API。当前管理接口面向单用户本地服务，没有独立的管理账号；如需远程访问，应由反向代理提供 HTTPS 和访问认证。Canvas 登录负责学校会话，不是 WebUI 访问控制。

## 页面与能力

| 页面 | 能力 |
| --- | --- |
| 概览 | 课程、视频、执行统计，调度状态，自动化开关 |
| 待执行课程 | 按课程组织执行中、排队中、等待认证、即将开始与待安排的课次；同步、课程规则与手动安排 |
| 执行记录 | 已结束的执行，按课程 / 状态 / 类型筛选，查看详情、重试及打开产物 |
| 产物浏览 | 课程资料库、课次目录、Slides 图片墙与键盘翻页、转写时间轴 / 搜索 / 连续阅读、结构化数据面板、作业与附件 |
| 作业 | Canvas 作业列表、按作业或课程搜索、截止时间、立即同步及跳转 Canvas |
| 点名与二维码 | 直播点名关键词和回放二维码记录，课程筛选 / 搜索、时间定位、原图查看与内容复制 |
| 全部设置 | 全部 Settings 字段，当前值 / 保存值及生效时机 |
| 登录与鉴权 | jAccount 验证码登录、Cookie 导入、会话检查、清除本地会话 |
| 服务日志 | 最近 64 KB 日志，自动刷新 |

本地处理已从 WebUI 移除，CLI / API 仍保留。旧 `/local` 地址跳转到产物浏览。历史片段试跑可在课次目录中勾选显示，不混入完整回放产物。

执行详情集中展示状态、进度与操作，内部标识和异常类型收在诊断信息中。文字预览上限为 2 MB，较大文档可下载；长转写按每页 60 段阅读。

UI 保存的设置在运行目录的 `settings.json`，CLI 启动时也会读取。调度字段保存后用于下一次调度判断；处理参数等正在执行的任务和模型推理空闲后应用；数据目录、监听地址、端口需重启。更改数据目录不迁移原有会话、数据库或产物。重启前仍向原运行目录保存配置；从原启动入口重启可加载新的目录设置。

## 前端边界

- `src/pages/`：各页面独立维护交互状态和查询，按路由加载。
- `src/api.ts`：HTTP 请求与文件 URL。
- `src/queries.ts`：查询缓存与操作反馈。
- `src/App.vue`：页面外壳和 SSE 连接，断线重连。
- `src/format.ts`：状态和日期展示。

删除 `webui/` 不影响 CLI、API 或自动化；关闭浏览器不停止后端任务。后端接口说明见 `../docs/web-api.md`。

## 前端验证

```sh
npm test
npm run build
```

测试覆盖待办与历史划分、执行中课次归组、直播时段、同名课次、产物目录隔离和 Slides 排序。测试命令需要支持 `--experimental-strip-types` 的 Node（22.12+）。

## 密码保护入口（当前部署）

对外入口已改为独立 `dashboard_gateway`，使用项目根目录 `dashboard.yml` 的访问密码。先构建 WebUI，再运行 `python -m dashboard_gateway --config dashboard.yml`。4173 由网关直接托管构建产物并转发课程后端，未登录不加载前端 bundle 或 API。Vite preview 仅用于本机调试，改为 `127.0.0.1:4174`。cloudflared 必须指向 `http://127.0.0.1:4173`，具体说明见 [访问鉴权](../dashboard_gateway/README.md)。
