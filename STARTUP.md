# AutoCanvas 标准化启动流程

本文档记录 AutoCanvas 及其 WebUI、密码网关和 Cloudflare Tunnel 的标准启动流程。

## 服务架构

```
外部访问 (autocanvas.example.com)
    ↓
cloudflared (Cloudflare Tunnel)
    ↓
密码网关 (0.0.0.0:4173) ← dashboard_gateway
    ↓
├─→ WebUI 静态文件 (webui/dist/)
└─→ AutoCanvas API (127.0.0.1:8080)
```

## 前置检查

### 1. 检查端口占用

```bash
# 检查 8080 端口（后端 API）
lsof -i :8080 | grep LISTEN

# 检查 4173 端口（密码网关）
lsof -i :4173 | grep LISTEN

# 如果端口被占用，停止旧进程
kill <PID>
```

### 2. 清理锁文件

```bash
rm -f /path/to/AutoCanvas/runtime/service.lock
```

### 3. 确认 conda 环境

```bash
conda env list | grep auto-canvas
# 应该显示: auto-canvas /path/to/conda/envs/auto-canvas
```

## 启动流程

### 方法一：tmux 标准启动（推荐）

在 tmux 中启动所有服务，便于管理和查看日志。

#### 1. 创建 tmux session 并启动后端服务

```bash
cd /path/to/AutoCanvas
tmux new-session -d -s autocanvas -c /path/to/AutoCanvas
tmux send-keys -t autocanvas 'conda activate auto-canvas' C-m
tmux send-keys -t autocanvas 'python -m autocanvas serve' C-m
```

等待 3-5 秒，检查启动状态：

```bash
tmux capture-pane -t autocanvas -p | tail -5
# 应该看到: AutoCanvas V2 listening on http://127.0.0.1:8080
```

#### 2. 启动密码网关

```bash
tmux new-window -t autocanvas -n dashboard -c /path/to/AutoCanvas
tmux send-keys -t autocanvas:dashboard 'conda activate auto-canvas' C-m
tmux send-keys -t autocanvas:dashboard 'python -m dashboard_gateway --config dashboard.yml' C-m
```

等待 2-3 秒，检查启动状态：

```bash
tmux capture-pane -t autocanvas:dashboard -p | tail -5
# 应该看到: ======== Running on http://0.0.0.0:4173 ========
```

#### 3. （可选）启动开发用 WebUI

仅用于前端开发调试，生产环境不需要：

```bash
tmux new-window -t autocanvas -n webui -c /path/to/AutoCanvas/webui
tmux send-keys -t autocanvas:webui 'npm run dev' C-m
```

### 方法二：独立终端启动

如果不使用 tmux，可以在独立终端窗口中启动：

```bash
# 终端 1: 后端服务
cd /path/to/AutoCanvas
conda activate auto-canvas
python -m autocanvas serve

# 终端 2: 密码网关
cd /path/to/AutoCanvas
conda activate auto-canvas
python -m dashboard_gateway --config dashboard.yml
```

## 服务验证

### 1. 检查本地服务状态

```bash
# 后端 API
curl http://127.0.0.1:8080/health

# 密码网关
curl -I http://127.0.0.1:4173
```

### 2. 检查 cloudflared 状态

```bash
# 查看 cloudflared 进程
ps aux | grep cloudflared | grep -v grep

# 应该看到类似:
# <user>  <PID>  ... /opt/homebrew/bin/cloudflared tunnel --protocol http2 run --token-file ...
```

### 3. 测试外部访问

访问 https://autocanvas.example.com

- 应该看到密码登录页面
- 密码见 `dashboard.yml` 中的 `password` 字段

## 重新构建 WebUI（生产环境）

当前端代码有更新时，需要重新构建静态文件：

```bash
cd /path/to/AutoCanvas/webui
npm run build
```

构建完成后，重启密码网关以加载新的构建文件：

```bash
# 找到密码网关的 PID
lsof -i :4173 | grep LISTEN

# 停止旧进程
kill <PID>

# 重启密码网关（在 tmux 中）
tmux send-keys -t autocanvas:dashboard 'python -m dashboard_gateway --config dashboard.yml' C-m
```

## tmux 会话管理

### 查看当前会话

```bash
tmux list-sessions
tmux list-windows -t autocanvas
```

### 进入会话

```bash
# 进入 autocanvas session（默认显示 window 0）
tmux attach -t autocanvas

# 切换 window
# Ctrl+B 然后按数字键: 0=后端, 1=webui, 2=dashboard
# 或者 Ctrl+B 然后按 n (next) / p (previous)
```

### 退出会话但保持运行

按 `Ctrl+B` 然后按 `d` (detach)

### 停止服务

```bash
# 方法 1: 进入 tmux 后在对应 window 按 Ctrl+C

# 方法 2: 直接停止进程
lsof -i :8080 | grep LISTEN  # 找到后端 PID
lsof -i :4173 | grep LISTEN  # 找到密码网关 PID
kill <PID>

# 方法 3: 关闭整个 tmux session
tmux kill-session -t autocanvas
```

