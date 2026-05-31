#!/usr/bin/env python3
"""
作业自动同步脚本（独立运行版）
每6小时同步一次所有课程的作业和附件
无需重启gateway，独立运行
"""
import os
import re
import json
import hashlib
import time
import html
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import requests
from requests.sessions import Session

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent
# Session文件路径
SESSION_FILE = BASE_DIR / "canvas_session.json"
# 作业数据存储路径
ASSIGNMENTS_DATA_FILE = BASE_DIR / "utils" / "data" / "assignments.json"
# 作业附件存储根目录
ATTACHMENTS_ROOT = BASE_DIR / "assignments"

# 确保目录存在
ASSIGNMENTS_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
ATTACHMENTS_ROOT.mkdir(parents=True, exist_ok=True)


def html_to_text(html_str: str) -> str:
    """将HTML转换为纯文本"""
    if not html_str:
        return ""
    # 移除script和style标签
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html_str, flags=re.DOTALL | re.IGNORECASE)
    # 移除所有HTML标签
    text = re.sub(r'<[^>]+>', '', text)
    # 解码HTML实体
    text = html.unescape(text)
    # 合并多余空格和换行
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def load_session() -> Session:
    """加载已保存的Canvas Session"""
    if not SESSION_FILE.exists():
        raise RuntimeError(f"Session文件不存在: {SESSION_FILE}，请先登录Canvas")

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/134.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })

    try:
        cookies = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        for c in cookies:
            session.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
    except Exception as e:
        raise RuntimeError(f"加载Session失败: {e}")

    # 验证Session是否有效
    try:
        resp = session.get("https://oc.sjtu.edu.cn/api/v1/dashboard/dashboard_cards", timeout=15)
        if resp.status_code != 200:
            raise RuntimeError("Session已失效，请重新登录Canvas")
    except Exception as e:
        raise RuntimeError(f"验证Session失败: {e}")

    return session


def get_course_list(session: Session) -> List[Dict[str, Any]]:
    """获取当前用户的所有课程列表"""
    url = "https://oc.sjtu.edu.cn/api/v1/courses"
    params = {
        "enrollment_state": "active",
        "per_page": 100,
    }
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    courses = resp.json()

    result = []
    for course in courses:
        course_id = course.get("id")
        course_name = course.get("name", "")
        if course_id:
            result.append({
                "id": str(course_id),
                "name": course_name,
            })
    return result


def get_course_assignments(session: Session, course_id: str) -> List[Dict[str, Any]]:
    """获取指定课程的作业列表"""
    url = f"https://oc.sjtu.edu.cn/api/v1/courses/{course_id}/assignments"
    params = {
        "per_page": 100,
        "include": ["submission", "description"],
    }
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def download_attachment(session: Session, attachment: dict, save_dir: Path) -> dict:
    """
    下载作业附件，保存到指定目录
    返回更新后的附件信息
    """
    save_dir.mkdir(parents=True, exist_ok=True)

    file_id = str(attachment["id"])
    file_name = attachment["display_name"]
    file_size = attachment["size"]
    updated_at = attachment["updated_at"]
    download_url = attachment["url"]

    # 清理文件名中的非法字符
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", file_name)
    save_path = save_dir / safe_name

    # 检查文件是否已存在且相同（大小相同）
    if save_path.exists() and save_path.stat().st_size == file_size:
        # 文件已存在，直接返回路径
        return {
            "file_id": file_id,
            "display_name": file_name,
            "size": file_size,
            "content_type": attachment.get("content-type", ""),
            "canvas_updated_at": updated_at,
            "local_path": str(save_path.relative_to(BASE_DIR)),
            "md5": hashlib.md5(save_path.read_bytes()).hexdigest(),
            "downloaded_at": datetime.now().isoformat(),
        }

    # 下载文件
    resp = session.get(download_url, timeout=60)
    resp.raise_for_status()

    # 保存文件
    save_path.write_bytes(resp.content)

    # 计算MD5
    md5_hash = hashlib.md5(resp.content).hexdigest()

    return {
        "file_id": file_id,
        "display_name": file_name,
        "size": file_size,
        "content_type": attachment.get("content-type", ""),
        "canvas_updated_at": updated_at,
        "local_path": str(save_path.relative_to(BASE_DIR)),
        "md5": md5_hash,
        "downloaded_at": datetime.now().isoformat(),
    }


def load_existing_assignments() -> Dict[str, Any]:
    """加载已有的作业数据"""
    if not ASSIGNMENTS_DATA_FILE.exists():
        return {"courses": {}}
    try:
        return json.loads(ASSIGNMENTS_DATA_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"加载现有作业数据失败: {e}，将创建新的数据文件")
        return {"courses": {}}


