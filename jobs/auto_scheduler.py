#!/usr/bin/env python3
"""
Auto Scheduler
--------------
自动化调度器（Loop 层）。

职责：
1. 周期性同步课程视频列表（每30分钟）
2. 扫描未来直播，自动注册 Schedule 监听任务
3. 检测课程切换，自动调度连续课程
4. 自动预热 ASR 模型

这是系统的"大脑"，负责决定"什么时候做什么"。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Any

if TYPE_CHECKING:
    from gateway import AutoCanvasGateway

from schedule_manager import (
    ScheduleManager,
    schedule_live_monitor,
    schedule_live_migrate,
    schedule_course_sync,
)
from workers.video_manager import get_course_video_state
from workers.replay_worker import _load_asr_model


class AutoScheduler:
    """
    自动化调度器。
    作为 Gateway 的 Loop 层任务持续运行。
    """

    def __init__(
        self,
        gateway: "AutoCanvasGateway",
        course_id: str,
        *,
        sync_interval: float = 1800.0,  # 30分钟同步一次
        lookahead_hours: float = 24.0,  # 提前24小时扫描直播
        auto_preheat_model: bool = True,
    ):
        self.gateway = gateway
        self.course_id = str(course_id)
        self.sync_interval = sync_interval
        self.lookahead_hours = lookahead_hours
        self.auto_preheat_model = auto_preheat_model
        self.logger = gateway.logger

        # 内部状态
        self._model_preheated: bool = False
        self._last_sync_time: Optional[datetime] = None
        self._scheduled_lectures: set = set()  # 已调度的 lecture_id

    async def run(self) -> None:
        """
        主循环。由 Gateway 作为 Job 调用。
        每次循环执行一次完整的检查和调度。
        """
        self.logger.info(
            "[auto_scheduler] 开始调度循环: course=%s, sync_interval=%.0fmin",
            self.course_id, self.sync_interval / 60
        )

        # 首次运行：预热模型
        if self.auto_preheat_model and not self._model_preheated:
            await self._preheat_model()

        # 执行课程同步
        await self._sync_course()

        # 扫描并调度直播
        await self._scan_and_schedule_live()

        # 检查即将开始的直播，确保 Schedule 已注册
        await self._ensure_live_schedules()

        self.logger.info("[auto_scheduler] 本轮调度完成，%.0f分钟后下次检查", self.sync_interval / 60)

    async def _preheat_model(self) -> None:
        """预热 ASR 模型，避免首次任务延迟。"""
        self.logger.info("[auto_scheduler] 正在预热 ASR 模型...")
        try:
            await self.gateway.run_sync(_load_asr_model)
            self._model_preheated = True
            self.logger.info("[auto_scheduler] ASR 模型预热完成")
        except Exception as e:
            self.logger.error("[auto_scheduler] 模型预热失败: %s", e)

    async def _sync_course(self) -> None:
        """同步课程视频列表。"""
        from workers.video_manager import sync_course_videos
        try:
            await sync_course_videos(self.gateway, self.course_id)
            self._last_sync_time = datetime.now()
            self.logger.info("[auto_scheduler] 课程 %s 同步完成", self.course_id)
        except Exception as e:
            self.logger.error("[auto_scheduler] 课程同步失败: %s", e)

    async def _scan_and_schedule_live(self) -> None:
        """
        扫描未来直播，自动注册 Schedule。
        - 对于即将开始的直播（<24h），注册监听 Schedule
        - 对于即将结束的直播，注册迁移 Schedule
        """
        course_state = get_course_video_state(self.course_id)
        live_list = course_state.get("live", [])

        now = datetime.now()
        lookahead = now + timedelta(hours=self.lookahead_hours)

        scheduled_count = 0
        for live in live_list:
            lecture_id = live.get("id")
            if not lecture_id:
                continue

            begin_time_str = live.get("begin_time", "")
            end_time_str = live.get("end_time", "")

            try:
                begin_time = datetime.strptime(begin_time_str, "%Y-%m-%d %H:%M:%S")
                end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue

            # 只关注未来24小时内的直播
            if not (now <= begin_time <= lookahead):
                continue

            schedule_id = f"live_monitor_{self.course_id}_{lecture_id}"

            # 检查是否已调度
            if lecture_id not in self._scheduled_lectures:
                # 注册直播监听 Schedule（提前10分钟）
                entry = schedule_live_monitor(
                    self.gateway,
                    self.course_id,
                    lecture_id,
                    begin_time,
                    buffer_minutes=10,
                )
                self._scheduled_lectures.add(lecture_id)
                scheduled_count += 1
                self.logger.info(
                    "[auto_scheduler] 已调度直播监听: %s @ %s (提前10分钟启动)",
                    lecture_id, begin_time_str
                )

            # 同时注册直播结束后的迁移任务
            migrate_id = f"live_migrate_{self.course_id}_{lecture_id}"
            # 如果结束时间在将来，也注册迁移 Schedule
            if end_time > now:
                schedule_live_migrate(
                    self.gateway,
                    self.course_id,
                    lecture_id,
                    end_time,
                    delay_minutes=5,  # 结束后5分钟执行迁移
                )

        if scheduled_count > 0:
            self.logger.info("[auto_scheduler] 本轮新调度 %d 个直播监听任务", scheduled_count)

    async def _ensure_live_schedules(self) -> None:
        """
        确保即将开始的直播都有对应的 Schedule。
        处理边缘情况：Gateway 重启后恢复 Schedule。
        """
        manager: ScheduleManager = getattr(self.gateway, "_schedule_manager", None)
        if not manager:
            return

        course_state = get_course_video_state(self.course_id)
        live_list = course_state.get("live", [])

        now = datetime.now()

        for live in live_list:
            lecture_id = live.get("id")
            if not lecture_id or lecture_id in self._scheduled_lectures:
                continue

            begin_time_str = live.get("begin_time", "")
            try:
                begin_time = datetime.strptime(begin_time_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue

            # 对于已经开始但未结束的直播，立即启动监听
            end_time_str = live.get("end_time", "")
            try:
                end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                end_time = begin_time + timedelta(hours=2)  # 默认2小时

            if begin_time <= now < end_time:
                # 直播进行中，检查是否已在监听
                job_name = f"live_monitor_{lecture_id}"
                if job_name not in self.gateway.state.jobs:
                    self.logger.info(
                        "[auto_scheduler] 检测到进行中的直播 %s，立即启动监听",
                        lecture_id
                    )
                    from live_worker import process_live

                    async def _live_job(gw, cid=self.course_id, lid=lecture_id):
                        await process_live(gw, cid, lid)

                    self.gateway.start_job_runtime(
                        job_name, _live_job,
                        interval=0,
                        retries=5,
                        retry_delay=30,
                    )
                    self._scheduled_lectures.add(lecture_id)


async def auto_scheduler_loop(
    gateway: "AutoCanvasGateway",
    course_id: str,
    **kwargs
) -> None:
    """
    Gateway Job 入口：自动化调度器循环。
    每隔 sync_interval 秒执行一次完整调度。
    """
    scheduler = AutoScheduler(gateway, course_id, **kwargs)

    while not gateway._shutting_down:
        await scheduler.run()
        # 等待下次循环
        for _ in range(int(scheduler.sync_interval)):
            if gateway._shutting_down:
                break
            await asyncio.sleep(1)
