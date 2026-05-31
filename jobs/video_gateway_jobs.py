#!/usr/bin/env python3
"""
Video Gateway Jobs
------------------
将视频模块的周期性同步任务与 API 服务器注册到 Gateway 的辅助接口。

使用示例：
    from gateway import AutoCanvasGateway
    from video_gateway_jobs import register_video_jobs, start_video_api

    gw = AutoCanvasGateway()
    register_video_jobs(gw, course_id=YOUR_COURSE_ID, sync_interval=1800)
    start_video_api(gw, host="0.0.0.0", port=8080)
    asyncio.run(gw.start())
"""
from __future__ import annotations

from typing import Optional

from gateway import AutoCanvasGateway
from workers.video_manager import sync_course_videos, queue_pending_vods
from api.video_api import run_video_api


def register_video_jobs(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    *,
    sync_interval: float = 1800.0,  # 默认30分钟
    auto_sync_at_start: bool = True,
    enable_auto_scheduler: bool = True,  # 启用全自动调度
) -> None:
    """
    注册视频模块的周期性任务。

    包括：
    1. video_sync_<course_id>: 定时同步视频元数据（Loop 层）
    2. auto_scheduler_<course_id>: 全自动调度器（扫描直播、自动监听、自动迁移）
    """
    job_name = f"video_sync_{course_id}"

    async def _sync_job(gw: AutoCanvasGateway) -> None:
        await sync_course_videos(gw, course_id)

    gateway.register_job(
        job_name,
        _sync_job,
        interval=sync_interval,
        enabled=True,
    )

    # 若需要立即执行一次同步，可注册一个只跑一次的前置任务
    if auto_sync_at_start:
        gateway.register_job(
            f"video_sync_{course_id}_initial",
            _sync_job,
            enabled=True,
        )

    # 注册全自动调度器（核心自动化功能）
    if enable_auto_scheduler:
        async def _auto_scheduler_loop(gw: AutoCanvasGateway) -> None:
            from auto_scheduler import auto_scheduler_loop
            await auto_scheduler_loop(
                gw,
                str(course_id),
                sync_interval=sync_interval,
                lookahead_hours=24.0,
                auto_preheat_model=True,
            )

        gateway.register_job(
            f"auto_scheduler_{course_id}",
            _auto_scheduler_loop,
            interval=0,  # auto_scheduler_loop 内部自己控制循环
            enabled=True,
        )
        gateway.logger.info(
            "[video_jobs] 已启用全自动调度器: course=%s, sync_interval=%.0fmin",
            course_id, sync_interval / 60
        )


def start_video_api(
    gateway: AutoCanvasGateway,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> None:
    """
    动态注册 video_api aiohttp 服务作为 Gateway Job。
    由于 server 是常驻的，interval 设为 0（只启动一次）并通过内部循环挂起。
    """
    async def _api_job(gw: AutoCanvasGateway) -> None:
        await run_video_api(gw, host=host, port=port)

    gateway.register_job(
        "video_api_server",
        _api_job,
        interval=0,
        enabled=True,
    )


def build_video_gateway(
    course_id: int | str,
    *,
    sync_interval: float = 1800.0,  # 30分钟
    api_host: str = "0.0.0.0",
    api_port: int = 8080,
    auto_sync_at_start: bool = True,
    enable_auto_scheduler: bool = True,
) -> AutoCanvasGateway:
    """
    一键构建已配置视频模块的 Gateway 实例（尚未启动）。
    """
    gateway = AutoCanvasGateway()
    register_video_jobs(
        gateway,
        course_id,
        sync_interval=sync_interval,
        auto_sync_at_start=auto_sync_at_start,
        enable_auto_scheduler=enable_auto_scheduler,
    )
    start_video_api(gateway, host=api_host, port=api_port)
    return gateway
