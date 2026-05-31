#!/usr/bin/env python3
"""
示例插件: 作业检查
----------------

这是一个 Plugin Job 示例，演示如何编写可动态加载的 Job。

使用方法:
1. 通过 API 上传/注册此插件
2. Gateway 会动态加载并执行
3. 支持所有 Job 操作（暂停、恢复、删除等）

API 调用示例:
    POST /plugins
    {
        "name": "homework_check_example",
        "code": "...",
        "job_type": "basic",
        "auto_start": true
    }
"""
# 插件必须实现的入口函数
async def run(gateway):
    """
    Job 执行入口。

    Args:
        gateway: AutoCanvasGateway 实例
                 可通过它访问 session、logger、job_registry 等
    """
    logger = gateway.logger

    logger.info("[homework_check] 开始检查作业...")

    # 示例: 检查 Canvas 作业
    try:
        session = gateway.session

        # 这里可以调用 Canvas API 获取作业列表
        # 示例代码:
        # response = await gateway.run_sync(
        #     session.get,
        #     "https://oc.sjtu.edu.cn/api/v1/courses/YOUR_COURSE_ID/assignments"
        # )

        logger.info("[homework_check] 作业检查完成")

        # 可以触发事件或发送通知
        # await gateway.post_event("homework_found", {...})

    except Exception as e:
        logger.error("[homework_check] 检查失败: %s", e)
        raise

# 可选: 如果插件需要初始化
async def setup(gateway):
    """插件初始化（可选）"""
    gateway.logger.info("[homework_check] 插件已加载")

# 可选: 清理
async def teardown(gateway):
    """插件卸载（可选）"""
    gateway.logger.info("[homework_check] 插件已卸载")
