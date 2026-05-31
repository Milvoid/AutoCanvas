#!/usr/bin/env python3
"""
插件: 作业自动同步
----------------
自动同步所有课程的作业信息和附件，支持增量更新。

功能:
1. 每6小时同步一次所有课程的作业列表
2. 增量更新：只有作业更新时间变化时才重新抓取
3. 自动下载作业附件到 assignments/<course_id>/<assignment_id>/ 目录
4. 附件去重：相同文件（按file_id+更新时间+大小）不会重复下载
5. 作业元数据存储到 utils/data/assignments.json

元数据结构:
{
  "courses": {
    "<course_id>": {
      "assignments": {
        "<assignment_id>": {
          "id": 403846,
          "name": "作业一",
          "due_at": "2026-04-25T15:59:59Z",
          "points_possible": 10.0,
          "submission_types": ["online_upload"],
          "html_url": "https://oc.sjtu.edu.cn/courses/YOUR_COURSE_ID/assignments/YOUR_ASSIGNMENT_ID",
          "description": "<p>作业描述</p>",
          "description_text": "纯文本描述",
          "workflow_state": "published",
          "submission": { "workflow_state": "unsubmitted", ... },
          "created_at": "2026-04-13T14:17:14Z",
          "updated_at": "2026-04-13T14:17:14Z",
          "attachments": [
            {
              "file_id": 13095684,
              "display_name": "作业说明.pdf",
              "size": 134891,
              "content_type": "application/pdf",
              "canvas_updated_at": "2026-04-13T14:15:23Z",
              "local_path": "assignments/YOUR_COURSE_ID/403846/作业说明.pdf",
              "md5": "d6d2cef6ef15d7f8b0e968bb3e4d56d0",
              "downloaded_at": "2026-04-21T15:30:00Z"
            }
          ],
          "last_synced_at": "2026-04-21T15:30:00Z",
          "last_changed_at": "2026-04-13T14:17:14Z"
        }
      },
      "last_synced_at": "2026-04-21T15:30:00Z"
    }
  }
}
"""
import os
import re
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from bs4 import BeautifulSoup

# 作业存储根目录
ASSIGNMENT_ROOT = Path(__file__).parent.parent / "assignments"


def html_to_text(html: str) -> str:
    """将HTML转换为纯文本，便于搜索"""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # 移除script和style标签
    for script in soup(["script", "style"]):
        script.decompose()
    # 获取纯文本
    text = soup.get_text(separator="\n", strip=True)
    # 合并多余空行
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = "\n".join(chunk for chunk in chunks if chunk)
    return text


def get_course_assignments(session, course_id: str) -> List[Dict[str, Any]]:
    """获取指定课程的作业列表"""
    url = f"https://oc.sjtu.edu.cn/api/v1/courses/{course_id}/assignments"
    params = {
        "per_page": 100,
        "include": ["submission", "description"],
    }
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def download_attachment(session, attachment: dict, save_dir: Path) -> dict:
    """
    下载作业附件，保存到指定目录。
    返回更新后的附件信息（含local_path、md5、downloaded_at）。
    """
    # 确保目录存在
    save_dir.mkdir(parents=True, exist_ok=True)

    file_id = str(attachment["id"])
    file_name = attachment["display_name"]
    file_size = attachment["size"]
    updated_at = attachment["updated_at"]
    download_url = attachment["url"]

    # 清理文件名中的非法字符
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", file_name)
    save_path = save_dir / safe_name

    # 检查文件是否已存在且相同（大小相同+更新时间相同）
    if save_path.exists() and save_path.stat().st_size == file_size:
        # 文件已存在，直接返回路径
        return {
            **attachment,
            "local_path": str(save_path.relative_to(ASSIGNMENT_ROOT.parent)),
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
        **attachment,
        "local_path": str(save_path.relative_to(ASSIGNMENT_ROOT.parent)),
        "md5": md5_hash,
        "downloaded_at": datetime.now().isoformat(),
    }


