#!/usr/bin/env python3
"""列出所有Canvas课程"""
import requests
from utils.canvas_auth import ensure_session

CANVAS_BASE = "https://oc.sjtu.edu.cn"

def list_courses(session: requests.Session):
    courses = []
    url = f"{CANVAS_BASE}/api/v1/courses"
    params = {"per_page": 100, "include[]": "term"}

    while url:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list):
            raise RuntimeError(f"非预期返回: {page}")
        courses.extend(page)

        # 分页处理
        next_url = None
        link_hdr = resp.headers.get("Link", "")
        for part in link_hdr.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
                break
        url = next_url
        params = None

    return courses

if __name__ == "__main__":
    session = ensure_session(auto_prompt=False)
    courses = list_courses(session)

    print("当前学期课程列表:")
    print("-" * 80)
    for course in courses:
        name = course.get("name", "")
        course_code = course.get("course_code", "")
        course_id = course.get("id", "")
        term = course.get("term", {}).get("name", "")
        print(f"ID: {course_id:>6} | {name} ({course_code}) | 学期: {term}")
