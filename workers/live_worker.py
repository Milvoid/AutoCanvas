#!/usr/bin/env python3
"""
Live Worker - 直播监听
----------------------
Schedule 驱动：由 monitor_start Schedule 启动，monitor_end Schedule 停止。
"""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from gateway import AutoCanvasGateway
from utils.course_data import (
    get_schedules,
    mark_schedule_executed,
)
from workers.video_manager import (
    get_course_video_state,
    get_transcript_filename,
)
from workers.replay_worker import _ffmpeg_audio_pipe, _load_asr_model, _asr_lock, _sec_to_hhmmss

BASE_DIR = Path(__file__).resolve().parent
TRANSCRIPT_DIR = BASE_DIR.parent / "transcripts"

LIVE_CONFIG = {
    "keywords": ["签到", "点名", "名字"],
    "silence_threshold": 0.005,
    "chunk_seconds": 3,
    "sample_rate": 16000,
    "keyword_debounce_seconds": 30,
}


async def process_live(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    lecture_id: str,
    *,
    scheduled_end: Optional[str] = None,
) -> None:
    """
    直播监听任务。
    由 Schedule 触发启动，直到 scheduled_end 时间或流结束。
    """
    # 查找直播信息
    course_state = get_course_video_state(course_id)
    entry = None
    for live in course_state.get("live", []):
        if live.get("id") == lecture_id:
            entry = live
            break

    if not entry:
        gateway.logger.error("[live] 未找到直播 %s", lecture_id)
        return

    # 选择最佳流
    streams = entry.get("streams", [])
    target_url = _select_stream(streams)

    if not target_url:
        # 本地没有流地址，尝试现场从 Canvas API 拉取
        try:
            gateway.logger.warning("[live] 直播 %s 本地无流地址，尝试从 API 获取...", lecture_id)
            from api.canvas_video import get_live_video_info, _extract_streams
            info = await gateway.run_sync(get_live_video_info, gateway.session, lecture_id)
            fresh_streams = _extract_streams(info)
            if fresh_streams:
                streams = [
                    {"quality": s.quality, "url": s.url, "feed_type": "unknown"}
                    for s in fresh_streams
                ]
                target_url = _select_stream(streams)
                gateway.logger.info("[live] 成功从 API 获取流地址: %s", lecture_id)
        except Exception as e:
            gateway.logger.error("[live] 从 API 获取流地址失败 %s: %s", lecture_id, e)

    if not target_url:
        gateway.logger.error("[live] 直播 %s 无可用流地址", lecture_id)
        return

    # 准备转写文件
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / get_transcript_filename(entry, suffix="stream")

    actual_start = datetime.now()
    _write_header(transcript_path, entry, target_url, actual_start)

    gateway.logger.info(
        "[live] 开始监听 %s, 预定结束=%s",
        lecture_id,
        scheduled_end or "未指定"
    )

    # 配置
    sample_rate = int(LIVE_CONFIG["sample_rate"])
    chunk_seconds = int(LIVE_CONFIG["chunk_seconds"])
    chunk_bytes = sample_rate * 2 * chunk_seconds
    silence_threshold = float(LIVE_CONFIG["silence_threshold"])

    proc: Optional[subprocess.Popen] = None
    current_sec = 0.0
    last_alert = 0.0

    # 计算结束时间
    end_time = None
    if scheduled_end:
        try:
            end_time = datetime.fromisoformat(scheduled_end)
        except:
            pass

    try:
        while not gateway._shutting_down:
            # 检查是否到达预定结束时间
            if end_time and datetime.now() >= end_time:
                gateway.logger.info("[live] 到达预定结束时间，停止监听")
                break

            # 需要连接或重连
            if proc is None:
                proc = await _connect(gateway, target_url)
                if not proc:
                    gateway.logger.warning("[live] 连接失败，30秒后重试...")
                    await asyncio.sleep(30)
                    continue

            # 读取音频块
            raw = await gateway.run_sync(lambda: proc.stdout.read(chunk_bytes))

            if not raw:
                # 流结束或断线，关闭并尝试重连
                gateway.logger.warning("[live] 流数据中断，5秒后重连...")
                try:
                    proc.terminate()
                    await gateway.run_sync(proc.wait)
                except:
                    pass
                proc = None
                await asyncio.sleep(5)
                continue

            if len(raw) < chunk_bytes:
                raw = raw + b"\x00" * (chunk_bytes - len(raw))

            # ASR 识别
            audio_np = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            if np.max(np.abs(audio_np)) >= silence_threshold:
                text = await _transcribe(gateway, audio_np, sample_rate)
                if text:
                    ts = _sec_to_hhmmss(current_sec)
                    _write_transcript(transcript_path, ts, text)
                    await _check_keywords(gateway, text, course_id, lecture_id, current_sec, last_alert)

            current_sec += chunk_seconds

    except asyncio.CancelledError:
        gateway.logger.info("[live] 任务被取消")
        raise
    except Exception as e:
        gateway.logger.exception("[live] 异常: %s", e)
    finally:
        if proc:
            try:
                proc.terminate()
                await gateway.run_sync(proc.wait)
            except:
                pass

        # 记录结果到 Schedule 的 metadata
        _record_result(gateway, lecture_id, actual_start, transcript_path)
        gateway.logger.info("[live] 监听结束: %s", lecture_id)

