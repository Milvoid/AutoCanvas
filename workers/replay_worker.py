#!/usr/bin/env python3
"""
Replay Worker
-------------
负责离线处理 VOD 回放：
- ffmpeg 拉取音频流（不下载完整视频）
- 3 秒窗口切片 → 静音跳过 → Qwen3-ASR 0.6B 识别
- 输出到 transcripts/{yymmdd-HHMM}-{课程名}-replay.txt
"""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

# 抑制 transformers 的 pad_token_id 警告（必须在导入 qwen_asr 之前）
try:
    import transformers
    transformers.logging.set_verbosity_error()
except ImportError:
    pass

from gateway import AutoCanvasGateway
from workers.video_manager import (
    get_course_video_state,
    get_transcript_filename,
    mark_vod_finished,
    mark_vod_processing,
    queue_pending_vods,
)

BASE_DIR = Path(__file__).resolve().parent
TRANSCRIPT_DIR = BASE_DIR.parent / "transcripts"

# ASR 模型单例缓存
_asr_model: Optional[Any] = None
_asr_lock = asyncio.Lock()


def _load_asr_model() -> Any:
    """懒加载 Qwen3-ASR 0.6B 模型（线程安全由调用方保证）。"""
    global _asr_model
    if _asr_model is None:
        import os
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

        try:
            import torch
            from qwen_asr import Qwen3ASRModel
        except ImportError as exc:
            raise RuntimeError(
                "无法导入 qwen_asr 或 torch，请确认已安装依赖: "
                "pip install qwen-asr torch"
            ) from exc

        # 加载模型 - Qwen3ASRModel 使用 device_map 参数指定设备
        _asr_model = Qwen3ASRModel.from_pretrained(
            "Qwen/Qwen3-ASR-0.6B",
            device_map="mps",
            local_files_only=True,
        )
        # 记录模型设备信息
        device = getattr(_asr_model, 'device', 'unknown')
        print(f"[ASR] Model loaded on device: {device}", flush=True)
    return _asr_model


def _ffmpeg_audio_pipe(url: str, gateway: AutoCanvasGateway) -> subprocess.Popen:
    """启动 ffmpeg，将音频以 16kHz 单声道 s16le PCM 输出到 stdout。"""
    headers = "Referer: https://courses.sjtu.edu.cn/\r\n"
    if gateway._session is not None:
        cookies = gateway._session.cookies.get_dict()
        if cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            headers += f"Cookie: {cookie_str}\r\n"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-headers", headers,
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-i", url,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-f", "s16le", "pipe:1",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _sec_to_hhmmss(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _sample_audio_level(url: str, gateway: AutoCanvasGateway, seconds: int = 3) -> float:
    """采样指定 URL 的前几秒音频，返回最大振幅（0.0~1.0）。"""
    proc = _ffmpeg_audio_pipe(url, gateway)
    try:
        chunk_bytes = 16000 * 2 * seconds
        raw = proc.stdout.read(chunk_bytes)  # type: ignore[union-attr]
        if not raw:
            return 0.0
        if len(raw) < chunk_bytes:
            raw = raw + b"\x00" * (chunk_bytes - len(raw))
        audio_np = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.max(np.abs(audio_np)))
    except Exception:
        return 0.0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass


async def _select_asr_stream(
    gateway: AutoCanvasGateway,
    streams: List[Dict[str, Any]],
) -> Optional[str]:
    """
    为 ASR 选择真正有音频的流。
    1. 若只有 1 个流，直接返回。
    2. 对每个流采样前 3 秒音频，过滤掉几乎无声的。
    3. 有声流中优先 camera，其次 screen，其次任意；同类型优先 hdv。
    """
    if not streams:
        return None
    if len(streams) == 1:
        return streams[0]["url"]

    # 并发采样各流音频电平（丢进线程池）
    levels: List[tuple] = []
    for s in streams:
        url = s["url"]
        lvl = await gateway.run_sync(_sample_audio_level, url, gateway, 3)
        levels.append((s, lvl))
        gateway.logger.debug("[stream_select] %s max_abs=%.6f", url.split("/")[-2], lvl)

    # 过滤静音（阈值比正常静音判定稍低，避免边缘情况）
    audible = [(s, lvl) for s, lvl in levels if lvl >= 0.001]
    if not audible:
        gateway.logger.warning("[stream_select] 所有流均无声， fallback 到 hdv / 首条流")
        for s, lvl in levels:
            if s.get("quality") == "hdv":
                return s["url"]
        return streams[0]["url"]

    # 有声流中：优先 camera，其次 screen，其次 unknown
    def _sort_key(item: tuple) -> tuple:
        s, lvl = item
        feed_rank = {"camera": 0, "screen": 1, "unknown": 2}.get(s.get("feed_type", "unknown"), 2)
        qual_rank = 0 if s.get("quality") == "hdv" else 1
        return (feed_rank, qual_rank, -lvl)

    audible.sort(key=_sort_key)
    chosen, chosen_lvl = audible[0]
    gateway.logger.info(
        "[stream_select] 选中 %s (feed_type=%s, quality=%s, max_abs=%.6f)",
        chosen["url"].split("/")[-2],
        chosen.get("feed_type", "unknown"),
        chosen.get("quality", ""),
        chosen_lvl,
    )
    return chosen["url"]


