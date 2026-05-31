#!/usr/bin/env python3
"""
Course Data Manager
-------------------
管理课程相关数据：
- timetable: 课程表（有哪些课可用）
- schedules: 直播调度记录（开始监听、结束整理）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TIMETABLE_FILE = DATA_DIR / "timetable.json"
SCHEDULE_FILE = DATA_DIR / "schedules.json"
ASSIGNMENTS_FILE = DATA_DIR / "assignments.json"


def _load_json(path: Path, default: Any) -> Any:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save_json(path: Path, data: Any) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------
# Timetable - 课程表（有哪些课可用）
# ---------------------------------------------------------------------

def update_timetable(course_list: List[Dict[str, Any]]) -> None:
    """
    更新课程表。
    每周同步一次，记录用户有哪些课（课程ID、名称等）
    """
    data = {
        "courses": course_list,
        "updated_at": datetime.now().isoformat(),
    }
    _save_json(TIMETABLE_FILE, data)


def get_timetable() -> List[Dict[str, Any]]:
    """获取课程表（有哪些课可用）"""
    data = _load_json(TIMETABLE_FILE, {})
    return data.get("courses", [])


def get_course_ids() -> List[str]:
    """获取所有课程ID"""
    return [str(c.get("id")) for c in get_timetable() if c.get("id")]


# ---------------------------------------------------------------------
# Schedule - 直播调度记录
# ---------------------------------------------------------------------

class ScheduleStatus:
    PENDING = "pending"      # 待执行
    RUNNING = "running"      # 执行中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"        # 失败


def add_schedule(
    schedule_id: str,
    schedule_type: str,  # "monitor_start" | "monitor_end"
    course_id: str,
    lecture_id: str,
    trigger_at: datetime,
    metadata: Optional[Dict] = None,
) -> None:
    """
    添加 Schedule。

    Args:
        schedule_id: 唯一标识
        schedule_type: monitor_start (开始监听) / monitor_end (结束整理)
        course_id: 课程ID
        lecture_id: 直播/课时ID
        trigger_at: 触发时间
        metadata: 额外信息（如预定上课时间、实际上课时间等）
    """
    data = _load_json(SCHEDULE_FILE, {"schedules": []})

    # 检查是否已存在
    for s in data["schedules"]:
        if s["id"] == schedule_id:
            new_trigger_at = trigger_at.isoformat()
            # 如果已经执行完成且触发时间没变，则不复用（避免同一小时内重复触发）
            if s["status"] == ScheduleStatus.COMPLETED and s.get("trigger_at") == new_trigger_at:
                s["metadata"] = {**s.get("metadata", {}), **(metadata or {})}
                _save_json(SCHEDULE_FILE, data)
                return

            # 更新：重置为 pending（允许 scan_live 每天复用同一 ID）
            s["trigger_at"] = new_trigger_at
            s["status"] = ScheduleStatus.PENDING
            s["executed_at"] = None
            s.pop("result", None)
            s["metadata"] = {**s.get("metadata", {}), **(metadata or {})}
            _save_json(SCHEDULE_FILE, data)
            return

    # 新增
    schedule = {
        "id": schedule_id,
        "type": schedule_type,
        "course_id": course_id,
        "lecture_id": lecture_id,
        "trigger_at": trigger_at.isoformat(),
        "status": ScheduleStatus.PENDING,
        "created_at": datetime.now().isoformat(),
        "executed_at": None,
        "metadata": metadata or {},
    }
    data["schedules"].append(schedule)
    _save_json(SCHEDULE_FILE, data)


def get_pending_schedules(before: Optional[datetime] = None) -> List[Dict]:
    """获取待执行的 Schedule"""
    data = _load_json(SCHEDULE_FILE, {"schedules": []})
    now = datetime.now()
    pending = []
    for s in data["schedules"]:
        if s["status"] == ScheduleStatus.PENDING:
            trigger_at = datetime.fromisoformat(s["trigger_at"])
            if before is None or trigger_at <= before:
                pending.append({**s, "_trigger_at": trigger_at})
    return sorted(pending, key=lambda x: x["_trigger_at"])


def get_schedules(course_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict]:
    """获取 Schedule 列表（支持筛选）"""
    data = _load_json(SCHEDULE_FILE, {"schedules": []})
    result = data["schedules"]
    if course_id:
        result = [s for s in result if s["course_id"] == course_id]
    if status:
        result = [s for s in result if s["status"] == status]
    return sorted(result, key=lambda x: x["trigger_at"], reverse=True)


def mark_schedule_executed(schedule_id: str, success: bool = True, result: Optional[Dict] = None) -> None:
    """标记 Schedule 已执行（归档）"""
    data = _load_json(SCHEDULE_FILE, {"schedules": []})
    for s in data["schedules"]:
        if s["id"] == schedule_id:
            s["status"] = ScheduleStatus.COMPLETED if success else ScheduleStatus.FAILED
            s["executed_at"] = datetime.now().isoformat()
            if result:
                s["result"] = result
            break
    _save_json(SCHEDULE_FILE, data)


# ---------------------------------------------------------------------
# Assignments - 作业同步数据
# ---------------------------------------------------------------------

def update_assignments(course_id: str, assignments: List[Dict[str, Any]]) -> None:
    """
    更新指定课程的作业数据。
    按 course_id -> assignment_id 两级索引存储，支持增量更新。
    """
    data = _load_json(ASSIGNMENTS_FILE, {"courses": {}})
    course_id = str(course_id)

    # 初始化课程数据（如果不存在）
    if course_id not in data["courses"]:
        data["courses"][course_id] = {
            "assignments": {},
            "last_synced_at": None
        }

    # 处理每个作业
    for assignment in assignments:
        assignment_id = str(assignment["id"])
        existing = data["courses"][course_id]["assignments"].get(assignment_id, {})

        # 计算是否有变更
        has_changes = False
        if existing.get("updated_at") != assignment.get("updated_at"):
            has_changes = True

        # 更新作业数据
        data["courses"][course_id]["assignments"][assignment_id] = {
            **assignment,
            "last_synced_at": datetime.now().isoformat(),
            "last_changed_at": datetime.now().isoformat() if has_changes else existing.get("last_changed_at")
        }

    # 更新课程最后同步时间
    data["courses"][course_id]["last_synced_at"] = datetime.now().isoformat()

    _save_json(ASSIGNMENTS_FILE, data)


def get_assignments(course_id: Optional[str] = None) -> Dict[str, Any]:
    """
    获取作业数据。
    如果指定 course_id，返回该课程的作业列表；否则返回所有课程的作业数据。
    """
    data = _load_json(ASSIGNMENTS_FILE, {"courses": {}})

    if course_id:
        course_id = str(course_id)
        return data["courses"].get(course_id, {"assignments": {}})

    return data


def get_assignment(course_id: str, assignment_id: str) -> Optional[Dict[str, Any]]:
    """获取单个作业的详细信息"""
    course_id = str(course_id)
    assignment_id = str(assignment_id)

    data = _load_json(ASSIGNMENTS_FILE, {"courses": {}})
    if course_id not in data["courses"]:
        return None

    return data["courses"][course_id]["assignments"].get(assignment_id)


