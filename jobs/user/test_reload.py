import logging
logger = logging.getLogger(__name__)

async def setup(gateway):
    logger.info("[test_reload] setup called v2")

async def run(gateway):
    logger.info("[test_reload] run v2")

async def teardown(gateway):
    logger.info("[test_reload] teardown called v2")