async def process_vod(
    gateway: AutoCanvasGateway,
    course_id: int | str,
    video_id: str,
) -> None:
    """处理单个 VOD：ffmpeg -> ASR -> transcript。"""
    course_state = get_course_video_state(course_id)
    entry: Optional[Dict[str, Any]] = None
    for vod in course_state.get("vod", []):
        if vod.get("id") == video_id:
            entry = vod
            break

    if not entry:
        gateway.logger.warning("[replay] 未找到 VOD %s", video_id)
        return

    if entry.get("transcribed") or entry.get("processing") or entry.get("skipped"):
        gateway.logger.info("[replay] VOD %s 已处理/正在处理/已跳过，跳过", video_id)
        return

    mark_vod_processing(gateway, course_id, video_id, True)

    # 选择真正有音频的流用于 ASR
    streams = entry.get("streams", [])
    target_url = await _select_asr_stream(gateway, streams)

    if not target_url:
        gateway.logger.error("[replay] VOD %s 无可用流地址", video_id)
        mark_vod_processing(gateway, course_id, video_id, False)
        return

    gateway.logger.info("[replay] 开始处理 VOD %s, URL=%s", video_id, target_url)

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / get_transcript_filename(entry, suffix="replay")

    header = (
        f"# 课程: {entry.get('name', '')}\n"
        f"# 教师: {entry.get('teacher', '')}\n"
        f"# 时间: {entry.get('begin_time', '')}\n"
        f"# 流地址: {target_url}\n"
        f"# 开始转写: {datetime.now().isoformat()}\n"
        f"{'-'*40}\n"
    )
    transcript_path.write_text(header, encoding="utf-8")

    proc: Optional[subprocess.Popen] = None
    try:
        # 在线程池启动 ffmpeg（避免阻塞事件循环）
        proc = await gateway.run_sync(_ffmpeg_audio_pipe, target_url, gateway)

        chunk_bytes = 16000 * 2 * 3   # 3 秒，16kHz，16bit，单声道
        current_sec = 0.0
        chunk_count = 0
        text_count = 0

        def _read_chunk() -> bytes:
            return proc.stdout.read(chunk_bytes)  # type: ignore[union-attr]

        while True:
            raw = await gateway.run_sync(_read_chunk)
            if not raw:
                break
            if len(raw) < chunk_bytes:
                raw = raw + b"\x00" * (chunk_bytes - len(raw))

            audio_np = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            chunk_count += 1

            # 静音跳过
            if np.max(np.abs(audio_np)) < 0.005:
                current_sec += 3.0
                continue

            # ASR 识别：先加载模型，再用 asyncio.Lock 串行调用 transcribe
            model = await gateway.run_sync(_load_asr_model)
            async with _asr_lock:
                results = await gateway.run_sync(
                    model.transcribe,
                    audio=(audio_np, 16000),
                    language="Chinese",
                )

            text = results[0].text.strip() if results and results[0].text else ""
            if text:
                text_count += 1
                ts = _sec_to_hhmmss(current_sec)
                with open(transcript_path, "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] {text}\n")
                # 每处理10个有效文本段打印进度
                if text_count % 10 == 0:
                    gateway.logger.info("[replay] VOD %s 进度: %s, 已处理 %d 段", video_id, ts, text_count)

            current_sec += 3.0

    except Exception as exc:
        gateway.logger.exception("[replay] VOD %s 处理异常: %s", video_id, exc)
        mark_vod_processing(gateway, course_id, video_id, False)
        return
    finally:
        if proc is not None:
            try:
                proc.terminate()
                await gateway.run_sync(proc.wait)
            except Exception:
                pass

    mark_vod_finished(gateway, course_id, video_id, str(transcript_path))
    gateway.logger.info("[replay] VOD %s 处理完成，输出: %s", video_id, transcript_path)


async def process_all_pending_vods(
    gateway: AutoCanvasGateway,
    course_id: int | str,
) -> None:
    """处理某课程下全部待转写 VOD（顺序执行，避免 ASR 资源争抢）。"""
    pending = queue_pending_vods(gateway, course_id)
    if not pending:
        gateway.logger.info("[replay] 课程 %s 没有待转写的 VOD", course_id)
        return
    for entry in pending:
        await process_vod(gateway, course_id, entry["id"])
