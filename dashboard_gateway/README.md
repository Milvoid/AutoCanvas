# Dashboard 访问密码

整个站点的入口网关：未登录只提供独立密码页，Vue 页面、JS/CSS、课程与作业 API、文件及 SSE 均要求有效访问会话。学校 jAccount 登录与此密码互相独立。

配置在项目根目录 `dashboard.yml`（已忽略，不要提交到仓库）。`password` 为访问密码，至少 12 字符；请从 `dashboard.example.yml` 复制配置并设置自己的随机密码。编辑 YAML 后重启网关即可更改密码，同时使旧会话失效。会话保存在内存，默认 12 小时，退出会撤销会话并断开该会话的活动流。Cookie 为 HttpOnly、SameSite=Strict；HTTPS 代理请求设置 Secure。错误密码限速不信任访客自行提供的 IP 头。

```sh
cd webui
npm run build
cd ..
python -m dashboard_gateway --config dashboard.yml
```

本机 4173 端口现在由网关提供，取代 Vite preview。课程后端运行在 `127.0.0.1:8080`。作业列表由课程后端提供；重启入口不影响课程后端。Vite preview 改为仅本机 4174，用于开发；它不提供访问鉴权，不应映射到外网。

cloudflared 的服务目标设为 `http://127.0.0.1:4173`。例如临时 tunnel：

```sh
cloudflared tunnel --url http://127.0.0.1:4173
```

正式 tunnel 的 ingress service 同样指向 4173。不能指向 8080、4174 或开发端口 5173，这些是受信任的本机内部接口。无需在网关列举 tunnel 的域名，保留原始 Host / Origin；HTTPS 请求需保留 cloudflared 的 `X-Forwarded-Proto: https`。不要为该站点的 HTML、API 或静态文件启用 Cloudflare 强制缓存（网关均返回 no-store）。

会话过期后前端转回密码页，所有后续请求被拒绝。页面已经显示过的数据无法从用户截图、下载文件或浏览器内存中追回。
