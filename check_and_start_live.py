#!/usr/bin/env python3
"""
立即检查并启动直播监听
"""
import asyncio
from datetime import datetime
from gateway import AutoCanvasGateway
from video_manager import sync_course_videos, get_course_video_state, mark_live_monitoring
from live_worker import process_live

COURSE_ID = 0

async def main():
    gw = AutoCanvasGateway(auto_prompt=False)

    # 初始化 async 资源
    gw._init_async_resources()

    # 初始化 session
    print("[1/4] 初始化 Gateway session...")
    await gw._ensure_session()

    # 立即同步课程视频
    print(f"[2/4] 同步课程 {COURSE_ID} 视频列表...")
    await sync_course_videos(gw, COURSE_ID)

    # 获取视频状态
    print("[3/4] 查找正在进行的直播...")
    course_state = get_course_video_state(COURSE_ID)
    live_list = course_state.get("live", [])

    now = datetime.now()
    active_live = None

    for live in live_list:
        begin_time_str = live.get("begin_time", "")
        end_time_str = live.get("end_time", "")

        try:
            begin_time = datetime.strptime(begin_time_str, "%Y-%m-%d %H:%M:%S")
            end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue

        if begin_time <= now < end_time:
            active_live = live
            print(f"  ✓ 找到进行中的直播:")
            print(f"    - 课程: {live.get('name', 'N/A')}")
            print(f"    - Lecture ID: {live.get('id', 'N/A')}")
            print(f"    - 时间: {begin_time_str} ~ {end_time_str}")
            print(f"    - 教师: {live.get('teacher', 'N/A')}")
            print(f"    - 流数量: {len(live.get('streams', []))}")
            for s in live.get('streams', []):
                print(f"      - [{s.get('quality', 'N/A')}] {s.get('feed_type', 'unknown')} - {s.get('url', 'N/A')[:60]}...")
            break

    if not active_live:
        print("  ✗ 没有找到正在进行的直播")
        print(f"\n当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print("\n未来24小时内的直播:")
        for live in live_list:
            begin_time_str = live.get("begin_time", "")
            try:
                begin_time = datetime.strptime(begin_time_str, "%Y-%m-%d %H:%M:%S")
            except:
                continue
            if now <= begin_time <= now + __import__('datetime').timedelta(hours=24):
                print(f"  - {live.get('name')} @ {begin_time_str}")
        return

    # 检查是否已在监听
    lecture_id = active_live.get('id')
    job_name = f"live_monitor_{lecture_id}"

    if job_name in gw.state.jobs:
        print(f"\n  ⚠️  直播监听任务 {lecture_id} 已在运行中")
        return

    if active_live.get('monitoring'):
        print(f"\n  ⚠️  直播已在监听中 (monitoring=true)")
        return

    # 启动监听
    print(f"\n[4/4] 启动直播监听 Job: {job_name}...")

    async def _live_job(gw):
        await process_live(gw, COURSE_ID, lecture_id)

    gw.start_job_runtime(
        job_name, _live_job,
        interval=0,
        retries=10,
        retry_delay=30,
    )

    print(f"  ✓ 直播监听已启动!")
    print(f"\n监控命令:")
    print(f"  tail -f logs/gateway.log | grep -i 'live\\|{lecture_id}'")
    print(f"\n转写输出位置:")
    print(f"  transcript/ (将在监听开始后生成)")

    # 保持运行一段时间让 Job 启动
    print(f"\n等待 5 秒让 Job 启动...")
    await asyncio.sleep(5)

    # 检查 Job 状态
    job_state = gw.get_job_state(job_name)
    if job_state.get('task_running'):
        print(f"  ✓ Job 正在运行!")
    else:
        print(f"  ⚠️ Job 可能尚未启动或已结束")
        print(f"    状态: {job_state}")

if __name__ == "__main__":
    asyncio.run(main())
