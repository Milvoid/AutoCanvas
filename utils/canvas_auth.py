#!/usr/bin/env python3
"""
SJTU Canvas 认证模块（jAccount 程序化登录）。
供其他脚本 import 调用，负责：
- jAccount 登录（手动验证码）
- JAAuthCookie 静默刷新
- Session 持久化与有效性校验

三级认证策略:
1. 加载缓存 session → 有效则直接返回
2. 缓存失效 → 用 JAAuthCookie 静默刷新（无需交互）
3. JAAuthCookie 也失效 → 完整 jAccount 登录（需手动输入验证码）
"""
import getpass
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
CANVAS_LOGIN_URL = "https://oc.sjtu.edu.cn/login/canvas"
CANVAS_OPENID_URL = "https://oc.sjtu.edu.cn/login/openid_connect"
JACCOUNT_LOGIN_POST_URL = "https://jaccount.sjtu.edu.cn/jaccount/ulogin"

DEFAULT_SESSION_PATH = Path("canvas_session.json")
MAX_CAPTCHA_RETRIES = 5


def _load_config():
    try:
        from config import DEFAULT_USERNAME
        return DEFAULT_USERNAME
    except Exception:
        return ""


def _prompt_credentials():
    """交互式读取用户名密码。"""
    default_username = _load_config()
    if default_username:
        username_input = input(f"jAccount 用户名（回车使用默认 {default_username}）: ").strip()
        username = username_input if username_input else default_username
    else:
        username = input("jAccount 用户名（邮箱前缀或完整邮箱）: ").strip()
    if not username:
        raise ValueError("用户名不能为空")
    password = getpass.getpass("jAccount 密码: ").strip()
    if not password:
        raise ValueError("密码不能为空")
    return username, password


# ---------------------------------------------------------------------------
# Session 工具
# ---------------------------------------------------------------------------
def create_session() -> requests.Session:
    """创建一个预置合理 headers 和连接池的 requests.Session。"""
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
    from requests.adapters import HTTPAdapter
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=3)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def save_session(session: requests.Session, path: Path = DEFAULT_SESSION_PATH) -> None:
    """把 Session cookies 导出为 JSON。"""
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
    ]
    path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")


def load_session(path: Path = DEFAULT_SESSION_PATH) -> Optional[requests.Session]:
    """从 JSON 文件恢复 Session cookies；文件不存在则返回 None。"""
    if not path.exists():
        return None
    session = create_session()
    try:
        for item in json.loads(path.read_text(encoding="utf-8")):
            session.cookies.set(
                item["name"],
                item["value"],
                domain=item.get("domain"),
                path=item.get("path", "/"),
            )
    except Exception:
        return None
    return session


def is_session_valid(session: requests.Session) -> bool:
    """
    快速校验 Canvas Session 是否仍有效。
    访问 dashboard_cards API，返回 200 则认为有效。
    """
    try:
        with session.get(
            "https://oc.sjtu.edu.cn/api/v1/dashboard/dashboard_cards",
            timeout=15,
            stream=True,
        ) as resp:
            _ = resp.content
            return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# jAccount 登录内部工具
# ---------------------------------------------------------------------------
def _dedup_session_cookies(session: requests.Session) -> None:
    """去重同 domain+name 的 cookie，只保留最后一个。"""
    seen = {}
    for c in session.cookies:
        key = (c.domain, c.name)
        seen[key] = c

    jar = session.cookies
    jar.clear()
    for c in seen.values():
        jar.set_cookie(c)


def _get_jaccount_login_page(
    session: requests.Session,
) -> Tuple[str, str]:
    """访问 Canvas → 跟重定向到 jAccount 登录页，返回 (login_url, html)。"""
    session.get(CANVAS_LOGIN_URL, allow_redirects=True, timeout=30)
    resp = session.get(CANVAS_OPENID_URL, allow_redirects=True, timeout=30)
    logger.debug("[auth] jAccount 登录页: %s (status=%d)", resp.url[:80], resp.status_code)
    return resp.url, resp.text


