"""应用配置：pydantic-settings 从环境变量/.env 读取。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 应用
    APP_ENV: str = "dev"
    APP_NAME: str = "mall-backend"
    APP_DEBUG: bool = True
    TRACE_ID_PREFIX: str = "mall"

    # 安全
    # 开发默认值也满足 HS256 建议的最小 32 字节长度；生产环境仍必须通过
    # 环境变量覆盖为独立随机密钥，禁止继续使用此公开默认值。
    JWT_SECRET: str = "dev-only-change-me-to-a-random-32b-secret"
    JWT_ALGORITHM: str = "HS256"
    # 手机号加密专用密钥；未设置时回退 JWT_SECRET。生产环境必须设置独立随机值。
    PHONE_ENCRYPT_KEY: str = ""
    JWT_ACCESS_EXPIRE: int = 7200
    JWT_REFRESH_EXPIRE: int = 604800
    BCRYPT_ROUNDS: int = 10

    # 数据库
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = "root123"
    DB_NAME: str = "mall"
    DATABASE_URL: str = "mysql+pymysql://root:root123@127.0.0.1:3306/mall?charset=utf8mb4"

    # 文件上传
    UPLOAD_DIR: str = "uploads"
    UPLOAD_MAX_BYTES: int = 5 * 1024 * 1024

    # Redis
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_BLACKLIST_DB: int = 1
    IDEMPOTENCY_TTL: int = 300
    WECOM_FAKE_LOGIN: bool = False

    # 用户小程序微信登录。生产使用微信官方 code2session；本地 fake 默认关闭，
    # 开启后仍只接受 ``fake:<稳定测试标识>``，避免把任意 code 当作身份。
    WECHAT_LOGIN_FAKE_ENABLED: bool = False
    WECHAT_CODE2SESSION_URL: str = "https://api.weixin.qq.com/sns/jscode2session"
    WECHAT_CODE2SESSION_TIMEOUT_SECONDS: float = 5.0
    WECHAT_SECRET_ENCRYPT_KEY: str = ""

    # 单号时区：订单/退款号内嵌日期按该时区计算（业务本地自然日）
    ORDER_NO_TZ: str = "Asia/Shanghai"

    # 积分比率（业务假设：消费 1 元得 1 积分；100 积分抵 1 元）
    # - POINTS_EARN_RATE：实付现金 1 元自动发放的积分数（默认 1）。
    # - POINTS_REDUCE_RATIO：多少积分可抵扣 1 元现金（默认 100）。
    # 积分一律为整数；实付金额折算积分与抵扣金额均向下取整，避免超额发放/超额抵扣。
    POINTS_EARN_RATE: int = 1
    POINTS_REDUCE_RATIO: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
