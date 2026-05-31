#!/usr/bin/env python3
"""
Job Manager - Foundation Layer
------------------------------
所有任务的基础管理层。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from gateway import AutoCanvasGateway

BASE_DIR = Path(__file__).resolve().parent
JOBS_USER_DIR = BASE_DIR / "user"
JOBS_STATE_FILE = BASE_DIR / "data" / "jobs_state.json"


class JobType(str, Enum):
    BASIC = "basic"
    LOOP = "loop"
    QUEUED = "queued"
    PLUGIN = "plugin"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobConfig:
    name: str
    job_type: JobType = JobType.BASIC
    enabled: bool = True
    interval: float = 0.0
    retries: int = 3
    retry_delay: float = 5.0
    max_concurrent: int = 1
    priority: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    plugin_path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JobConfig":
        data.pop('run_at', None)
        if 'job_type' not in data:
            data['job_type'] = JobType.LOOP if data.get('interval', 0) > 0 else JobType.BASIC
        elif isinstance(data['job_type'], str):
            data['job_type'] = JobType(data['job_type'])
        return cls(**data)


@dataclass
class JobInstance:
    config: JobConfig
    coro: Optional[Callable[[AutoCanvasGateway], Coroutine[Any, Any, Any]]] = None
    task: Optional[asyncio.Task] = None
    status: JobStatus = JobStatus.PENDING
    last_run: Optional[str] = None
    last_error: Optional[str] = None
    run_count: int = 0
    plugin_version: int = 0


class JobRegistry:
    def __init__(self, gateway: AutoCanvasGateway):
        self.gateway = gateway
        self.logger = gateway.logger
        self._jobs: Dict[str, JobInstance] = {}
        self._queue_semaphore: Optional[asyncio.Semaphore] = None
        self._max_queued_concurrent: int = 2
        self._load_state()
        JOBS_USER_DIR.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> None:
        if JOBS_STATE_FILE.exists():
            try:
                data = json.loads(JOBS_STATE_FILE.read_text(encoding="utf-8"))
                for name, job_data in data.get("jobs", {}).items():
                    config_data = job_data.get("config", {})
                    if config_data.get("job_type") == "scheduled":
                        self.logger.warning(
                            "[job_registry] 跳过旧 ScheduledJob '%s'；请迁移到业务 Schedule 或 LoopJob",
                            name,
                        )
                        continue
                    config = JobConfig.from_dict(config_data)
                    instance = JobInstance(
                        config=config,
                        status=JobStatus(job_data.get("status", "pending")),
                        last_run=job_data.get("last_run"),
                        last_error=job_data.get("last_error"),
                        run_count=job_data.get("run_count", 0),
                    )
                    self._jobs[name] = instance
            except Exception as e:
                self.logger.error("[job_registry] 加载状态失败: %s", e)

    def _save_state(self) -> None:
        try:
            JOBS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "jobs": {
                    name: {
                        "config": instance.config.to_dict(),
                        "status": instance.status.value,
                        "last_run": instance.last_run,
                        "last_error": instance.last_error,
                        "run_count": instance.run_count,
                    }
                    for name, instance in self._jobs.items()
                },
                "updated_at": datetime.now().isoformat(),
            }
            JOBS_STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self.logger.error("[job_registry] 保存状态失败: %s", e)

    def register_basic(self, name: str, coro, *, enabled: bool = True, retries: int = 3, retry_delay: float = 5.0, **metadata) -> JobConfig:
        if name in self._jobs:
            self.logger.warning("[job_registry] Job '%s' 已存在，覆盖注册", name)
        config = JobConfig(name=name, job_type=JobType.BASIC, enabled=enabled, retries=retries, retry_delay=retry_delay, metadata=metadata)
        instance = JobInstance(config=config, coro=coro)
        self._jobs[name] = instance
        self._save_state()
        self.logger.info("[job_registry] 注册 BasicJob '%s'", name)
        return config

    def register_loop(self, name: str, coro, *, interval: float, enabled: bool = True, retries: int = 3, retry_delay: float = 5.0, **metadata) -> JobConfig:
        if name in self._jobs:
            self.logger.warning("[job_registry] Job '%s' 已存在，覆盖注册", name)
        config = JobConfig(name=name, job_type=JobType.LOOP, enabled=enabled, interval=interval, retries=retries, retry_delay=retry_delay, metadata=metadata)
        instance = JobInstance(config=config, coro=coro)
        self._jobs[name] = instance
        self._save_state()
        self.logger.info("[job_registry] 注册 LoopJob '%s' interval=%.0fs", name, interval)
        return config

    def register_queued(self, name: str, coro, *, priority: int = 0, enabled: bool = True, retries: int = 3, retry_delay: float = 5.0, **metadata) -> JobConfig:
        if name in self._jobs:
            self.logger.warning("[job_registry] Job '%s' 已存在，覆盖注册", name)
        config = JobConfig(name=name, job_type=JobType.QUEUED, enabled=enabled, priority=priority, retries=retries, retry_delay=retry_delay, metadata=metadata)
        instance = JobInstance(config=config, coro=coro)
        self._jobs[name] = instance
        self._save_state()
        self.logger.info("[job_registry] 注册 QueuedJob '%s' priority=%d", name, priority)
        return config

    def start(self, name: str) -> bool:
        if name not in self._jobs:
            return False
        instance = self._jobs[name]
        if not instance.config.enabled:
            return False
        if instance.task and not instance.task.done():
            return True
        if not instance.coro:
            return False

        if instance.config.job_type == JobType.QUEUED:
            asyncio.create_task(self._run_queued_job(name))
        else:
            instance.task = asyncio.create_task(self._run_job_loop(name), name=f"job_{name}")
        instance.status = JobStatus.RUNNING
        self._save_state()
        return True

    def start_all(self) -> None:
        for name in self._jobs:
            instance = self._jobs[name]
            if instance.config.enabled:
                self.start(name)

    def pause(self, name: str) -> bool:
        if name not in self._jobs:
            return False
        self._jobs[name].config.enabled = False
        self._jobs[name].status = JobStatus.PAUSED
        self._save_state()
        return True

    def resume(self, name: str) -> bool:
        if name not in self._jobs:
            return False
        self._jobs[name].config.enabled = True
        self._jobs[name].status = JobStatus.PENDING
        self._save_state()
        return True

    def cancel(self, name: str) -> bool:
        if name not in self._jobs:
            return False
        instance = self._jobs[name]
        if instance.task and not instance.task.done():
            instance.task.cancel()
            instance.status = JobStatus.CANCELLED
            self._save_state()
            return True
        return False

    async def delete(self, name: str) -> bool:
        if name not in self._jobs:
            return False
        instance = self._jobs[name]
        module_name = f"job_{name}_v{instance.plugin_version}"
        module = sys.modules.get(module_name)
        if module and hasattr(module, "teardown"):
            try:
                await module.teardown(self.gateway)
            except Exception as e:
                self.logger.error("[job] '%s' teardown 失败（忽略）: %s", name, e)
        if instance.task and not instance.task.done():
            instance.task.cancel()
        sys.modules.pop(module_name, None)
        del self._jobs[name]
        self._save_state()
        return True

    def update(self, name: str, **kwargs) -> bool:
        if name not in self._jobs:
            return False
        config = self._jobs[name].config
        if 'enabled' in kwargs:
            config.enabled = kwargs['enabled']
        if 'interval' in kwargs:
            config.interval = kwargs['interval']
        if 'retries' in kwargs:
            config.retries = kwargs['retries']
        if 'retry_delay' in kwargs:
            config.retry_delay = kwargs['retry_delay']
        if 'priority' in kwargs:
            config.priority = kwargs['priority']
        if 'metadata' in kwargs:
            config.metadata.update(kwargs['metadata'])
        self._save_state()
        return True

    def get(self, name: str) -> Optional[JobInstance]:
        return self._jobs.get(name)

    def list_all(self) -> List[Dict[str, Any]]:
        return [self._to_dict(name) for name in self._jobs]

    def list_by_type(self, job_type: JobType) -> List[Dict[str, Any]]:
        return [self._to_dict(name) for name, instance in self._jobs.items() if instance.config.job_type == job_type]

    def _to_dict(self, name: str) -> Dict[str, Any]:
        instance = self._jobs[name]
        return {
            "name": name,
            "type": instance.config.job_type.value,
            "status": instance.status.value,
            "enabled": instance.config.enabled,
            "interval": instance.config.interval,
            "priority": instance.config.priority,
            "retries": instance.config.retries,
            "run_count": instance.run_count,
            "last_run": instance.last_run,
            "last_error": instance.last_error,
            "task_running": instance.task is not None and not instance.task.done(),
            "metadata": instance.config.metadata,
            "plugin_path": instance.config.plugin_path,
        }

    async def _run_job_loop(self, name: str) -> None:
        instance = self._jobs[name]
        config = instance.config

        self.logger.info("[job] '%s' 启动 (type=%s)", name, config.job_type.value)

        while not self.gateway._shutting_down:
            if not config.enabled:
                await asyncio.sleep(5.0)
                continue

            for attempt in range(1, config.retries + 1):
                try:
                    if instance.coro:
                        await instance.coro(self.gateway)
                    instance.last_run = datetime.now().isoformat()
                    instance.last_error = None
                    instance.run_count += 1
                    instance.status = JobStatus.COMPLETED if config.interval <= 0 else JobStatus.RUNNING
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    instance.last_error = f"{type(exc).__name__}: {exc}"
                    self.logger.exception("[job] '%s' 执行失败（第 %d/%d 次）: %s", name, attempt, config.retries, exc)
                    if attempt < config.retries:
                        await asyncio.sleep(config.retry_delay)
                    else:
                        instance.status = JobStatus.FAILED

            self._save_state()

            if config.interval <= 0:
                self.logger.info("[job] '%s' 为单次任务，执行完毕", name)
                break
            await asyncio.sleep(config.interval)

    async def _run_queued_job(self, name: str) -> None:
        if self._queue_semaphore is None:
            self._queue_semaphore = asyncio.Semaphore(self._max_queued_concurrent)
        instance = self._jobs[name]
        config = instance.config
        instance.status = JobStatus.PENDING

        async with self._queue_semaphore:
            instance.status = JobStatus.RUNNING
            instance.task = asyncio.current_task()
            for attempt in range(1, config.retries + 1):
                try:
                    if instance.coro:
                        await instance.coro(self.gateway)
                    instance.last_run = datetime.now().isoformat()
                    instance.last_error = None
                    instance.run_count += 1
                    instance.status = JobStatus.COMPLETED
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    instance.last_error = f"{type(exc).__name__}: {exc}"
                    self.logger.exception("[job] '%s' 执行失败（第 %d/%d 次）: %s", name, attempt, config.retries, exc)
                    if attempt < config.retries:
                        await asyncio.sleep(config.retry_delay)
                    else:
                        instance.status = JobStatus.FAILED
            self._save_state()

    def shutdown(self) -> None:
        for name in list(self._jobs.keys()):
            self.cancel(name)
        self._save_state()

    async def reload_job(
        self,
        name: str,
        job_path: str,
        job_type: JobType = JobType.BASIC,
        interval: float = 0.0,
        priority: int = 0,
        retries: int = 3,
        retry_delay: float = 5.0,
        metadata: Optional[Dict[str, Any]] = None,
        auto_start: bool = True,
    ) -> Optional[JobConfig]:
        """加载或热重载用户 Job 文件。支持 setup/run/teardown 钩子。"""
        old_version = 0
        if name in self._jobs:
            old_instance = self._jobs[name]
            old_version = old_instance.plugin_version
            old_module_name = f"job_{name}_v{old_version}"
            old_module = sys.modules.get(old_module_name)
            if old_module and hasattr(old_module, "teardown"):
                try:
                    await old_module.teardown(self.gateway)
                except Exception as e:
                    self.logger.error("[job] '%s' teardown 失败（忽略）: %s", name, e)
            if old_instance.task and not old_instance.task.done():
                old_instance.task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(old_instance.task), timeout=10.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            sys.modules.pop(old_module_name, None)
            del self._jobs[name]

        version = old_version + 1
        module_name = f"job_{name}_v{version}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, job_path)
            if not spec or not spec.loader:
                self.logger.error("[job] 无法加载 Job '%s': 无效路径 %s", name, job_path)
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as e:
            self.logger.exception("[job] 加载 Job '%s' 失败: %s", name, e)
            sys.modules.pop(module_name, None)
            return None

        if not hasattr(module, "run") or not callable(module.run):
            self.logger.error("[job] Job '%s' 必须实现 async def run(gateway)", name)
            sys.modules.pop(module_name, None)
            return None

        if hasattr(module, "setup") and callable(module.setup):
            try:
                await module.setup(self.gateway)
            except Exception as e:
                self.logger.error("[job] '%s' setup 失败（忽略）: %s", name, e)

        coro = module.run
        config: Optional[JobConfig] = None
        if job_type == JobType.LOOP:
            config = self.register_loop(name, coro, interval=interval, retries=retries, retry_delay=retry_delay, **(metadata or {}))
        elif job_type == JobType.QUEUED:
            config = self.register_queued(name, coro, priority=priority, retries=retries, retry_delay=retry_delay, **(metadata or {}))
        else:
            config = self.register_basic(name, coro, retries=retries, retry_delay=retry_delay, **(metadata or {}))

        if config:
            config.plugin_path = job_path
            self._jobs[name].plugin_version = version
            self._save_state()
            if auto_start:
                self.start(name)
            self.logger.info("[job] '%s' 已加载 (v%d, type=%s)", name, version, job_type.value)

        return config

    def register_plugin(self, name: str, plugin_path: str, job_type: JobType = JobType.BASIC,
                        interval: float = 0.0,
                        priority: int = 0, retries: int = 3, retry_delay: float = 5.0,
                        metadata: Optional[Dict[str, Any]] = None) -> Optional[JobConfig]:
        """兼容入口，异步调用 reload_job。"""
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(self.reload_job(
                name, plugin_path, job_type=job_type, interval=interval,
                priority=priority, retries=retries, retry_delay=retry_delay, metadata=metadata,
            ))
            return None
        return loop.run_until_complete(self.reload_job(
            name, plugin_path, job_type=job_type, interval=interval,
            priority=priority, retries=retries, retry_delay=retry_delay, metadata=metadata,
        ))
