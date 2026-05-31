# AGENTS.md

## Project Overview

AutoCanvas Gateway is a Python-based async job scheduling system for automating Canvas LMS video processing. It monitors live streams, transcribes VODs using Qwen3-ASR, and provides a plugin system for extensibility.

## Dev Environment

- The gateway runs in tmux session `auto-canvas`
- Use the specific conda environment `auto-canvas` (located in `/opt/homebrew/Caskroom/miniconda/base/envs/auto-canvas`)

## Common Commands

**Start the Gateway:**
```bash
conda activate auto-canvas
python start_gateway.py
```

The API server listens on `http://0.0.0.0:8080`.

**Check system health:**
```bash
curl http://localhost:8080/health
```

**List all jobs:**
```bash
curl http://localhost:8080/jobs
```

## Architecture Overview

### Core Components

1. **Gateway (`gateway.py`)**: Central async orchestrator with ThreadPoolExecutor for sync operations. Manages session, event queue, and job lifecycle.

2. **JobRegistry (`job_manager.py`)**: Foundation layer managing 4 job types:
   - `BasicJob`: Single execution or long-running
   - `LoopJob`: Periodic execution with interval
   - `QueuedJob`: Concurrency-limited with semaphore
   - `PluginJob`: Dynamic Python script loading

3. **Built-in Jobs (`builtin_jobs.py`)**: Three core loop jobs:
   - `update_timetable`: Weekly course sync (7-day interval)
   - `scan_live`: Hourly scan for upcoming live streams (24h lookahead)
   - `schedule_executor`: Minute-by-minute schedule trigger

4. **API Layer (`video_api.py`)**: HTTP endpoints for job CRUD, plugin upload, and Canvas operations.

### Data Flow

```
scan_live (hourly) → detects live streams → registers Schedule
schedule_executor (per minute) → triggers at (start_time - 10min)
  → starts live_monitor Job (QueuedJob, long-running)
  → at end_time: cancels monitor → scans VODs
```

### Key Files

- `config.py`: Live streaming config (keywords, thresholds)
- `canvas_auth.py`: Canvas session management with auto-refresh
- `canvas_video.py`: Canvas API wrappers for live/VOD lists
- `live_worker.py`: Live stream processing (ffmpeg → ASR)
- `replay_worker.py`: VOD transcription worker
- `course_data.py`: Local state management (JSON-based)

### State Persistence

All state is stored in `data/` directory:
- `jobs_state.json`: Job registry state
- `video_state.json`: Course and video metadata
- `schedule.json`: Pending schedules
- `stream_labels.json`: Camera/screen stream type cache

### Plugin System

Plugins are Python files in `plugins/` with required `run(gateway)` async function. Optional `setup(gateway)` and `teardown(gateway)` hooks. Uploaded via `POST /plugins` with code inline.

### Conventions

- All async operations use `gateway.run_sync()` for blocking calls (requests, file I/O)
- Logging via `gateway.logger` - writes to `logs/gateway.log` with daily rotation
- Session access via `gateway.session` (requests.Session, Canvas authenticated)
- Job metadata uses Chinese descriptions for UI display
- Transcripts output to `transcript/` with naming pattern: `yymmdd-HHMM-{coursename}-{replay|stream}.txt`
