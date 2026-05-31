#!/usr/bin/env python3
"""
手动触发：扫描所有课程的回放
"""
import asyncio
from gateway import AutoCanvasGateway
from utils.course_data import get_course_ids, update_timetable
from jobs.builtin_jobs import _scan_course_vods
from api.canvas_video import get_user_courses

async def main():
    gw = AutoCanvasGateway(auto_prompt=False)
    gw._init_async()
    await gw._ensure_session()

    # 先同步课程表
    from api.canvas_video import get_user_courses
    from utils.course_data import update_timetable
    courses = await gw.run_sync(get_user_courses, gw.session)
    update_timetable(courses)
    print(f"课程表已同步: {len(courses)} 门课")
    for c in courses:
        print(f"  - {c['id']}: {c['name']}")

    # 扫描所有回放
    print("\n开始扫描回放...")
    course_ids = get_course_ids()
    total = 0
    for cid in course_ids:
        count = await _scan_course_vods(gw, cid)
        total += count

    print(f"\n总计: {total} 条回放")

if __name__ == "__main__":
    asyncio.run(main())
