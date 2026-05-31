#!/usr/bin/env python3
"""
AutoCanvas Gateway 启动入口
--------------------------
"""
from __future__ import annotations

import os
os.environ["HF_HUB_OFFLINE"] = "1"

import asyncio
import sys

from gateway import AutoCanvasGateway
from jobs.builtin_jobs import register_builtin_jobs
from api.video_api import run_video_api


def main():
    gw = AutoCanvasGateway(auto_prompt=True)

    async def setup_jobs(g):
        # 注册内置 Job
        register_builtin_jobs(g)

        # API Server
        async def api_job(gw):
            await run_video_api(gw, host="0.0.0.0", port=8080)

        g.job_registry.register_basic("api_server", api_job)
        g.job_registry.start("api_server")

    gw._setup_callback = setup_jobs

    try:
        asyncio.run(gw.start())
    except KeyboardInterrupt:
        print("\nShutdown requested")
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