def _parse_login_form(
    html: str, url: str,
) -> Tuple[dict, str, bool]:
    """
    解析 jAccount 登录页的表单字段和 uuid。
    返回 (merged_params, uuid_val, has_captcha)。
    """
    soup = BeautifulSoup(html, "html.parser")

    # 从 JS loginContext 提取参数
    js_params = {}
    for key in ("sid", "client", "returl", "se", "v", "uuid"):
        match = re.search(rf'{key}\s*:\s*"([^"]*)"', html)
        if match:
            js_params[key] = match.group(1)

    # URL query string 作为 fallback
    parsed = urlparse(url)
    url_params = {}
    for key, vals in parse_qs(parsed.query).items():
        url_params[key] = vals[0]

    merged_params = {**url_params, **js_params}

    # 提取 uuid
    uuid_val = js_params.get("uuid") or url_params.get("uuid")
    if not uuid_val:
        match = re.search(r'captcha\?uuid=([0-9a-f-]+)', html)
        if match:
            uuid_val = match.group(1)

    has_captcha = bool(soup.find(id="captcha-img") or re.search(r'captcha', html, re.I))
    return merged_params, uuid_val, has_captcha


def _display_captcha_inline(image_data: bytes) -> bool:
    """尝试用 iTerm2 Inline Images 协议在终端内显示验证码。返回是否成功。"""
    import base64
    b64 = base64.b64encode(image_data).decode("ascii")
    osc = f"\033]1337;File=inline=1;width=30:{b64}\a"

    try:
        if os.environ.get("TMUX"):
            # tmux 需要 passthrough 转义
            sys.stdout.write(f"\033Ptmux;\033{osc}\033\\")
        else:
            sys.stdout.write(osc)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return True
    except Exception:
        return False


