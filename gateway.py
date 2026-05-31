#!/usr/bin/env python3
"""
AutoCanvas Gateway - Foundation
-------------------------------
基于 Job Registry 的常驻异步调度器。

架构:
- Foundation: JobRegistry 管理所有任务类型
- Built-in: Canvas 视频相关 Job
- Plugin: 动态加载 .py 脚本

所有任务统一通过 JobRegistry 管理，支持:
- Basic Job: 单次执行
- Loop Job: 周期性执行
- Queued Job: 排队执行
- Plugin Job: 动态加载
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from jobs.job_manager import JobRegistry, JobType
from utils.canvas_auth import ensure_session, is_session_valid

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "gateway.log"

HEARTBEAT_INTERVAL = 60.0
SESSION_CHECK_INTERVAL = 300.0


class GatewayFormatter(logging.Formatter):
    """自定义格式: '<timestamp> [LEVEL] 内容'"""
    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        return f"{ts} [{record.levelname}] {record.getMessage()}"


class AutoCanvasGateway:
    """
    AutoCanvas 常驻调度器。

    核心组件:
    - job_registry: 所有任务的注册中心
    - session: Canvas 会话管理
    - event_queue: 事件总线
    """

    def __init__(
        self,
        *,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        session_check_interval: float = SESSION_CHECK_INTERVAL,
        log_level: int = logging.INFO,
        auto_prompt: bool = False,
        executor_workers: int = 4,
    ):
        self.heartbeat_interval = heartbeat_interval
        self.session_check_interval = session_check_interval
        self.auto_prompt = auto_prompt
        self.log_level = log_level
        self.executor_workers = executor_workers

        # 延迟初始化
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        self._executor: Optional[Any] = None

        # 核心组件
        self.logger = self._setup_logger()
        self.job_registry: Optional[JobRegistry] = None
        self._session: Optional[requests.Session] = None
        self._event_queue: Optional[asyncio.Queue] = None
        self._event_handlers: dict[str, list[Callable]] = {}

        # 状态
        self.started_at: Optional[str] = None
        self.last_heartbeat: Optional[str] = None

    def _setup_logger(self) -> logging.Logger:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("AutoCanvasGateway")
        logger.setLevel(self.log_level)

        if logger.hasHandlers():
            logger.handlers.clear()

        # 按天滚动的文件
        fh = logging.handlers.TimedRotatingFileHandler(
            LOG_FILE, when='midnight', interval=1, backupCount=30,
            encoding='utf-8'
        )
        fh.suffix = "%Y%m%d"
        fh.setFormatter(GatewayFormatter())
        logger.addHandler(fh)

        # 控制台
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(GatewayFormatter())
        logger.addHandler(ch)

        return logger

    def _init_async(self) -> None:
        """初始化异步资源"""
        self._loop = asyncio.get_running_loop()
        self._shutdown_event = asyncio.Event()
        from concurrent.futures import ThreadPoolExecutor
        self._executor = ThreadPoolExecutor(
            max_workers=self.executor_workers,
            thread_name_prefix="acgw_",
        )
        self._event_queue = asyncio.Queue(maxsize=1000)

    @property
    def _shutting_down(self) -> bool:
        return self._shutdown_event is not None and self._shutdown_event.is_set()

    # ---------------------------------------------------------------------
    # 线程池包装
    # ---------------------------------------------------------------------

    async def run_sync(self, fn, *args, **kwargs) -> Any:
        """同步函数丢进线程池"""
        if self._executor is None:
            raise RuntimeError("ThreadPoolExecutor 未初始化")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    # ---------------------------------------------------------------------
    # Session 管理
    # ---------------------------------------------------------------------

    async def _ensure_session(self) -> requests.Session:
        if self._session is not None and is_session_valid(self._session):
            return self._session

        self.logger.info("[session] 初始化 Canvas Session...")
        try:
            self._session = await self.run_sync(
                ensure_session,
                session_path=Path("canvas_session.json"),
                auto_prompt=self.auto_prompt,
            )
            self.logger.info("[session] Session 就绪")
        except Exception as exc:
            self.logger.error("[session] 初始化失败: %s", exc)
            raise
        return self._session

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            raise RuntimeError("Session 未初始化")
        return self._session

    # ---------------------------------------------------------------------
    # 事件系统
    # ---------------------------------------------------------------------

    def on_event(self, event_type: str, handler: Callable) -> None:
        """注册异步事件处理器。handler 签名: async def handler(gateway, event_dict)"""
        self._event_handlers.setdefault(event_type, []).append(handler)

    async def post_event(self, event_type: str, **data) -> None:
        """触发事件: 入队 + 立即调用已注册 handler。"""
        event = {"type": event_type, "ts": datetime.now().isoformat(), **data}
        if self._event_queue is not None:
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                self.logger.warning("[event] 队列已满，丢弃: %s", event_type)
        for handler in self._event_handlers.get(event_type, []):
            try:
                await handler(self, event)
            except Exception as exc:
                self.logger.error("[event] handler 异常 (%s): %s", event_type, exc)

    # ---------------------------------------------------------------------
    # 生命周期
    # ---------------------------------------------------------------------

    async def start(self) -> None:
        """启动 Gateway"""
        self._init_async()

        # 信号处理
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._loop.add_signal_handler(sig, lambda s=sig: self._signal_handler(s))

        self.started_at = datetime.now().isoformat()
        self.logger.info("-" * 50)
        self.logger.info("AutoCanvas Gateway 启动")
        self.logger.info("-" * 50)

        # Session
        await self._ensure_session()

        # Job Registry
        self.job_registry = JobRegistry(self)

        # 如果有设置回调，执行额外设置
        if hasattr(self, '_setup_callback') and self._setup_callback:
            import asyncio
            await self._setup_callback(self)

        # 启动所有已注册且启用的 Job
        self.job_registry.start_all()

        # 后台任务
        asyncio.create_task(self._session_keepalive())
        asyncio.create_task(self._heartbeat())
        asyncio.create_task(self._event_dispatcher())

        self.logger.info("[start] Gateway 已启动，等待任务...")
        await self._shutdown_event.wait()
        await self.shutdown()

    def _signal_handler(self, sig: signal.Signals) -> None:
        self.logger.info("[signal] 收到 %s，准备退出...", sig.name)
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """优雅关闭"""
        self.logger.info("[shutdown] 开始清理...")

        if self.job_registry:
            self.job_registry.shutdown()

        if self._executor:
            self._executor.shutdown(wait=True)

        self.logger.info("[shutdown] Gateway 已停止")

    # ---------------------------------------------------------------------
    # 后台任务
    # ---------------------------------------------------------------------

    async def _session_keepalive(self) -> None:
        """Session 保活，失败时触发 auth_failure 事件"""
        while not self._shutting_down:
            try:
                if self._session is None or not is_session_valid(self._session):
                    self.logger.warning("[session_keepalive] Session 失效，尝试恢复...")
                    try:
                        self._session = await self.run_sync(
                            ensure_session,
                            session_path=Path("canvas_session.json"),
                            auto_prompt=self.auto_prompt,
                        )
                        self.logger.info("[session_keepalive] Session 恢复成功")
                    except Exception as exc:
                        self.logger.error("[session_keepalive] 恢复失败: %s", exc)
                        await self.post_event(
                            "auth_failure",
                            error=str(exc),
                            source="session_keepalive",
                        )
            except Exception as exc:
                self.logger.error("[session_keepalive] 异常: %s", exc)
            await asyncio.sleep(self.session_check_interval)

    async def _heartbeat(self) -> None:
        """心跳"""
        while not self._shutting_down:
            self.last_heartbeat = datetime.now().isoformat()
            self.logger.debug(
                "[heartbeat] jobs=%d",
                len(self.job_registry._jobs) if self.job_registry else 0
            )
            await asyncio.sleep(self.heartbeat_interval)

    async def _event_dispatcher(self) -> None:
        """事件队列消费（handler 已在 post_event 中同步调用，队列用于审计/扩展）"""
        while not self._shutting_down:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                self.logger.debug("[event] 已消费: %s", event.get("type", "?"))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    # ---------------------------------------------------------------------
    # 便捷接口
    # ---------------------------------------------------------------------

    def register_job(self, name: str, coro, **kwargs) -> Any:
        """便捷注册 Job（自动判断类型）"""
        if self.job_registry is None:
            raise RuntimeError("JobRegistry 未初始化")

        job_type = kwargs.pop('job_type', JobType.BASIC)
        if job_type == "scheduled":
            raise ValueError("JobRegistry scheduled job 已移除；请使用业务 Schedule 或 LoopJob")
        if isinstance(job_type, str):
            job_type = JobType(job_type)

        if job_type == JobType.LOOP:
            return self.job_registry.register_loop(name, coro, **kwargs)
        elif job_type == JobType.QUEUED:
            return self.job_registry.register_queued(name, coro, **kwargs)
        else:
            return self.job_registry.register_basic(name, coro, **kwargs)

    def schedule_interval(self, name: str, coro, interval: float, **kwargs) -> Any:
        """按间隔循环执行"""
        return self.register_job(name, coro, job_type=JobType.LOOP, interval=interval, **kwargs)


if __name__ == "__main__":
    gw = AutoCanvasGateway()

    async def demo_job(gateway):
        gateway.logger.info("Demo job running")

    gw.register_job("demo", demo_job, job_type=JobType.LOOP, interval=10)

    try:
        asyncio.run(gw.start())
    except KeyboardInterrupt:
        pass
