#!/usr/bin/env python3
"""
Video API Server
----------------
HTTP API 接口，支持 Job/Schedule/Queue/Plugin 的完整 CRUD。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from aiohttp import web

from gateway import AutoCanvasGateway
from jobs.job_manager import JobType, JOBS_USER_DIR

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)

def _error(message: str, status: int = 400) -> web.Response:
    return _json_response({"success": False, "error": message}, status=status)

# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------
async def health_handler(request: web.Request) -> web.Response:
    gw: AutoCanvasGateway = request.app["gateway"]
    return _json_response({
        "success": True,
        "started_at": gw.started_at,
        "last_heartbeat": gw.last_heartbeat,
        "jobs_count": len(gw.job_registry._jobs) if gw.job_registry else 0,
    })

# ---------------------------------------------------------------------
# Job Management (Foundation)
# ---------------------------------------------------------------------
async def jobs_list_handler(request: web.Request) -> web.Response:
    """列出所有 Job"""
    gw: AutoCanvasGateway = request.app["gateway"]
    job_type = request.query.get("type")

    if job_type:
        jobs = gw.job_registry.list_by_type(JobType(job_type))
    else:
        jobs = gw.job_registry.list_all()

    return _json_response({"success": True, "jobs": jobs})

async def jobs_detail_handler(request: web.Request) -> web.Response:
    """获取单个 Job 详情"""
    gw: AutoCanvasGateway = request.app["gateway"]
    name = request.match_info["name"]
    instance = gw.job_registry.get(name)
    if not instance:
        return _error(f"Job {name} 不存在", 404)

    return _json_response({
        "success": True,
        "job": gw.job_registry._to_dict(name),
    })

async def jobs_create_handler(request: web.Request) -> web.Response:
    """创建 Job (动态注册)"""
    gw: AutoCanvasGateway = request.app["gateway"]

    try:
        body = await request.json()
    except:
        return _error("请求体必须是合法 JSON")

    name = body.get("name")
    job_type = body.get("type", "basic")
    coro_name = body.get("coro")  # 可以是内置 coro 名称或 plugin 路径

    if not name:
        return _error("缺少 name")

    # 检查是否为插件注册
    if job_type == "plugin":
        plugin_path = body.get("plugin_path")
        if not plugin_path:
            return _error("plugin 类型需要提供 plugin_path")
        plugin_job_type = body.get("plugin_job_type", "basic")
        if plugin_job_type == "scheduled":
            return _error("JobRegistry scheduled job 已移除；请使用业务 Schedule 或 LoopJob", 400)

        config = gw.job_registry.register_plugin(
            name,
            plugin_path,
            job_type=JobType(plugin_job_type),
            interval=body.get("interval", 0),
            priority=body.get("priority", 0),
            retries=body.get("retries", 3),
        )
        if not config:
            return _error("插件注册失败", 500)

        # 如果指定了 auto_start，立即启动
        if body.get("auto_start", False):
            gw.job_registry.start(name)

        return _json_response({
            "success": True,
            "message": f"Plugin Job {name} 已注册",
            "config": config.to_dict(),
        })

    # 内置 Job 类型（需要代码中预定义）
    # 这里简化处理，实际应该通过名称查找对应的 coro
    return _error("内置 Job 请使用特定端点或通过 plugin 方式加载")

async def jobs_update_handler(request: web.Request) -> web.Response:
    """更新 Job 配置"""
    gw: AutoCanvasGateway = request.app["gateway"]
    name = request.match_info["name"]

    try:
        body = await request.json()
    except:
        return _error("请求体必须是合法 JSON")

    ok = gw.job_registry.update(
        name,
        enabled=body.get("enabled"),
        interval=body.get("interval"),
        retries=body.get("retries"),
        retry_delay=body.get("retry_delay"),
        priority=body.get("priority"),
        metadata=body.get("metadata"),
    )

    if not ok:
        return _error(f"Job {name} 不存在", 404)

    return _json_response({"success": True, "message": f"Job {name} 已更新"})

async def jobs_delete_handler(request: web.Request) -> web.Response:
    """删除 Job"""
    gw: AutoCanvasGateway = request.app["gateway"]
    name = request.match_info["name"]

    ok = await gw.job_registry.delete(name)
    if ok:
        return _json_response({"success": True, "message": f"Job {name} 已删除"})
    return _error(f"Job {name} 不存在或删除失败", 404)

async def jobs_pause_handler(request: web.Request) -> web.Response:
    """暂停 Job"""
    gw: AutoCanvasGateway = request.app["gateway"]
    name = request.match_info["name"]

    ok = gw.job_registry.pause(name)
    if ok:
        return _json_response({"success": True, "message": f"Job {name} 已暂停"})
    return _error(f"Job {name} 不存在", 404)

async def jobs_resume_handler(request: web.Request) -> web.Response:
    """恢复 Job"""
    gw: AutoCanvasGateway = request.app["gateway"]
    name = request.match_info["name"]

    ok = gw.job_registry.resume(name)
    if ok:
        return _json_response({"success": True, "message": f"Job {name} 已恢复"})
    return _error(f"Job {name} 不存在", 404)

async def jobs_start_handler(request: web.Request) -> web.Response:
    """启动 Job（手动触发）"""
    gw: AutoCanvasGateway = request.app["gateway"]
    name = request.match_info["name"]

    ok = gw.job_registry.start(name)
    if ok:
        return _json_response({"success": True, "message": f"Job {name} 已启动"})
    return _error(f"Job {name} 不存在、已禁用或已在运行", 400)

# ---------------------------------------------------------------------
# Plugin Management
# ---------------------------------------------------------------------
async def jobs_reload_handler(request: web.Request) -> web.Response:
    """热重载用户 Job（从已保存的文件重新加载代码）"""
    gw: AutoCanvasGateway = request.app["gateway"]
    name = request.match_info["name"]
    instance = gw.job_registry.get(name)
    if not instance or not instance.config.plugin_path:
        return _error("Job 不存在或不是用户 Job", 404)
    config = await gw.job_registry.reload_job(
        name,
        instance.config.plugin_path,
        job_type=instance.config.job_type,
        interval=instance.config.interval,
        priority=instance.config.priority,
        retries=instance.config.retries,
        retry_delay=instance.config.retry_delay,
        metadata=dict(instance.config.metadata),
        auto_start=True,
    )
    if not config:
        return _error("热重载失败", 500)
    return _json_response({"success": True, "config": config.to_dict()})

async def plugins_list_handler(request: web.Request) -> web.Response:
    """列出用户 Job（兼容端点）"""
    gw: AutoCanvasGateway = request.app["gateway"]
    return _json_response({"success": True, "plugins": gw.job_registry.list_by_type(JobType.PLUGIN)})

async def plugins_upload_handler(request: web.Request) -> web.Response:
    """上传并注册用户 Job（兼容端点）"""
    gw: AutoCanvasGateway = request.app["gateway"]

    try:
        body = await request.json()
    except:
        return _error("请求体必须是合法 JSON")

    name = body.get("name")
    code = body.get("code")

    if not name or not code:
        return _error("缺少 name 或 code")

    JOBS_USER_DIR.mkdir(parents=True, exist_ok=True)
    job_path = JOBS_USER_DIR / f"{name}.py"

    try:
        job_path.write_text(code, encoding="utf-8")
    except Exception as e:
        return _error(f"保存 Job 文件失败: {e}", 500)

    job_type_value = body.get("job_type", "basic")
    if job_type_value == "scheduled":
        return _error("JobRegistry scheduled job 已移除；请使用业务 Schedule 或 LoopJob", 400)

    config = await gw.job_registry.reload_job(
        name,
        str(job_path),
        job_type=JobType(job_type_value),
        interval=body.get("interval", 0.0),
        priority=body.get("priority", 0),
        retries=body.get("retries", 3),
        metadata=body.get("metadata", {}),
        auto_start=body.get("auto_start", True),
    )

    if not config:
        return _error("Job 注册失败，请检查代码格式", 500)

    return _json_response({
        "success": True,
        "message": f"Job {name} 已上传并注册",
        "path": str(job_path),
        "config": config.to_dict(),
    })

# ---------------------------------------------------------------------
# Built-in: Canvas Video
# ---------------------------------------------------------------------
async def canvas_sync_handler(request: web.Request) -> web.Response:
    """手动触发课程同步"""
    gw: AutoCanvasGateway = request.app["gateway"]

    try:
        body = await request.json()
    except:
        return _error("请求体必须是合法 JSON")

    course_id = body.get("course_id")
    if not course_id:
        return _error("缺少 course_id")

    from workers.video_manager import sync_course_videos

    async def sync_job(g):
        await sync_course_videos(g, course_id)

    job_name = f"manual_sync_{course_id}_{datetime.now().strftime('%H%M%S')}"
    gw.job_registry.register_basic(job_name, sync_job)
    gw.job_registry.start(job_name)

    return _json_response({
        "success": True,
        "job_name": job_name,
        "message": f"课程 {course_id} 同步任务已启动",
    })

async def canvas_live_monitor_handler(request: web.Request) -> web.Response:
    """手动触发直播监听"""
    gw: AutoCanvasGateway = request.app["gateway"]

    try:
        body = await request.json()
    except:
        return _error("请求体必须是合法 JSON")

    course_id = body.get("course_id")
    lecture_id = body.get("lecture_id")

    if not course_id or not lecture_id:
        return _error("缺少 course_id 或 lecture_id")

    from workers.live_worker import process_live

    job_name = f"live_monitor_{lecture_id}"

    async def live_job(g):
        await process_live(g, course_id, lecture_id)

    gw.job_registry.register_basic(job_name, live_job)
    gw.job_registry.start(job_name)

    return _json_response({
        "success": True,
        "job_name": job_name,
        "message": f"直播 {lecture_id} 监听已启动",
    })

async def canvas_vod_queue_handler(request: web.Request) -> web.Response:
    """将 VOD 加入转写队列"""
    gw: AutoCanvasGateway = request.app["gateway"]

    try:
        body = await request.json()
    except:
        return _error("请求体必须是合法 JSON")

    course_id = body.get("course_id")
    video_id = body.get("video_id")
    priority = body.get("priority", 0)

    if not course_id or not video_id:
        return _error("缺少 course_id 或 video_id")

    from workers.replay_worker import process_vod

    job_name = f"vod_process_{course_id}_{video_id}"

    async def vod_job(g):
        await process_vod(g, course_id, video_id)

    gw.job_registry.register_queued(job_name, vod_job, priority=priority)
    gw.job_registry.start(job_name)

    return _json_response({
        "success": True,
        "message": f"VOD {video_id} 已加入队列",
    })

async def canvas_videos_handler(request: web.Request) -> web.Response:
    """获取课程视频列表"""
    course_id = request.query.get("course_id")
    if not course_id:
        return _error("缺少 course_id 参数", 422)

    from workers.video_manager import get_course_video_state
    state = get_course_video_state(course_id)

    return _json_response({
        "success": True,
        "course_id": course_id,
        "live": state.get("live", []),
        "vod": state.get("vod", []),
    })

# ---------------------------------------------------------------------
# Queue Management
# ---------------------------------------------------------------------
async def vod_queue_handler(request: web.Request) -> web.Response:
    """VOD 队列管理"""
    gw: AutoCanvasGateway = request.app["gateway"]

    try:
        body = await request.json()
    except:
        return _error("请求体必须是合法 JSON")

    course_id = body.get("course_id")
    action = body.get("action", "list")  # list, reset, skip

    if not course_id:
        return _error("缺少 course_id")

    from workers.video_manager import (
        get_vod_queue,
        reset_vod,
        skip_vod,
    )

    if action == "list":
        vod_list = get_vod_queue(course_id)
        return _json_response({
            "success": True,
            "course_id": course_id,
            "vod": vod_list,
        })
    elif action == "reset":
        video_id = body.get("video_id")
        if not video_id:
            return _error("reset 操作需要提供 video_id")
        ok = reset_vod(gw, course_id, video_id)
        return _json_response({
            "success": ok,
            "message": f"VOD {video_id} 已重置" if ok else "VOD 不存在",
        })
    elif action == "skip":
        video_id = body.get("video_id")
        if not video_id:
            return _error("skip 操作需要提供 video_id")
        ok = skip_vod(gw, course_id, video_id)
        return _json_response({
            "success": ok,
            "message": f"VOD {video_id} 已跳过" if ok else "VOD 不存在",
        })
    else:
        return _error(f"未知 action: {action}")


async def live_queue_handler(request: web.Request) -> web.Response:
    """直播队列管理"""
    gw: AutoCanvasGateway = request.app["gateway"]

    try:
        body = await request.json()
    except:
        return _error("请求体必须是合法 JSON")

    course_id = body.get("course_id")
    action = body.get("action", "list")  # list, monitor, stop

    if not course_id:
        return _error("缺少 course_id")

    from workers.video_manager import get_live_queue

    if action == "list":
        live_list = get_live_queue(course_id)
        return _json_response({
            "success": True,
            "course_id": course_id,
            "live": live_list,
        })
    elif action == "monitor":
        lecture_id = body.get("lecture_id")
        if not lecture_id:
            return _error("monitor 操作需要提供 lecture_id")
        # 调用直播监听
        from workers.live_worker import process_live
        job_name = f"live_monitor_{lecture_id}"
        async def live_job(g):
            await process_live(g, course_id, lecture_id)
        gw.job_registry.register_queued(job_name, live_job, priority=1)
        gw.job_registry.start(job_name)
        return _json_response({
            "success": True,
            "job_name": job_name,
            "message": f"直播 {lecture_id} 监听已启动",
        })
    elif action == "stop":
        lecture_id = body.get("lecture_id")
        if not lecture_id:
            return _error("stop 操作需要提供 lecture_id")
        job_name = f"live_monitor_{lecture_id}"
        gw.job_registry.cancel(job_name)
        gw.job_registry.delete(job_name)
        return _json_response({
            "success": True,
            "message": f"直播 {lecture_id} 监听已停止",
        })
    else:
        return _error(f"未知 action: {action}")


# ---------------------------------------------------------------------
# Schedule Management
# ---------------------------------------------------------------------
async def schedules_list_handler(request: web.Request) -> web.Response:
    """列出 Schedule"""
    course_id = request.query.get("course_id")
    status = request.query.get("status")

    from utils.course_data import get_schedules
    schedules = get_schedules(course_id=course_id, status=status)

    return _json_response({"success": True, "schedules": schedules})


async def schedules_pending_handler(request: web.Request) -> web.Response:
    """获取待执行的 Schedule"""
    from utils.course_data import get_pending_schedules

    before = datetime.now()
    pending = get_pending_schedules(before=before)

    return _json_response({"success": True, "count": len(pending), "schedules": pending})


async def schedules_cancel_handler(request: web.Request) -> web.Response:
    """取消 Schedule"""
    schedule_id = request.match_info["id"]

    from utils.course_data import mark_schedule_executed
    mark_schedule_executed(schedule_id, success=False, result={"cancelled": True})

    return _json_response({"success": True, "message": f"Schedule {schedule_id} 已取消"})


# ---------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------
def create_app(gateway: AutoCanvasGateway) -> web.Application:
    app = web.Application()
    app["gateway"] = gateway

    # Health
    app.router.add_get("/health", health_handler)

    # Job CRUD (Foundation)
    app.router.add_get("/jobs", jobs_list_handler)
    app.router.add_post("/jobs", jobs_create_handler)
    app.router.add_get("/jobs/{name}", jobs_detail_handler)
    app.router.add_patch("/jobs/{name}", jobs_update_handler)
    app.router.add_delete("/jobs/{name}", jobs_delete_handler)
    app.router.add_post("/jobs/{name}/start", jobs_start_handler)
    app.router.add_post("/jobs/{name}/pause", jobs_pause_handler)
    app.router.add_post("/jobs/{name}/resume", jobs_resume_handler)
    app.router.add_post("/jobs/{name}/reload", jobs_reload_handler)

    # Job Management (兼容端点)
    app.router.add_get("/plugins", plugins_list_handler)
    app.router.add_post("/plugins", plugins_upload_handler)

    # Queue Management
    app.router.add_get("/queue/vod", vod_queue_handler)
    app.router.add_post("/queue/vod", vod_queue_handler)
    app.router.add_get("/queue/live", live_queue_handler)
    app.router.add_post("/queue/live", live_queue_handler)

    # Schedule Management
    app.router.add_get("/schedules", schedules_list_handler)
    app.router.add_get("/schedules/pending", schedules_pending_handler)
    app.router.add_post("/schedules/{id}/cancel", schedules_cancel_handler)

    # Built-in: Canvas
    app.router.add_get("/videos", canvas_videos_handler)
    app.router.add_post("/canvas/sync", canvas_sync_handler)
    app.router.add_post("/canvas/live/monitor", canvas_live_monitor_handler)
    app.router.add_post("/canvas/vod/queue", canvas_vod_queue_handler)

    return app


async def run_video_api(
    gateway: AutoCanvasGateway,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> None:
    """启动 API 服务"""
    app = create_app(gateway)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    gateway.logger.info("[api] 服务已启动: http://%s:%d", host, port)

    while not gateway._shutting_down:
        await asyncio.sleep(1.0)

    await runner.cleanup()
    gateway.logger.info("[api] 服务已关闭")
