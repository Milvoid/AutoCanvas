#!/usr/bin/env python3
"""
Schedule Manager
----------------
管理定时触发的调度任务（Schedule 层）。

核心概念：
- Schedule: 在特定时间点触发一次的任务注册表
- 与 Loop 区别：Loop 是周期性任务（如每30分钟同步）
- 与 Job 区别：Job 是正在执行的工作单元

应用场景：
- 直播开始前10分钟自动启动监听
- 课程结束后自动迁移转写结果
- 每周一凌晨更新课表
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from gateway import AutoCanvasGateway

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SCHEDULE_FILE = DATA_DIR / "schedule.json"


@dataclass
class ScheduleEntry:
    """单个调度条目。"""
    id: str                          # 唯一标识，如 "live_COURSE_ID_abc123"
    type: str                        # "live_monitor", "vod_migrate", "course_sync"
    course_id: str
    target_id: str                   # lecture_id 或 video_id
    trigger_at: str                  # ISO-8601 触发时间
    status: str = "pending"          # pending, scheduled, triggered, cancelled
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleEntry":
        return cls(**data)


class ScheduleManager:
    """
    Schedule 管理器。
    负责在精确时间点触发任务，支持重试策略。
    """

    def __init__(self, gateway: "AutoCanvasGateway"):
        self.gateway = gateway
        self.logger = gateway.logger
        self._schedules: Dict[str, ScheduleEntry] = {}
        self._check_interval: float = 30.0  # 每30秒检查一次
        self._task: Optional[asyncio.Task] = None
        self._load()

    def _load(self) -> None:
        """从磁盘加载 schedule 状态。"""
        if SCHEDULE_FILE.exists():
            try:
                data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
                for entry_data in data.get("schedules", []):
                    entry = ScheduleEntry.from_dict(entry_data)
                    self._schedules[entry.id] = entry
                self.logger.info("[schedule] 已加载 %d 条调度记录", len(self._schedules))
            except Exception as e:
                self.logger.error("[schedule] 加载失败: %s", e)

    def _save(self) -> None:
        """保存 schedule 状态到磁盘。"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "schedules": [e.to_dict() for e in self._schedules.values()],
            "updated_at": datetime.now().isoformat(),
        }
        SCHEDULE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def register(
        self,
        schedule_type: str,
        course_id: str,
        target_id: str,
        trigger_at: datetime,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ScheduleEntry:
        """
        注册一个新的调度条目。

        Args:
            schedule_type: "live_monitor", "vod_migrate", "course_sync", etc.
            course_id: 课程ID
            target_id: 目标ID (lecture_id/video_id)
            trigger_at: 触发时间
            metadata: 额外元数据
        """
        entry_id = f"{schedule_type}_{course_id}_{target_id}"

        # 如果已存在且未触发，更新触发时间
        if entry_id in self._schedules:
            existing = self._schedules[entry_id]
            if existing.status in ("pending", "scheduled"):
                existing.trigger_at = trigger_at.isoformat()
                existing.metadata.update(metadata or {})
                self._save()
                self.logger.info("[schedule] 更新 %s 触发时间为 %s", entry_id, trigger_at)
                return existing

        entry = ScheduleEntry(
            id=entry_id,
            type=schedule_type,
            course_id=course_id,
            target_id=target_id,
            trigger_at=trigger_at.isoformat(),
            status="pending",
            metadata=metadata or {},
        )
        self._schedules[entry_id] = entry
        self._save()
        self.logger.info(
            "[schedule] 注册 %s @ %s (还有 %.1f 分钟)",
            entry_id, trigger_at.isoformat(),
            (trigger_at - datetime.now()).total_seconds() / 60
        )
        return entry

    def cancel(self, entry_id: str) -> bool:
        """取消一个调度条目。"""
        if entry_id in self._schedules:
            self._schedules[entry_id].status = "cancelled"
            self._save()
            self.logger.info("[schedule] 已取消 %s", entry_id)
            return True
        return False

    def get_pending(self, course_id: Optional[str] = None) -> List[ScheduleEntry]:
        """获取待触发的调度条目。"""
        pending = [
            e for e in self._schedules.values()
            if e.status in ("pending", "scheduled")
        ]
        if course_id:
            pending = [e for e in pending if e.course_id == course_id]
        return sorted(pending, key=lambda x: x.trigger_at)

    def get_all(self, course_id: Optional[str] = None) -> List[ScheduleEntry]:
        """获取所有调度条目。"""
        entries = list(self._schedules.values())
        if course_id:
            entries = [e for e in entries if e.course_id == course_id]
        return sorted(entries, key=lambda x: x.trigger_at, reverse=True)

    def _should_trigger(self, entry: ScheduleEntry) -> bool:
        """检查是否应该触发该条目。"""
        if entry.status not in ("pending", "scheduled"):
            return False
        try:
            trigger_time = datetime.fromisoformat(entry.trigger_at)
            return datetime.now() >= trigger_time
        except Exception:
            return False

    async def _check_and_trigger(self) -> None:
        """检查并触发到期的调度条目。"""
        for entry in list(self._schedules.values()):
            if self._should_trigger(entry):
                entry.status = "triggered"
                self._save()
                self.logger.info("[schedule] 触发 %s", entry.id)

                try:
                    await self._execute_entry(entry)
                except Exception as e:
                    self.logger.exception("[schedule] 执行 %s 失败: %s", entry.id, e)
                    # 失败后可以重试或标记为失败
                    retry_count = entry.metadata.get("retry_count", 0)
                    max_retries = entry.metadata.get("max_retries", 3)

                    if retry_count < max_retries:
                        # 延迟30秒后重试
                        retry_at = datetime.now() + timedelta(seconds=30)
                        entry.trigger_at = retry_at.isoformat()
                        entry.status = "pending"
                        entry.metadata["retry_count"] = retry_count + 1
                        self._save()
                        self.logger.info(
                            "[schedule] %s 将在30秒后重试 (第 %d/%d 次)",
                            entry.id, retry_count + 1, max_retries
                        )

    async def _execute_entry(self, entry: ScheduleEntry) -> None:
        """执行调度条目对应的具体操作。"""
        from workers.live_worker import process_live
        from workers.video_manager import resolve_live_to_vod

        if entry.type == "live_monitor":
            # 启动直播监听
            job_name = f"live_monitor_{entry.target_id}"

            # 检查是否已在监听
            if job_name in self.gateway.state.jobs:
                self.logger.info("[schedule] 直播监听 %s 已在运行", entry.target_id)
                return

            async def _live_job(gw: "AutoCanvasGateway") -> None:
                await process_live(gw, entry.course_id, entry.target_id)

            self.gateway.start_job_runtime(
                job_name, _live_job,
                interval=0,
                retries=entry.metadata.get("retries", 5),
                retry_delay=entry.metadata.get("retry_delay", 30),
            )
            self.logger.info("[schedule] 已启动直播监听 Job: %s", job_name)

        elif entry.type == "live_migrate":
            # 直播结束后迁移到 VOD
            result = await self.gateway.run_sync(
                resolve_live_to_vod,
                self.gateway,
                entry.course_id,
                entry.target_id,
            )
            if result:
                self.logger.info(
                    "[schedule] 直播 %s 已迁移到 VOD %s",
                    entry.target_id, result.get("id")
                )
            else:
                self.logger.warning(
                    "[schedule] 直播 %s 未找到匹配的 VOD",
                    entry.target_id
                )

        elif entry.type == "course_sync":
            # 课程同步（课表更新等）
            from workers.video_manager import sync_course_videos
            await sync_course_videos(self.gateway, entry.course_id)

        elif entry.type == "vod_process":
            # 处理 VOD 转写
            from workers.replay_worker import process_vod
            job_name = f"vod_process_{entry.target_id}"

            async def _vod_job(gw: "AutoCanvasGateway") -> None:
                await process_vod(gw, entry.course_id, entry.target_id)

            self.gateway.start_job_runtime(job_name, _vod_job, interval=0)

        else:
            self.logger.warning("[schedule] 未知类型: %s", entry.type)

    async def start(self) -> None:
        """启动 Schedule 管理器循环。"""
        self.logger.info("[schedule] 管理器启动，检查间隔 %.1f 秒", self._check_interval)
        while not self.gateway._shutting_down:
            await self._check_and_trigger()
            await asyncio.sleep(self._check_interval)

    def start_in_background(self) -> None:
        """在后台启动 Schedule 管理器。"""
        self._task = asyncio.create_task(self.start(), name="schedule_manager")

    async def stop(self) -> None:
        """停止 Schedule 管理器。"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("[schedule] 管理器已停止")


# 快捷函数
def schedule_live_monitor(
    gateway: "AutoCanvasGateway",
    course_id: str,
    lecture_id: str,
    begin_time: datetime,
    buffer_minutes: int = 10,
) -> ScheduleEntry:
    """
    调度直播监听任务，在课程开始前 buffer_minutes 分钟启动。
    """
    trigger_at = begin_time - timedelta(minutes=buffer_minutes)
    manager: ScheduleManager = getattr(gateway, "_schedule_manager", None)
    if not manager:
        raise RuntimeError("ScheduleManager 未初始化")

    return manager.register(
        "live_monitor",
        course_id,
        lecture_id,
        trigger_at,
        metadata={
            "buffer_minutes": buffer_minutes,
            "max_retries": 10,  # 直播开始前持续重试
            "retry_delay": 30,  # 30秒重试间隔
        },
    )


def schedule_live_migrate(
    gateway: "AutoCanvasGateway",
    course_id: str,
    lecture_id: str,
    end_time: datetime,
    delay_minutes: int = 5,
) -> ScheduleEntry:
    """
    调度直播迁移任务，在课程结束后 delay_minutes 分钟执行。
    """
    trigger_at = end_time + timedelta(minutes=delay_minutes)
    manager: ScheduleManager = getattr(gateway, "_schedule_manager", None)
    if not manager:
        raise RuntimeError("ScheduleManager 未初始化")

    return manager.register(
        "live_migrate",
        course_id,
        lecture_id,
        trigger_at,
        metadata={"delay_minutes": delay_minutes},
    )


def schedule_course_sync(
    gateway: "AutoCanvasGateway",
    course_id: str,
    trigger_at: Optional[datetime] = None,
) -> ScheduleEntry:
    """
    调度课程同步任务。
    """
    if trigger_at is None:
        trigger_at = datetime.now()
    manager: ScheduleManager = getattr(gateway, "_schedule_manager", None)
    if not manager:
        raise RuntimeError("ScheduleManager 未初始化")

    return manager.register(
        "course_sync",
        course_id,
        f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        trigger_at,
        metadata={"auto": True},
    )