## 常见问题排查

### 问题 1: 后端服务启动失败 "OSError: address already in use"

**原因**: 8080 端口已被占用

**解决**:
```bash
lsof -i :8080 | grep LISTEN
kill <PID>
rm -f /path/to/AutoCanvas/runtime/service.lock
```

### 问题 2: cloudflared Error 1033 "Cloudflare Tunnel error"

**原因**: Cloudflare 找不到已连接的 cloudflared 实例。进程存在不代表隧道连接正常；密码网关未运行通常导致 502，而不是 1033。

**解决**:
```bash
# 检查隧道连接日志和就绪状态（示例 metrics 端口为 20241，请按部署调整）
tail -50 ~/Library/Logs/com.cloudflare.cloudflared.err.log
curl --max-time 5 http://127.0.0.1:20241/ready

# 检查当前 DNS 解析
dig +short region1.v2.argotunnel.com A
dig +short region2.v2.argotunnel.com A

# macOS LaunchAgent 部署示例；请按自己的服务管理方式重启
launchctl kickstart -k gui/$(id -u)/com.cloudflare.cloudflared

# 等待日志出现 Registered tunnel connection 后验证公网入口
curl --max-time 15 -I https://autocanvas.example.com/
```

若使用代理的 fake-ip DNS，切换或重启代理后，隧道可能仍尝试连接过期的虚拟地址。对照连接日志与当前 DNS 结果，必要时重启 cloudflared 以重新解析。日志路径、metrics 端口及服务管理方式需按自己的部署调整。

参考：[Cloudflare Tunnel 故障排查](https://developers.cloudflare.com/tunnel/troubleshooting/)。

### 问题 3: 访问网站显示旧版本或 404

**原因**: webui/dist 构建过期

**解决**:
```bash
cd /path/to/AutoCanvas/webui
npm run build
# 然后重启密码网关
```

### 问题 4: 认证失败 "needs_login"

**原因**: Canvas 会话过期

**解决**:
```bash
cd /path/to/AutoCanvas
conda activate auto-canvas
python -m autocanvas login
```

## 配置文件说明

### dashboard.yml

```yaml
password: "访问密码（至少12个字符）"
host: "0.0.0.0"
port: 4173
session_hours: 12
dist: webui/dist                    # WebUI 静态文件目录
autocanvas_api: http://127.0.0.1:8080  # 后端 API 地址
```

### runtime/auth/credentials.yml

自动登录配置（可选）：

```yaml
enabled: true
username: 'jAccount 用户名'
password: 'jAccount 密码'
```

## 日志查看

### 后端服务日志

```bash
tail -f /path/to/AutoCanvas/runtime/logs/service.log
```

### tmux 日志

在 tmux 会话中按 `Ctrl+B` 然后按 `[` 进入滚动模式，按 `q` 退出。

## 完整启动脚本

保存为 `start.sh` 便于快速启动：

```bash
#!/bin/bash
set -e

cd /path/to/AutoCanvas

echo "检查并清理旧进程..."
OLD_8080=$(lsof -ti :8080 || true)
OLD_4173=$(lsof -ti :4173 || true)
[ -n "$OLD_8080" ] && kill $OLD_8080 && echo "已停止旧的后端服务 (PID: $OLD_8080)"
[ -n "$OLD_4173" ] && kill $OLD_4173 && echo "已停止旧的密码网关 (PID: $OLD_4173)"

echo "清理锁文件..."
rm -f runtime/service.lock

echo "启动 tmux session..."
tmux new-session -d -s autocanvas -c /path/to/AutoCanvas

echo "启动后端服务..."
tmux send-keys -t autocanvas 'conda activate auto-canvas' C-m
tmux send-keys -t autocanvas 'python -m autocanvas serve' C-m

sleep 3

echo "启动密码网关..."
tmux new-window -t autocanvas -n dashboard -c /path/to/AutoCanvas
tmux send-keys -t autocanvas:dashboard 'conda activate auto-canvas' C-m
tmux send-keys -t autocanvas:dashboard 'python -m dashboard_gateway --config dashboard.yml' C-m

sleep 2

echo "验证服务状态..."
tmux capture-pane -t autocanvas:0 -p | tail -3
tmux capture-pane -t autocanvas:dashboard -p | tail -3

echo ""
echo "✓ AutoCanvas 启动完成"
echo ""
echo "本地访问:"
echo "  - 后端 API: http://127.0.0.1:8080"
echo "  - 密码网关: http://127.0.0.1:4173"
echo ""
echo "外部访问: https://autocanvas.example.com"
echo ""
echo "tmux 会话管理:"
echo "  - 进入会话: tmux attach -t autocanvas"
echo "  - 退出会话: Ctrl+B 然后按 d"
echo "  - 停止服务: tmux kill-session -t autocanvas"
```

使用方法：

```bash
chmod +x start.sh
./start.sh
```
