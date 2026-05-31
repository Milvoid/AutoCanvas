#!/usr/bin/env python3
"""
Video Manager
-------------
负责：
- 维护 data/video_state.json（课程直播/点播元数据与转写状态）
- 维护 data/stream_labels.json（摄像头 vs 屏幕的 URL 特征映射）
- 定期与 Canvas LTI 同步视频列表
- 提供 VOD 队列管理、直播->点播迁移逻辑
- 使用 ffprobe 自动区分多路视频流（camera / screen）
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from api.canvas_video import get_course_videos, VideoStream
from gateway import AutoCanvasGateway

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
VIDEO_STATE_FILE = DATA_DIR / "video_state.json"
STREAM_LABELS_FILE = DATA_DIR / "stream_labels.json"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: Any) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save_json(path: Path, data: Any) -> None:
    _ensure_data_dir()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Stream labeling (camera vs screen)
# ---------------------------------------------------------------------------
def _extract_device_id(url: str) -> Optional[str]:
    """从 URL 路径中提取设备/房间 ID，用于缓存标签。"""
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2:
        return parts[-2]
    return None


def _run_ffprobe(url: str) -> Dict[str, Any]:
    """同步执行 ffprobe，返回视频流基本信息。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=bit_rate,width,height",
        "-of", "json",
        url,
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout.decode("utf-8"))
        return data.get("streams", [{}])[0]
    except Exception:
        return {}


async def classify_streams(
    gateway: AutoCanvasGateway,
    streams: List[VideoStream],
) -> Dict[str, str]:
    """
    为每个 stream URL 返回 'camera' | 'screen' | 'unknown'。
    优先读取缓存的 stream_labels.json；未命中则调用 ffprobe 进行启发式判断。
    """
    labels = _load_json(STREAM_LABELS_FILE, {})
    result: Dict[str, str] = {}
    uncached: List[tuple] = []

    for s in streams:
        device_id = _extract_device_id(s.url)
        if device_id and device_id in labels:
            result[s.url] = labels[device_id].get("type", "unknown")
        else:
            result[s.url] = "unknown"
            uncached.append((s, device_id))

    if not uncached:
        return result

    # 对未缓存的 URL 并发执行 ffprobe（通过线程池）
    probed: List[Dict[str, Any]] = []
    for s, device_id in uncached:
        info = await gateway.run_sync(_run_ffprobe, s.url)
        bitrate = int(info.get("bit_rate") or 0)
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        probed.append({
            "stream": s,
            "device_id": device_id,
            "bitrate": bitrate,
            "width": width,
            "height": height,
            "score": bitrate + width * height,
        })

    if len(probed) == 1:
        # 单流时默认视为 screen（教学内容）
        probed[0]["type"] = "screen"
    elif len(probed) >= 2:
        probed.sort(key=lambda x: x["score"], reverse=True)
        probed[0]["type"] = "screen"
        probed[1]["type"] = "camera"
        for p in probed[2:]:
            p["type"] = "unknown"

    for p in probed:
        url = p["stream"].url
        device_id = p["device_id"]
        label_type = p["type"]
        result[url] = label_type
        if device_id:
            labels[device_id] = {
                "type": label_type,
                "confidence": "heuristic",
                "probed_at": datetime.now().isoformat(),
            }

    _save_json(STREAM_LABELS_FILE, labels)
    return result


# ---------------------------------------------------------------------------
# Video state persistence
# ---------------------------------------------------------------------------
def _sanitize_name(name: str) -> str:
    """生成文件名安全的课程名称。"""
    # 去掉 "(第X讲)"
    name = re.sub(r"\s*\(第\d+讲\)", "", name)
    name = name.strip().replace(" ", "-")
    name = re.sub(r'[\\/:*?"\u003c\u003e|]', "", name)
    return name


def _lecture_to_dict(lec: Any, labels: Dict[str, str]) -> Dict[str, Any]:
    """将 canvas_video.Lecture 转换为可持久化的 dict。"""
    return {
        "id": lec.id,
        "name": lec.name,
        "teacher": lec.teacher,
        "classroom": lec.classroom,
        "begin_time": lec.begin_time,
        "end_time": lec.end_time,
        "status": lec.status,
        "streams": [
            {"quality": s.quality, "url": s.url, "feed_type": labels.get(s.url, "unknown")}
            for s in lec.streams
        ],
    }