def _prompt_captcha(
    session: requests.Session,
    uuid_val: str,
    login_url: str,
    attempt: int = 0,
) -> str:
    """下载验证码并展示给用户输入。"""
    _dedup_session_cookies(session)

    captcha_url = (
        f"https://jaccount.sjtu.edu.cn/jaccount/captcha"
        f"?uuid={uuid_val}&t={int(time.time() * 1000)}"
    )
    resp = session.get(
        captcha_url,
        timeout=15,
        headers={
            "Referer": login_url,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    if resp.status_code != 200 or len(resp.content) < 100:
        raise RuntimeError(f"验证码下载失败: status={resp.status_code}, size={len(resp.content)}")

    # 保存到 data/ 目录
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    captcha_path = os.path.join(data_dir, "captcha.jpg")
    with open(captcha_path, "wb") as f:
        f.write(resp.content)

    # 优先终端内联显示，失败则 open + 提示路径
    if not _display_captcha_inline(resp.content):
        subprocess.Popen(
            ["open", captcha_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(f"  验证码已保存到: {os.path.abspath(captcha_path)}")

    captcha_text = input("  请输入验证码: ").strip()
    if not captcha_text:
        raise RuntimeError("验证码输入为空")
    return captcha_text


def _submit_login(
    session: requests.Session,
    url_params: dict,
    uuid_val: str,
    username: str,
    password: str,
    captcha: str,
    login_url: str,
) -> Tuple[requests.Response, Optional[dict]]:
    """
    AJAX 方式提交登录表单。
    返回 (response, login_result_json_or_none)。
    """
    post_data = {
        "sid": url_params.get("sid", ""),
        "client": url_params.get("client", ""),
        "returl": url_params.get("returl", ""),
        "se": url_params.get("se", ""),
        "v": url_params.get("v", ""),
        "uuid": uuid_val or "",
        "user": username,
        "pass": password,
        "captcha": captcha,
        "lt": "p",
    }

    resp = session.post(
        JACCOUNT_LOGIN_POST_URL,
        data=post_data,
        allow_redirects=False,
        timeout=30,
        headers={
            "Referer": login_url,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://jaccount.sjtu.edu.cn",
        },
    )

    result = None
    try:
        result = resp.json()
    except ValueError:
        pass

    # errno==0: 登录成功，跟随回调拿 Canvas session
    if result and result.get("errno") == 0 and result.get("url"):
        redirect_url = result["url"]
        if redirect_url.startswith("/"):
            redirect_url = "https://jaccount.sjtu.edu.cn" + redirect_url
        logger.info("[auth] 登录成功，跟随回调...")
        resp = session.get(redirect_url, allow_redirects=True, timeout=30)

    return resp, result


def _jaccount_login(session: requests.Session) -> requests.Session:
    """
    完整 jAccount 登录流程（含手动验证码输入）。
    成功后 session 中同时包含 Canvas cookies 和 JAAuthCookie。
    """
    print("-" * 50)
    print("需要登录 SJTU Canvas (jAccount)")
    print("-" * 50)

    username, password = _prompt_credentials()

    login_url, login_html = _get_jaccount_login_page(session)

    if "jaccount.sjtu.edu.cn" not in login_url:
        logger.info("[auth] 已有登录态，无需重新登录")
        return session

    url_params, uuid_val, _ = _parse_login_form(login_html, login_url)
    if not uuid_val:
        raise RuntimeError("无法从登录页提取 uuid")

    for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
        print(f"\n第 {attempt}/{MAX_CAPTCHA_RETRIES} 次尝试")

        try:
            captcha_text = _prompt_captcha(session, uuid_val, login_url, attempt)
        except Exception as e:
            logger.warning("[auth] 验证码获取失败: %s", e)
            continue

        resp, result = _submit_login(
            session, url_params, uuid_val,
            username, password, captcha_text, login_url,
        )

        if result is None:
            raise RuntimeError(f"登录响应异常（非 JSON）: {resp.text[:200]}")

        errno = result.get("errno")
        error_msg = result.get("error", "")

        if errno == 0:
            # 成功
            if is_session_valid(session):
                print("登录成功!")
                return session
            raise RuntimeError("登录回调完成但 Canvas session 校验失败")

        logger.warning("[auth] 登录失败: errno=%s, error=%s", errno, error_msg)

        if "验证码" in error_msg or errno == 1:
            # 验证码错误，刷新登录页重试
            login_url, login_html = _get_jaccount_login_page(session)
            if "jaccount.sjtu.edu.cn" not in login_url:
                return session  # 已经登录了
            url_params, uuid_val, _ = _parse_login_form(login_html, login_url)
            if not uuid_val:
                raise RuntimeError("重试时无法提取 uuid")
            continue

        # 非验证码错误（密码错等），不重试
        raise RuntimeError(f"jAccount 登录失败: {error_msg}")

    raise RuntimeError(f"验证码重试 {MAX_CAPTCHA_RETRIES} 次均失败")


def _refresh_with_jacookie(session: requests.Session) -> Optional[requests.Session]:
    """
    用已有 JAAuthCookie 静默刷新 Canvas session。
    成功返回新 session（含 Canvas cookies + JAAuthCookie），失败返回 None。
    """
    ja_cookie = None
    for c in session.cookies:
        if c.name == "JAAuthCookie" and "jaccount" in (c.domain or ""):
            ja_cookie = c
            break

    if not ja_cookie:
        logger.debug("[auth] 无 JAAuthCookie，跳过静默刷新")
        return None

    logger.info("[auth] 尝试 JAAuthCookie 静默刷新...")
    fresh = create_session()
    fresh.cookies.set(
        ja_cookie.name,
        ja_cookie.value,
        domain=ja_cookie.domain,
        path=ja_cookie.path,
    )

    try:
        fresh.get(CANVAS_LOGIN_URL, allow_redirects=True, timeout=30)
        resp = fresh.get(CANVAS_OPENID_URL, allow_redirects=True, timeout=30)

        if "oc.sjtu.edu.cn" in resp.url and is_session_valid(fresh):
            logger.info("[auth] JAAuthCookie 刷新成功")
            return fresh

        logger.info("[auth] JAAuthCookie 刷新失败（落地 %s）", resp.url[:60])
    except Exception as exc:
        logger.warning("[auth] JAAuthCookie 刷新异常: %s", exc)

    return None


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------
def ensure_session(
    session_path: Path = DEFAULT_SESSION_PATH,
    auto_prompt: bool = True,
) -> requests.Session:
    """
    确保返回一个有效的 Canvas Session。

    三级认证:
    1. 加载缓存 session → 有效则直接返回
    2. 缓存失效 → JAAuthCookie 静默刷新（无需交互）
    3. JAAuthCookie 也失效 → 完整 jAccount 登录（需手动输入验证码）

    auto_prompt=False 时跳过第三级，直接抛异常。
    """
    # Tier 1: 加载缓存
    session = load_session(session_path)
    if session and is_session_valid(session):
        logger.debug("[auth] 缓存 session 有效")
        return session

    # Tier 2: JAAuthCookie 静默刷新
    if session:
        refreshed = _refresh_with_jacookie(session)
        if refreshed:
            save_session(refreshed, session_path)
            return refreshed

    # Tier 3: 完整 jAccount 登录
    if not auto_prompt:
        raise RuntimeError("Canvas Session 失效，JAAuthCookie 刷新也失败，需要手动登录")

    logger.info("[auth] 启动 jAccount 登录...")
    session = create_session()
    session = _jaccount_login(session)
    save_session(session, session_path)
    logger.info("[auth] Session 已保存到 %s", session_path)
    return session


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        session = ensure_session()
        dash = session.get(
            "https://oc.sjtu.edu.cn/api/v1/dashboard/dashboard_cards",
            timeout=15,
        )
        print(f"dashboard_cards: {dash.status_code}")
        print("登录完成。")
    except Exception as exc:
        print(f"失败: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