def _select_stream(streams: list) -> Optional[str]:
    """选择最佳流（hdv + screen 优先）"""
    for s in streams:
        if s.get("quality") == "hdv" and s.get("feed_type") == "screen":
            return s["url"]
    for s in streams:
        if s.get("quality") == "hdv":
            return s["url"]
    if streams:
        return streams[0]["url"]
    return None


async def _connect(gateway: AutoCanvasGateway, url: str) -> Optional[subprocess.Popen]:
    """连接直播流"""
    try:
        proc = await gateway.run_sync(_ffmpeg_audio_pipe, url, gateway)
        # 测试读取
        test = await gateway.run_sync(lambda: proc.stdout.read(1024))
        if test:
            return proc
        proc.terminate()
    except Exception as e:
        gateway.logger.error("[live] 连接失败: %s", e)
    return None


async def _transcribe(gateway, audio_np, sample_rate) -> str:
    """ASR 转写"""
    model = await gateway.run_sync(_load_asr_model)
    async with _asr_lock:
        results = await gateway.run_sync(
            model.transcribe,
            audio=(audio_np, sample_rate),
            language="Chinese",
        )
    return results[0].text.strip() if results and results[0].text else ""


def _write_header(path: Path, entry: dict, url: str, start_time: datetime) -> None:
    """写入文件头（已存在则追加恢复标记，不覆盖已有内容）"""
    if path.exists() and path.stat().st_size > 0:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n# 恢复监听: {start_time.isoformat()}\n")
            f.write(f"# 流地址: {url}\n")
            f.write("-" * 40 + "\n")
        return

    header = (
        f"# 课程: {entry.get('name', '')}\n"
        f"# 教师: {entry.get('teacher', '')}\n"
        f"# 开始时间: {start_time.isoformat()}\n"
        f"# 流地址: {url}\n"
        f"{'-'*40}\n"
    )
    path.write_text(header, encoding="utf-8")


def _write_transcript(path: Path, ts: str, text: str) -> None:
    """追加转写内容"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")


async def _check_keywords(gateway, text, course_id, lecture_id, current_sec, last_alert) -> None:
    """检查关键词"""
    keywords = LIVE_CONFIG["keywords"]
    debounce = LIVE_CONFIG["keyword_debounce_seconds"]
    now = asyncio.get_running_loop().time()

    if now - last_alert < debounce:
        return

    for kw in keywords:
        if kw in text:
            await gateway.post_event(
                "keyword_alert",
                course_id=str(course_id),
                lecture_id=lecture_id,
                keyword=kw,
                text=text,
                ts=datetime.now().isoformat(),
            )
            gateway.logger.info("[live] 关键词命中: %s", kw)
            return


def _record_result(gateway, lecture_id: str, actual_start: datetime, transcript_path: Path) -> None:
    """记录结果到 Schedule metadata"""
    try:
        # 查找对应的 monitor_start schedule 并更新 metadata
        schedules = get_schedules()
        for s in schedules:
            if s.get("lecture_id") == lecture_id and s.get("type") == "monitor_start":
                # 更新 metadata
                from utils.course_data import SCHEDULE_FILE
                import json
                data = json.loads(SCHEDULE_FILE.read_text())
                for sch in data["schedules"]:
                    if sch["id"] == s["id"]:
                        sch.setdefault("result", {})
                        sch["result"]["actual_start"] = actual_start.isoformat()
                        sch["result"]["transcript_path"] = str(transcript_path) if transcript_path.exists() else None
                        SCHEDULE_FILE.write_text(json.dumps(data, indent=2))
                        break
                break
    except Exception as e:
        gateway.logger.error("[live] 记录结果失败: %s", e)
