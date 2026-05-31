# 登录配置
DEFAULT_USERNAME = "your_jaccount@sjtu.edu.cn"

# Live 直播监听配置
LIVE_CONFIG = {
    "keywords": ["签到", "点名", "名字"],
    "silence_threshold": 0.005,
    "chunk_seconds": 3,
    "sample_rate": 16000,
    "keyword_debounce_seconds": 30,
    "enable_qr_detection": False,
    "qr_debounce_seconds": 60,
}