async def sync_course_videos(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    *,
    include_live: bool = True,
    include_vod: bool = True,
) -> None:
    """
    拉取 Canvas 课程的最新视频元数据，并与本地 video_state.json 合并。
    保留已有的 transcribed / processing / monitoring 等状态字段。
    """
    state = _load_json(VIDEO_STATE_FILE, {})
    gateway.logger.info("[video_manager] 开始同步课程 %s 的视频信息", course_id)

    lectures = await gateway.run_sync(
        get_course_videos,
        gateway.session,
        course_id,
        include_live=include_live,
        include_vod=include_vod,
        fetch_stream_info=True,
        live_days=365,
    )

    course_state = state.setdefault("courses", {}).setdefault(
        str(course_id), {"live": [], "vod": []}
    )

    old_live = {item.get("id"): item for item in course_state.get("live", [])}
    old_vod = {item.get("id"): item for item in course_state.get("vod", [])}

    new_live: List[Dict[str, Any]] = []
    new_vod: List[Dict[str, Any]] = []

    for lec in lectures:
        labels = await classify_streams(gateway, lec.streams)
        entry = _lecture_to_dict(lec, labels)

        if lec.category == "live":
            old = old_live.get(lec.id, {})
            entry["monitoring"] = old.get("monitoring", False)
            entry["monitored_at"] = old.get("monitored_at")
            entry["vod_migrated"] = old.get("vod_migrated", False)
            entry["transcript_path"] = old.get("transcript_path")
            new_live.append(entry)
        else:
            old = old_vod.get(lec.id, {})
            entry["transcribed"] = old.get("transcribed", False)
            entry["transcript_path"] = old.get("transcript_path")
            entry["processing"] = old.get("processing", False)
            entry["queued_at"] = old.get("queued_at")
            entry["finished_at"] = old.get("finished_at")
            new_vod.append(entry)

    course_state["live"] = new_live
    course_state["vod"] = new_vod
    state["last_sync"] = datetime.now().isoformat()
    _save_json(VIDEO_STATE_FILE, state)
    gateway.logger.info(
        "[video_manager] 课程 %s 同步完成: live=%d, vod=%d",
        course_id, len(new_live), len(new_vod)
    )


def get_course_video_state(course_id: int | str) -> Dict[str, Any]:
    """读取本地 video_state.json 中指定课程的状态。"""
    state = _load_json(VIDEO_STATE_FILE, {})
    return state.get("courses", {}).get(str(course_id), {"live": [], "vod": []})


def queue_pending_vods(
    gateway: AutoCanvasGateway,
    course_id: int | str,
) -> List[Dict[str, Any]]:
    """
    将指定课程下所有未被转写且未在处理的 VOD 标记为待处理。
    返回本次被加入队列的条目列表。
    """
    state = _load_json(VIDEO_STATE_FILE, {})
    course_state = state.setdefault("courses", {}).setdefault(
        str(course_id), {"live": [], "vod": []}
    )
    queued: List[Dict[str, Any]] = []
    for entry in course_state.get("vod", []):
        if (
            not entry.get("transcribed")
            and not entry.get("processing")
            and not entry.get("skipped")
        ):
            entry["queued_at"] = datetime.now().isoformat()
            queued.append(entry)
    _save_json(VIDEO_STATE_FILE, state)
    gateway.logger.info(
        "[video_manager] 课程 %s 新增 %d 条待转写 VOD 到队列",
        course_id, len(queued)
    )
    return queued


def mark_vod_processing(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    video_id: str,
    processing: bool,
) -> None:
    """标记 VOD 是否处于处理中。"""
    state = _load_json(VIDEO_STATE_FILE, {})
    course_state = state.get("courses", {}).get(str(course_id), {"vod": []})
    for entry in course_state.get("vod", []):
        if entry.get("id") == video_id:
            entry["processing"] = processing
            break
    _save_json(VIDEO_STATE_FILE, state)
    if processing:
        gateway.logger.info("[video_manager] VOD %s 标记为处理中", video_id)


def mark_vod_finished(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    video_id: str,
    transcript_path: str,
) -> None:
    """标记 VOD 转写完成。"""
    state = _load_json(VIDEO_STATE_FILE, {})
    course_state = state.get("courses", {}).get(str(course_id), {"vod": []})
    for entry in course_state.get("vod", []):
        if entry.get("id") == video_id:
            entry["transcribed"] = True
            entry["processing"] = False
            entry["transcript_path"] = transcript_path
            entry["finished_at"] = datetime.now().isoformat()
            break
    _save_json(VIDEO_STATE_FILE, state)
    gateway.logger.info(
        "[video_manager] VOD %s 转写完成，输出: %s",
        video_id, transcript_path
    )