def save_assignments(data: Dict[str, Any]) -> None:
    """保存作业数据到文件"""
    ASSIGNMENTS_DATA_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def sync_single_course(session: Session, course_id: str, existing_data: Dict[str, Any]) -> tuple[int, int]:
    """
    同步单个课程的作业
    返回 (更新的作业数, 新下载的附件数)
    """
    print(f"\n[{datetime.now()}] 同步课程 {course_id} 的作业...")

    # 获取最新作业列表
    assignments = get_course_assignments(session, course_id)

    updated_count = 0
    downloaded_attachments = 0

    # 获取课程现有数据
    course_data = existing_data["courses"].get(course_id, {
        "assignments": {},
        "last_synced_at": None
    })
    existing_assignments = course_data["assignments"]

    processed_assignments = {}

    for assignment in assignments:
        assignment_id = str(assignment["id"])
        assignment_name = assignment["name"]
        updated_at = assignment["updated_at"]

        # 检查是否需要更新
        existing = existing_assignments.get(assignment_id)
        if existing and existing.get("updated_at") == updated_at:
            # 无变化，直接复用现有数据
            processed_assignments[assignment_id] = existing
            print(f"  作业 {assignment_name} ({assignment_id}) 无变化，跳过")
            continue

        # 有变化，处理作业
        print(f"  处理作业: {assignment_name} ({assignment_id})")

        # 转换描述为纯文本
        description_html = assignment.get("description", "")
        description_text = html_to_text(description_html)

        # 处理附件
        attachments = []
        if assignment.get("attachments"):
            attachment_dir = ATTACHMENTS_ROOT / str(course_id) / str(assignment_id)
            for att in assignment["attachments"]:
                try:
                    downloaded = download_attachment(session, att, attachment_dir)
                    attachments.append(downloaded)
                    downloaded_attachments += 1
                    print(f"    已下载附件: {downloaded['display_name']}")
                except Exception as e:
                    print(f"    下载附件 {att.get('display_name', 'unknown')} 失败: {e}")
                    # 保留原始附件信息，但标记下载失败
                    attachments.append({
                        "file_id": str(att["id"]),
                        "display_name": att["display_name"],
                        "size": att["size"],
                        "content_type": att.get("content-type", ""),
                        "canvas_updated_at": att["updated_at"],
                        "download_error": str(e),
                    })

        # 构建作业数据
        processed = {
            "id": assignment["id"],
            "name": assignment["name"],
            "due_at": assignment.get("due_at"),
            "points_possible": assignment.get("points_possible"),
            "submission_types": assignment.get("submission_types", []),
            "html_url": assignment.get("html_url"),
            "description_html": description_html,
            "description_text": description_text,
            "workflow_state": assignment.get("workflow_state"),
            "submission": assignment.get("submission"),
            "created_at": assignment.get("created_at"),
            "updated_at": updated_at,
            "attachments": attachments,
            "last_synced_at": datetime.now().isoformat(),
            "last_changed_at": datetime.now().isoformat(),
        }

        processed_assignments[assignment_id] = processed
        updated_count += 1

    # 更新课程数据
    course_data["assignments"] = processed_assignments
    course_data["last_synced_at"] = datetime.now().isoformat()
    existing_data["courses"][course_id] = course_data

    print(f"课程 {course_id} 同步完成: 更新 {updated_count} 个作业，下载 {downloaded_attachments} 个附件")
    return updated_count, downloaded_attachments


def sync_all_courses() -> None:
    """同步所有课程的作业"""
    print(f"\n{'-'*60}")
    print(f"[{datetime.now()}] 开始全量作业同步")
    print(f"{'-'*60}")

    try:
        # 加载Session
        session = load_session()
        print("Session加载成功")

        # 获取课程列表
        courses = get_course_list(session)
        print(f"获取到 {len(courses)} 门课程")

        # 加载现有数据
        existing_data = load_existing_assignments()

        total_updated = 0
        total_downloaded = 0

        # 同步每门课程
        for course in courses:
            course_id = course["id"]
            course_name = course["name"]
            print(f"\n处理课程: {course_name} ({course_id})")
            try:
                updated, downloaded = sync_single_course(session, course_id, existing_data)
                total_updated += updated
                total_downloaded += downloaded
            except Exception as e:
                print(f"同步课程 {course_id} 失败: {e}")

        # 保存更新后的数据
        save_assignments(existing_data)

        print(f"\n{'-'*60}")
        print(f"[{datetime.now()}] 全量同步完成")
        print(f"  共更新 {total_updated} 个作业")
        print(f"  共下载 {total_downloaded} 个附件")
        print(f"  数据已保存到: {ASSIGNMENTS_DATA_FILE}")
        print(f"  附件已保存到: {ATTACHMENTS_ROOT}")
        print(f"{'-'*60}\n")

    except Exception as e:
        print(f"同步失败: {e}")
        raise


def main():
    """主函数：每6小时同步一次"""
    print("作业自动同步服务启动")
    print(f"将每6小时同步一次所有课程的作业和附件")
    print(f"按 Ctrl+C 停止服务\n")

    while True:
        try:
            sync_all_courses()
        except KeyboardInterrupt:
            print("\n收到停止信号，退出服务")
            break
        except Exception as e:
            print(f"同步过程中发生错误: {e}，将在6小时后重试")

        # 等待6小时
        print(f"\n[{datetime.now()}] 等待6小时后进行下一次同步...\n")
        time.sleep(6 * 3600)


if __name__ == "__main__":
    main()