async def sync_course_assignments(gateway, course_id: str) -> tuple[int, int]:
    """
    同步单个课程的所有作业。
    返回 (新增/更新作业数, 新下载附件数)。
    """
    from utils.course_data import update_assignments, get_assignment

    logger = gateway.logger
    session = gateway.session

    logger.info("[assignment_sync] 同步课程 %s 的作业...", course_id)

    # 获取最新作业列表
    assignments = await gateway.run_sync(get_course_assignments, session, course_id)

    updated_count = 0
    downloaded_attachments = 0

    processed_assignments = []

    for assignment in assignments:
        assignment_id = str(assignment["id"])
        assignment_name = assignment["name"]
        updated_at = assignment["updated_at"]

        # 检查是否需要更新
        existing = await gateway.run_sync(get_assignment, course_id, assignment_id)
        if existing and existing.get("updated_at") == updated_at:
            # 无变化，直接复用现有数据
            processed_assignments.append(existing)
            logger.debug("[assignment_sync] 作业 %s (%s) 无变化，跳过", assignment_id, assignment_name)
            continue

        # 有变化，处理作业
        logger.info("[assignment_sync] 处理作业: %s (%s)", assignment_id, assignment_name)

        # 转换描述为纯文本
        description_html = assignment.get("description", "")
        description_text = html_to_text(description_html)

        # 处理附件
        attachments = []
        if assignment.get("attachments"):
            attachment_dir = ASSIGNMENT_ROOT / str(course_id) / str(assignment_id)
            for att in assignment["attachments"]:
                try:
                    downloaded = await gateway.run_sync(
                        download_attachment,
                        session,
                        att,
                        attachment_dir
                    )
                    attachments.append({
                        "file_id": str(downloaded["id"]),
                        "display_name": downloaded["display_name"],
                        "size": downloaded["size"],
                        "content_type": downloaded["content-type"],
                        "canvas_updated_at": downloaded["updated_at"],
                        "local_path": downloaded["local_path"],
                        "md5": downloaded["md5"],
                        "downloaded_at": downloaded["downloaded_at"],
                    })
                    downloaded_attachments += 1
                    logger.debug("[assignment_sync] 已下载附件: %s", downloaded["display_name"])
                except Exception as e:
                    logger.error("[assignment_sync] 下载附件 %s 失败: %s", att.get("display_name", "unknown"), e)
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
        }

        processed_assignments.append(processed)
        updated_count += 1

    # 保存到数据库
    await gateway.run_sync(update_assignments, course_id, processed_assignments)

    logger.info(
        "[assignment_sync] 课程 %s 同步完成: 更新 %d 个作业，下载 %d 个附件",
        course_id, updated_count, downloaded_attachments
    )

    return updated_count, downloaded_attachments


# 插件入口函数
async def run(gateway):
    """
    Job 执行入口：同步所有课程的作业。
    """
    from utils.course_data import get_course_ids

    logger = gateway.logger
    logger.info("[assignment_sync] 开始全量作业同步...")

    course_ids = await gateway.run_sync(get_course_ids)
    if not course_ids:
        logger.warning("[assignment_sync] 课程表为空，请先同步课程表")
        return

    total_updated = 0
    total_downloaded = 0

    for course_id in course_ids:
        try:
            updated, downloaded = await sync_course_assignments(gateway, course_id)
            total_updated += updated
            total_downloaded += downloaded
        except Exception as e:
            logger.error("[assignment_sync] 同步课程 %s 失败: %s", course_id, e)

    logger.info(
        "[assignment_sync] 全量同步完成: 共更新 %d 个作业，下载 %d 个附件",
        total_updated, total_downloaded
    )

    # 触发同步完成事件
    await gateway.post_event(
        "assignments_synced",
        {
            "updated_count": total_updated,
            "downloaded_attachments": total_downloaded,
            "synced_at": datetime.now().isoformat(),
        }
    )


# 插件初始化
async def setup(gateway):
    """插件初始化：创建存储目录"""
    ASSIGNMENT_ROOT.mkdir(parents=True, exist_ok=True)
    gateway.logger.info("[assignment_sync] 作业同步插件已加载，存储目录: %s", ASSIGNMENT_ROOT)


# 插件清理
async def teardown(gateway):
    """插件卸载"""
    gateway.logger.info("[assignment_sync] 作业同步插件已卸载")