def resolve_live_to_vod(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    live_id: str,
) -> Optional[Dict[str, Any]]:
    """
    当一次直播监视结束后调用：
    - 将对应直播条目标记为 vod_migrated
    - 尝试在 VOD 列表中找到同名+同时间的课程
    - 若直播已产生 transcript，可将其迁移到匹配到的 VOD 条目
    返回匹配到的 VOD dict（或无匹配返回 None）。
    """
    state = _load_json(VIDEO_STATE_FILE, {})
    course_state = state.get("courses", {}).get(str(course_id), {"live": [], "vod": []})

    live_entry: Optional[Dict[str, Any]] = None
    for item in course_state.get("live", []):
        if item.get("id") == live_id:
            live_entry = item
            break

    if not live_entry:
        return None

    live_entry["monitoring"] = False
    live_entry["vod_migrated"] = True

    matched_vod: Optional[Dict[str, Any]] = None
    for vod in course_state.get("vod", []):
        if vod.get("name") == live_entry.get("name") and vod.get("begin_time") == live_entry.get("begin_time"):
            matched_vod = vod
            break

    if matched_vod and live_entry.get("transcript_path"):
        matched_vod["transcribed"] = True
        matched_vod["transcript_path"] = live_entry["transcript_path"]
        matched_vod["finished_at"] = datetime.now().isoformat()
        gateway.logger.info(
            "[video_manager] 直播 %s 的转写结果已迁移到点播 %s",
            live_id, matched_vod.get("id")
        )

    _save_json(VIDEO_STATE_FILE, state)
    return matched_vod


def mark_live_monitoring(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    lecture_id: str,
    monitoring: bool,
) -> None:
    """标记直播是否处于监听中。"""
    state = _load_json(VIDEO_STATE_FILE, {})
    course_state = state.get("courses", {}).get(str(course_id), {"live": [], "vod": []})
    for entry in course_state.get("live", []):
        if entry.get("id") == lecture_id:
            entry["monitoring"] = monitoring
            if monitoring:
                entry["monitored_at"] = datetime.now().isoformat()
            break
    _save_json(VIDEO_STATE_FILE, state)
    if monitoring:
        gateway.logger.info("[video_manager] 直播 %s 标记为监听中", lecture_id)


def mark_live_transcript(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    lecture_id: str,
    transcript_path: str,
) -> None:
    """为直播条目写入 transcript 路径。"""
    state = _load_json(VIDEO_STATE_FILE, {})
    course_state = state.get("courses", {}).get(str(course_id), {"live": [], "vod": []})
    for entry in course_state.get("live", []):
        if entry.get("id") == lecture_id:
            entry["transcript_path"] = transcript_path
            break
    _save_json(VIDEO_STATE_FILE, state)
    gateway.logger.info(
        "[video_manager] 直播 %s transcript 已记录: %s",
        lecture_id, transcript_path,
    )


def get_live_queue(course_id: int | str) -> List[Dict[str, Any]]:
    """返回指定课程的所有直播条目（含 monitoring 状态）。"""
    course_state = get_course_video_state(course_id)
    return course_state.get("live", [])


def reset_vod(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    video_id: str,
) -> bool:
    """重置 VOD 转写状态，允许重新处理。"""
    state = _load_json(VIDEO_STATE_FILE, {})
    course_state = state.get("courses", {}).get(str(course_id), {"vod": []})
    for entry in course_state.get("vod", []):
        if entry.get("id") == video_id:
            entry["transcribed"] = False
            entry["processing"] = False
            entry["transcript_path"] = None
            entry["finished_at"] = None
            entry.pop("skipped", None)
            _save_json(VIDEO_STATE_FILE, state)
            gateway.logger.info("[video_manager] VOD %s 已重置", video_id)
            return True
    return False


def skip_vod(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    video_id: str,
) -> bool:
    """标记 VOD 为跳过，后续不再处理。"""
    state = _load_json(VIDEO_STATE_FILE, {})
    course_state = state.get("courses", {}).get(str(course_id), {"vod": []})
    for entry in course_state.get("vod", []):
        if entry.get("id") == video_id:
            entry["skipped"] = True
            entry["processing"] = False
            _save_json(VIDEO_STATE_FILE, state)
            gateway.logger.info("[video_manager] VOD %s 已标记为跳过", video_id)
            return True
    return False


def get_vod_queue(course_id: int | str) -> List[Dict[str, Any]]:
    """返回指定课程的所有 VOD 条目。"""
    course_state = get_course_video_state(course_id)
    return course_state.get("vod", [])


def get_transcript_filename(entry: Dict[str, Any], suffix: str = "replay") -> str:
    """根据课程条目生成 transcript 文件名。"""
    name = _sanitize_name(entry.get("name", "unknown"))
    begin = entry.get("begin_time", "")
    # begin_time format: "2026-03-26 16:55:00"
    if begin:
        try:
            dt = datetime.strptime(begin, "%Y-%m-%d %H:%M:%S")
            ts = dt.strftime("%y%m%d-%H%M")
        except ValueError:
            ts = datetime.now().strftime("%y%m%d-%H%M")
    else:
        ts = datetime.now().strftime("%y%m%d-%H%M")
    return f"{ts}-{name}-{suffix}.txt"
