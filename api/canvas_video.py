#!/usr/bin/env python3
"""
SJTU Canvas 课程直播 / 点播视频信息获取模块。

功能：
- 对指定 Canvas course_id 完成 LTI 1.0 启动（external_tools/9487）
- 提取加密的 canvasCourseId
- 获取未来直播列表（findLiveList）
- 获取历史点播列表（findVodVideoList）
- 对每个视频条目拉取流地址与元数据（getLiveVideoInfos / getVodVideoInfos）

依赖：
    pip install requests
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

import requests


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class VideoStream:
    """单个播放流地址。"""
    quality: str  # hdv / fluency / distinct / default
    url: str


@dataclass
class Lecture:
    """单个课程（直播或点播）的元数据。"""
    id: str
    name: str
    category: str  # "live" | "vod"
    teacher: str = ""
    classroom: str = ""
    begin_time: str = ""
    end_time: str = ""
    status: str = ""  # liveStatus / vod audit status etc.
    streams: List[VideoStream] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _lti_launch(session: requests.Session, course_id: int | str) -> str:
    """
    1. 访问 Canvas external_tools/9487，拿到自动提交的 LTI launch form。
    2. 解析并 POST 到 https://courses.sjtu.edu.cn/lti/launch。
    3. 从最终 landing page 中提取 canvasCourseId（加密串）。
    """
    step1_url = f"https://oc.sjtu.edu.cn/courses/{course_id}/external_tools/9487"
    resp1 = session.get(step1_url, timeout=30)
    resp1.raise_for_status()

    action_match = re.search(
        r'<form[^>]*action=["\']([^"\']+)["\']',
        resp1.text,
        re.IGNORECASE | re.DOTALL,
    )
    if not action_match:
        raise RuntimeError("无法在 external_tools 页面找到 LTI launch form")
    action = action_match.group(1)

    inputs = re.findall(
        r'<input[^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"]*)["\']',
        resp1.text,
        re.IGNORECASE | re.DOTALL,
    )
    form_data = {name: value for name, value in inputs}

    resp2 = session.post(action, data=form_data, timeout=30)
    resp2.raise_for_status()

    cid_match = re.search(r'var\s+canvasCourseId\s*=\s*"([^"]+)"', resp2.text)
    if not cid_match:
        raise RuntimeError("LTI launch 后页面中未找到 canvasCourseId")
    return cid_match.group(1)


def get_user_courses(session: requests.Session) -> List[dict]:
    """
    获取当前用户的所有课程列表。
    返回课程列表，每个课程包含 id 和 name。
    """
    url = "https://oc.sjtu.edu.cn/api/v1/courses"
    params = {
        "enrollment_state": "active",
        "per_page": 100,
    }
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    courses = resp.json()

    result = []
    for course in courses:
        course_id = course.get("id")
        course_name = course.get("name", "")
        if course_id:
            result.append({
                "id": str(course_id),
                "name": course_name,
            })
    return result


def _extract_streams(data_object: dict) -> List[VideoStream]:
    """从 getLiveVideoInfos / getVodVideoInfos 返回的 object/body 中提取流地址。"""
    streams: List[VideoStream] = []
    for item in data_object.get("videoPlayResponseVoList") or []:
        for key, qual in (
            ("rtmpUrlHdv", "hdv"),
            ("rtmpUrlFluency", "fluency"),
            ("rtmpUrlDistinct", "distinct"),
            ("rtmpUrlDefault", "default"),
        ):
            url = item.get(key)
            if url:
                streams.append(VideoStream(quality=qual, url=url))
    return streams


# ---------------------------------------------------------------------------
# Public APIs
# ---------------------------------------------------------------------------
def get_live_list(
    session: requests.Session,
    course_id: int | str,
    live_days: int = 365,
    page_size: int = 100,
) -> List[dict]:
    """
    获取指定课程的**直播/未来课程**列表。
    返回原始 JSON list（每个元素为一个讲座 dict）。
    """
    canvas_course_id = _lti_launch(session, course_id)
    url = "https://courses.sjtu.edu.cn/lti/liveVideo/findLiveList"
    payload = {
        "liveDays": live_days,
        "pageIndex": 1,
        "pageSize": page_size,
        "canvasCourseId": canvas_course_id,
    }
    resp = session.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(f"findLiveList 返回错误: {data.get('desc')} (code={data.get('code')})")
    return data.get("body", {}).get("list", [])


def get_live_video_info(session: requests.Session, lecture_id: str) -> dict:
    """
    获取单个直播讲座的详细信息（含流地址、起止时间、教师等）。
    返回 API 原始 ``object`` 字段内容。
    """
    url = "https://courses.sjtu.edu.cn/lti/liveVideo/getLiveVideoInfos"
    payload = {
        "playMode": "",
        "id": lecture_id,
        "clroLiveVodvideoRight": "liveRight",
    }
    resp = session.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(
            f"getLiveVideoInfos 返回错误: {data.get('desc')} (code={data.get('code')})"
        )
    return data.get("body", {})


def get_vod_list(
    session: requests.Session,
    course_id: int | str,
    page_size: int = 1000,
) -> List[dict]:
    """
    获取指定课程的**历史点播**列表。
    返回原始 JSON list（每个元素为一个视频 dict，主键为 ``videoId``）。
    """
    canvas_course_id = _lti_launch(session, course_id)
    url = "https://courses.sjtu.edu.cn/lti/vodVideo/findVodVideoList"
    payload = {
        "pageIndex": 1,
        "pageSize": page_size,
        "canvasCourseId": canvas_course_id,
    }
    resp = session.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(
            f"findVodVideoList 返回错误: {data.get('desc')} (code={data.get('code')})"
        )
    return data.get("body", {}).get("list", [])


def get_vod_video_info(session: requests.Session, video_id: str) -> dict:
    """
    获取单个点播视频的详细信息（含流地址、起止时间等）。
    返回 API 原始 ``body`` 字段内容。
    """
    url = "https://courses.sjtu.edu.cn/lti/vodVideo/getVodVideoInfos"
    payload = {
        "playTypeHls": "true",
        "id": video_id,
        "isAudit": "true",
    }
    resp = session.post(url, data=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 200:
        raise RuntimeError(
            f"getVodVideoInfos 返回错误: {data.get('desc')} (code={data.get('code')})"
        )
    return data.get("body", {})


def get_course_videos(
    session: requests.Session,
    course_id: int | str,
    *,
    include_live: bool = True,
    include_vod: bool = True,
    fetch_stream_info: bool = True,
    live_days: int = 365,
) -> List[Lecture]:
    """
    对单一课程拉取完整的直播 + 点播视频元数据与流地址列表。

    Parameters
    ----------
    session : requests.Session
        已登录的 Canvas session（可直接复用 canvas_auth.ensure_session() 返回值）。
    course_id : int | str
        Canvas 课程数字 ID。
    include_live / include_vod : bool
        是否分别获取直播/点播列表。
    fetch_stream_info : bool
        是否为每个条目再调用一次 info API 拉取流地址。
    live_days : int
        调用 findLiveList 时的 liveDays 参数（默认 365 天以覆盖整个学期）。

    Returns
    -------
    List[Lecture]
        合并后的讲座/视频列表。
    """
    lectures: List[Lecture] = []

    if include_live:
        try:
            live_items = get_live_list(session, course_id, live_days=live_days)
        except RuntimeError as exc:
            # 部分课程尚未配置直播，findLiveList 会报错，允许空结果继续
            live_items = []

        for item in live_items:
            lec_id = item.get("id")
            name = item.get("courseName") or item.get("courName") or ""
            teacher = item.get("teacherName") or ""
            classroom = item.get("classroomName") or ""
            begin = item.get("startTime") or ""
            end = item.get("endTime") or ""
            status = str(item.get("liveStatus", ""))

            streams: List[VideoStream] = []
            if fetch_stream_info and lec_id:
                try:
                    info = get_live_video_info(session, lec_id)
                    streams = _extract_streams(info)
                    # 若 info 中字段更全，用其覆盖
                    name = info.get("courseName") or name
                    teacher = info.get("teacherName") or teacher
                    begin = info.get("courBeginTime") or begin
                    end = info.get("courEndTime") or end
                except Exception:
                    pass

            lectures.append(
                Lecture(
                    id=lec_id or "",
                    name=name,
                    category="live",
                    teacher=teacher,
                    classroom=classroom,
                    begin_time=begin,
                    end_time=end,
                    status=status,
                    streams=streams,
                    raw=item,
                )
            )

    if include_vod:
        try:
            vod_items = get_vod_list(session, course_id)
        except RuntimeError:
            vod_items = []

        for item in vod_items:
            vid = item.get("videoId")
            name = item.get("videoName") or ""
            teacher = item.get("userName") or ""
            classroom = item.get("classroomName") or ""
            begin = item.get("courseBeginTime") or ""
            end = item.get("courseEndTime") or ""
            status = str(item.get("videAuditStatus", ""))

            streams: List[VideoStream] = []
            if fetch_stream_info and vid:
                try:
                    info = get_vod_video_info(session, vid)
                    streams = _extract_streams(info)
                    name = info.get("courName") or name
                except Exception:
                    pass

            lectures.append(
                Lecture(
                    id=vid or "",
                    name=name,
                    category="vod",
                    teacher=teacher,
                    classroom=classroom,
                    begin_time=begin,
                    end_time=end,
                    status=status,
                    streams=streams,
                    raw=item,
                )
            )

    return lectures


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
def _main():
    import sys
    from utils.canvas_auth import ensure_session

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <course_id>")
        sys.exit(1)

    course_id = sys.argv[1]
    session = ensure_session(auto_prompt=False)
    lectures = get_course_videos(session, course_id)

    print(f"课程 {course_id} 共获取 {len(lectures)} 条视频记录\n")
    for lec in lectures:
        print(f"[{lec.category.upper()}] {lec.name}")
        print(f"  ID:    {lec.id}")
        print(f"  教师:  {lec.teacher or '—'}")
        print(f"  教室:  {lec.classroom or '—'}")
        print(f"  时间:  {lec.begin_time or '—'} ~ {lec.end_time or '—'}")
        print(f"  状态:  {lec.status or '—'}")
        if lec.streams:
            print(f"  流地址:")
            for s in lec.streams:
                print(f"    [{s.quality}] {s.url}")
        else:
            print(f"  流地址: (暂无)")
        print()


if __name__ == "__main__":
    _main()
