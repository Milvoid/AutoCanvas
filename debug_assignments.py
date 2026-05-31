#!/usr/bin/env python3
"""
调试脚本：验证能否从 SJTU Canvas 获取课程作业列表与单个作业详情。

用法：
    python debug_assignments.py [course_id]

不传 course_id 则默认使用示例课程 ID。
"""
from __future__ import annotations

import json
import sys
from typing import List, Dict, Any

import requests

from utils.canvas_auth import ensure_session


CANVAS_BASE = "https://oc.sjtu.edu.cn"


def list_assignments(session: requests.Session, course_id: str | int) -> List[Dict[str, Any]]:
    """
    拉取指定课程的作业列表（分页全量）。
    Canvas API: GET /api/v1/courses/:course_id/assignments
    """
    assignments: List[Dict[str, Any]] = []
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/assignments"
    params = {"per_page": 100}

    while url:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list):
            raise RuntimeError(f"非预期返回: {page}")
        assignments.extend(page)

        # Canvas 的分页用 Link header
        next_url = None
        link_hdr = resp.headers.get("Link", "")
        for part in link_hdr.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
                break
        url = next_url
        params = None

    return assignments


def get_assignment(
    session: requests.Session, course_id: str | int, assignment_id: str | int
) -> Dict[str, Any]:
    """
    获取单个作业详情。
    Canvas API: GET /api/v1/courses/:course_id/assignments/:id
    """
    url = f"{CANVAS_BASE}/api/v1/courses/{course_id}/assignments/{assignment_id}"
    # include[] 参数带上提交情况、锁定信息、overrides 等，方便看全
    params = [
        ("include[]", "submission"),
        ("include[]", "overrides"),
        ("include[]", "can_edit"),
        ("include[]", "score_statistics"),
    ]
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _short(s: Any, n: int = 80) -> str:
    if s is None:
        return ""
    s = str(s).replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> None:
    course_id = sys.argv[1] if len(sys.argv) > 1 else "YOUR_COURSE_ID"

    print("-" * 60)
    print(f"调试: 获取课程 {course_id} 的作业")
    print("-" * 60)

    session = ensure_session(auto_prompt=False)

    # 1) 作业列表
    assignments = list_assignments(session, course_id)
    print(f"\n[1] 作业列表：共 {len(assignments)} 条\n")
    for a in assignments:
        print(
            f"  - id={a.get('id')}  "
            f"due={a.get('due_at') or '—'}  "
            f"points={a.get('points_possible')}  "
            f"name={_short(a.get('name'), 40)}"
        )

    if not assignments:
        print("\n(该课程下暂无作业)")
        return

    # 2) 取第一个作业详情，看字段
    first = assignments[0]
    aid = first["id"]
    print("\n" + "-" * 60)
    print(f"[2] 作业详情: id={aid}")
    print("-" * 60)

    detail = get_assignment(session, course_id, aid)

    # 打印关键字段摘要
    keys_of_interest = [
        "id", "name", "due_at", "unlock_at", "lock_at",
        "points_possible", "submission_types", "html_url",
        "published", "has_submitted_submissions",
        "grading_type", "workflow_state", "created_at", "updated_at",
    ]
    print("\n关键字段:")
    for k in keys_of_interest:
        if k in detail:
            print(f"  {k}: {_short(detail[k], 100)}")

    desc = detail.get("description") or ""
    print(f"\n描述 HTML 长度: {len(desc)} chars")
    print(f"描述预览: {_short(desc, 200)}")

    sub = detail.get("submission")
    if sub:
        print("\n我的提交情况:")
        for k in ("workflow_state", "submitted_at", "score", "grade", "late", "missing"):
            print(f"  {k}: {sub.get(k)}")

    # 3) 把完整 JSON 写到 /tmp，方便查看
    import os
    out_dir = "/tmp/autocanvas_debug"
    os.makedirs(out_dir, exist_ok=True)
    list_path = os.path.join(out_dir, f"assignments_list_{course_id}.json")
    detail_path = os.path.join(out_dir, f"assignment_{course_id}_{aid}.json")
    with open(list_path, "w", encoding="utf-8") as f:
        json.dump(assignments, f, ensure_ascii=False, indent=2)
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    print(f"\n完整 JSON 已保存:\n  {list_path}\n  {detail_path}")


if __name__ == "__main__":
    main()
