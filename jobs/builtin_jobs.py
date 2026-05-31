#!/usr/bin/env python3
"""
Built-in Jobs - Canvas 核心任务
--------------------------------
1. update_timetable: 每周同步 Canvas 获取有哪些课
2. scan_live: 每小时扫描所有课程的直播并注册 Schedule
3. schedule_executor: 执行待触发的 Schedule
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway import AutoCanvasGateway

from utils.course_data import (
    update_timetable,
    get_timetable,
    get_course_ids,
    add_schedule,
    get_pending_schedules,
    get_schedules,
    mark_schedule_executed,
    SCHEDULE_FILE,
    _load_json,
    _save_json,
)
from api.canvas_video import get_live_list, get_live_video_info
from workers.video_manager import sync_course_videos


def register_builtin_jobs(gw) -> None:
    """注册所有内置 Job"""
    registry = gw.job_registry

    # Loop Job 1: 每周同步课程表（有哪些课可用）
    async def update_timetable_job(g):
        await _sync_timetable(g)

    registry.register_loop(
        "update_timetable",
        update_timetable_job,
        interval=604800,  # 7天 = 一周
        metadata={"what": "同步 Canvas 课程表", "why": "每周一次，知道有哪些课"},
    )

    # Loop Job 2: 每小时扫描所有课程的直播
    async def scan_live_job(g):
        await _scan_all_courses(g)

    registry.register_loop(
        "scan_live",
        scan_live_job,
        interval=3600,  # 1小时
        metadata={"what": "扫描所有课程的直播", "why": "1小时检查一次，注册 Schedule"},
    )

    # Loop Job 3: 执行待触发的 Schedule（每分钟检查）
    async def schedule_executor_job(g):
        await _execute_schedules(g)

    registry.register_loop(
        "schedule_executor",
        schedule_executor_job,
        interval=60,  # 1分钟检查一次
        metadata={"what": "执行待触发的 Schedule", "why": "每分钟检查，准时触发"},
    )

    gw.logger.info("[builtin_jobs] 已注册: update_timetable(周), scan_live(小时), schedule_executor(分)")

    # -----------------------------------------------------------------
    # 事件通知 handler（stub，按需填充具体通知逻辑）
    # -----------------------------------------------------------------
    async def on_auth_failure(gateway, event):
        """认证失败通知。按需替换为 Slack/邮件/Bark 等推送。"""
        gateway.logger.warning(
            "[notify] 认证失败: source=%s, error=%s",
            event.get("source", "?"),
            event.get("error", "?"),
        )
        # TODO: 在这里添加通知逻辑
        # requests.post("https://slack.webhook/...", json={...})

    async def on_request_failure(gateway, event):
        """请求失败通知。按需替换为具体推送逻辑。"""
        gateway.logger.warning(
            "[notify] 请求失败: url=%s, status=%s, error=%s",
            event.get("url", "?"),
            event.get("status", "?"),
            event.get("error", "?"),
        )

    gw.on_event("auth_failure", on_auth_failure)
    gw.on_event("request_failure", on_request_failure)
    gw.logger.info("[builtin_jobs] 已注册通知 handler: auth_failure, request_failure")


# ---------------------------------------------------------------------
# Job 实现
# ---------------------------------------------------------------------

async def _sync_timetable(gw) -> None:
    """同步课程表（有哪些课可用）"""
    gw.logger.info("[timetable] 开始同步课程表...")

    try:
        from api.canvas_video import get_user_courses
        session = gw.session
        courses = await gw.run_sync(get_user_courses, session)
        update_timetable(courses)
        gw.logger.info("[timetable] 已同步 %d 门课", len(courses))
        for c in courses:
            gw.logger.info("  - %s: %s", c["id"], c["name"])
    except Exception as e:
        gw.logger.error("[timetable] 同步失败: %s", e)
        raise


async def _scan_all_courses(gw) -> None:
    """扫描所有课程的直播"""
    gw.logger.info("[scan_live] 开始扫描直播...")

    course_ids = get_course_ids()
    if not course_ids:
        gw.logger.warning("[scan_live] 课程表为空，请先同步课程表")
        return

    now = datetime.now()
    lookahead = now + timedelta(hours=24)

    for course_id in course_ids:
        lives = []
        try:
            session = gw.session
            lives = await gw.run_sync(get_live_list, session, course_id)
        except Exception as e:
            gw.logger.error("[scan_live] 扫描课程 %s 直播失败: %s", course_id, e)

        try:
            for live in lives:
                lecture_id = live.get("id")
                begin_time_str = (
                    live.get("startTime")
                    or live.get("courBeginTime")
                    or live.get("begin_time", "")
                )
                end_time_str = (
                    live.get("endTime")
                    or live.get("courEndTime")
                    or live.get("end_time", "")
                )

                try:
                    begin_time = datetime.strptime(begin_time_str, "%Y-%m-%d %H:%M:%S")
                    end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue

                # 只关注未来 24h 内开始，或当前正在进行的直播
                if not (begin_time <= lookahead and end_time >= now):
                    continue

                # 注册开始监听 Schedule（提前10分钟）
                start_schedule_id = f"monitor_start_{course_id}_{lecture_id}"
                start_trigger = begin_time - timedelta(minutes=10)

                add_schedule(
                    schedule_id=start_schedule_id,
                    schedule_type="monitor_start",
                    course_id=course_id,
                    lecture_id=lecture_id,
                    trigger_at=start_trigger,
                    metadata={
                        "scheduled_begin": begin_time.isoformat(),
                        "scheduled_end": end_time.isoformat(),
                        "course_name": live.get("courseName") or live.get("courName", ""),
                        "description": f"课前10分钟启动直播监听",
                    },
                )

                # 注册结束整理 Schedule（课程结束时刻）
                end_schedule_id = f"monitor_end_{course_id}_{lecture_id}"

                add_schedule(
                    schedule_id=end_schedule_id,
                    schedule_type="monitor_end",
                    course_id=course_id,
                    lecture_id=lecture_id,
                    trigger_at=end_time,
                    metadata={
                        "scheduled_begin": begin_time.isoformat(),
                        "scheduled_end": end_time.isoformat(),
                        "course_name": live.get("courseName") or live.get("courName", ""),
                        "description": f"课程结束，停止监听并整理",
                    },
                )

                gw.logger.info(
                    "[scan_live] 已为 %s 注册 Schedule: start@%s, end@%s",
                    lecture_id[:20],
                    start_trigger.strftime("%H:%M"),
                    end_time.strftime("%H:%M"),
                )

            # 同步该课程的视频信息（含流地址）到 video_state.json
            if lives:
                try:
                    await sync_course_videos(gw, course_id, include_live=True, include_vod=False)
                except Exception as e:
                    gw.logger.error("[scan_live] 同步课程 %s 视频信息失败: %s", course_id, e)

        except Exception as e:
            gw.logger.error("[scan_live] 扫描课程 %s 失败: %s", course_id, e)

        try:
            await sync_course_videos(gw, course_id, include_live=False, include_vod=True)
        except Exception as e:
            gw.logger.error("[scan_live] 同步课程 %s 回放信息失败: %s", course_id, e)

    gw.logger.info("[scan_live] 扫描完成")


async def _execute_schedules(gw) -> None:
    """执行待触发的 Schedule"""
    now = datetime.now()

    # Gateway 重启后恢复进行中直播的监听
    await _recover_live_monitors(gw, now)

    pending = get_pending_schedules(before=now)

    for schedule in pending:
        schedule_id = schedule["id"]
        schedule_type = schedule["type"]
        course_id = schedule["course_id"]
        lecture_id = schedule["lecture_id"]
        metadata = schedule.get("metadata", {})

        gw.logger.info("[schedule] 执行 %s: %s", schedule_type, schedule_id)

        try:
            if schedule_type == "monitor_start":
                started = await _execute_monitor_start(gw, course_id, lecture_id, metadata, schedule)
                if not started:
                    # 需要重试，暂不标记为已执行
                    continue
            elif schedule_type == "monitor_end":
                await _execute_monitor_end(gw, course_id, lecture_id, metadata)

            mark_schedule_executed(schedule_id, success=True)
        except Exception as e:
            gw.logger.exception("[schedule] 执行失败 %s: %s", schedule_id, e)
            mark_schedule_executed(schedule_id, success=False, result={"error": str(e)})



async def _recover_live_monitors(gw, now: datetime) -> None:
    """检查正在进行的直播，若监听 Job 已消失则重置 schedule 以便恢复。"""
    from workers.video_manager import get_course_video_state

    for course_id in get_course_ids():
        course_state = get_course_video_state(course_id)
        for live in course_state.get("live", []):
            lecture_id = live.get("id")
            if not lecture_id:
                continue
            begin_time_str = live.get("begin_time", "")
            end_time_str = live.get("end_time", "")
            try:
                begin_time = datetime.strptime(begin_time_str, "%Y-%m-%d %H:%M:%S")
                end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

            # 只处理进行中且已开播的直播
            if not (begin_time <= now < end_time):
                continue

            job_name = f"live_monitor_{lecture_id}"
            job = gw.job_registry._jobs.get(job_name)
            if job and job.status == "running" and job.task is not None and not job.task.done():
                continue  # 监听正常

            # 查找对应的 monitor_start schedule
            schedules = get_schedules(course_id=course_id)
            start_schedule = None
            for s in schedules:
                if s.get("type") == "monitor_start" and s.get("lecture_id") == lecture_id:
                    start_schedule = s
                    break

            if start_schedule and start_schedule.get("status") == "completed":
                gw.logger.warning(
                    "[recover] 检测到进行中直播 %s 监听中断，自动恢复",
                    lecture_id[:20]
                )
                data = _load_json(SCHEDULE_FILE, {"schedules": []})
                for s in data["schedules"]:
                    if s["id"] == start_schedule["id"]:
                        s["status"] = "pending"
                        s.pop("executed_at", None)
                        s.pop("result", None)
                        _save_json(SCHEDULE_FILE, data)
                        break

async def _execute_monitor_start(gw, course_id, lecture_id, metadata, schedule) -> bool:
    """
    执行开始监听。
    返回 True 表示已成功启动（或已放弃），可以标记 schedule 为已完成；
    返回 False 表示需要下一分钟重试。
    """
    from workers.live_worker import process_live
    from workers.video_manager import get_course_video_state, sync_course_videos

    trigger_at = datetime.fromisoformat(schedule["trigger_at"])
    now = datetime.now()

    # 1. 尝试获取直播的流地址
    course_state = get_course_video_state(course_id)
    entry = None
    for live in course_state.get("live", []):
        if live.get("id") == lecture_id:
            entry = live
            break

    has_streams = entry and bool(entry.get("streams"))

    if not has_streams:
        # 现场再拉一次试试
        try:
            await sync_course_videos(gw, course_id, include_live=True, include_vod=False)
            course_state = get_course_video_state(course_id)
            for live in course_state.get("live", []):
                if live.get("id") == lecture_id:
                    entry = live
                    break
            has_streams = entry and bool(entry.get("streams"))
        except Exception as e:
            gw.logger.error("[monitor_start] 同步课程 %s 流地址失败: %s", course_id, e)

    if not has_streams:
        if now < trigger_at + timedelta(minutes=15):
            gw.logger.warning(
                "[monitor_start] 直播 %s 暂无流地址，1 分钟后重试（还有 %.0f 分钟）",
                lecture_id[:20],
                (trigger_at + timedelta(minutes=15) - now).total_seconds() / 60
            )
            return False
        else:
            gw.logger.error(
                "[monitor_start] 直播 %s 超过 15 分钟仍未获取到流地址，放弃监听",
                lecture_id[:20]
            )
            raise RuntimeError("15 分钟内未获取到直播流地址")

    # 2. 有流地址，启动监听 Job
    job_name = f"live_monitor_{lecture_id}"

    # 防重复: 如果同名 Job 已经在跑就不要再注册
    existing = gw.job_registry.get(job_name)
    if existing and existing.task is not None and not existing.task.done():
        gw.logger.info("[monitor_start] 监听 Job %s 已在运行，跳过重复启动", job_name)
        return True

    async def monitor_job(g):
        await process_live(g, course_id, lecture_id, scheduled_end=metadata.get("scheduled_end"))

    gw.job_registry.register_queued(
        job_name,
        monitor_job,
        priority=1,
        metadata={
            "course_id": course_id,
            "lecture_id": lecture_id,
            "scheduled_begin": metadata.get("scheduled_begin"),
            "scheduled_end": metadata.get("scheduled_end"),
        },
    )
    gw.job_registry.start(job_name)

    gw.logger.info("[monitor_start] 已启动监听 Job: %s", job_name)
    return True


async def _execute_monitor_end(gw, course_id, lecture_id, metadata) -> None:
    """执行结束整理（析构函数模式）"""
    job_name = f"live_monitor_{lecture_id}"

    # 1. 析构：杀掉对应的监听 Job
    if job_name in gw.job_registry._jobs:
        gw.logger.info("[monitor_end] 停止监听 Job: %s", job_name)
        gw.job_registry.cancel(job_name)
        gw.job_registry.delete(job_name)

    # 2. 调用专门的结束 Job 进行整理
    async def finalize_job(g):
        await _finalize_live(g, course_id, lecture_id, metadata)

    finalize_name = f"finalize_{lecture_id}"
    gw.job_registry.register_basic(
        finalize_name,
        finalize_job,
        metadata={"description": "直播结束整理", "lecture_id": lecture_id},
    )
    gw.job_registry.start(finalize_name)

    gw.logger.info("[monitor_end] 已停止监听，启动整理 Job: %s", finalize_name)


async def _finalize_live(gw, course_id, lecture_id, metadata) -> None:
    """直播结束后的整理工作 - 只扫描这门课的回放"""
    gw.logger.info("[finalize] 开始整理直播 %s", lecture_id)

    # 1. 检查转写结果
    transcript_path = metadata.get("transcript_path")

    # 2. 扫描这门课的新回放
    await _scan_course_vods(gw, course_id)

    # 3. 如果直播转写完成，关联到回放
    if transcript_path:
        gw.logger.info("[finalize] 转写文件: %s", transcript_path)


async def _scan_course_vods(gw, course_id: str) -> int:
    """扫描单个课程的回放，使用 video_manager 统一存储到 video_state.json"""
    try:
        from workers.video_manager import sync_course_videos, get_course_video_state

        # 使用统一的同步函数，数据存入 video_state.json
        await sync_course_videos(gw, course_id, include_live=False, include_vod=True)

        # 获取同步后的回放数量
        course_state = get_course_video_state(course_id)
        vod_count = len(course_state.get("vod", []))

        gw.logger.info("[scan_vods] 课程 %s 扫描完成，共 %d 条回放", course_id, vod_count)
        return vod_count

    except Exception as e:
        gw.logger.error("[scan_vods] 扫描课程 %s 失败: %s", course_id, e)
        return 0


async def scan_all_vods(gw: AutoCanvasGateway) -> None:
    """手动触发：扫描所有课程的回放（第一次全量同步）"""
    gw.logger.info("[scan_all_vods] 开始全量扫描所有课程回放...")

    course_ids = get_course_ids()
    if not course_ids:
        gw.logger.warning("[scan_all_vods] 课程表为空，请先同步课程表")
        return

    total = 0
    for course_id in course_ids:
        count = await _scan_course_vods(gw, course_id)
        total += count

    gw.logger.info("[scan_all_vods] 全量扫描完成，共新增 %d 条回放", total)
