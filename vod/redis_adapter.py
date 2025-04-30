import redis
import json
import os
from datetime import timedelta

# Redis client
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    db=int(os.getenv("REDIS_DB", 0)),
    decode_responses=True
)

# video asset management
REDIS_VIDEO_PREFIX = "vod:playlist"
REDIS_TTL_SECONDS = 2700 # 45 mins

def get_cached_playlist(video_name: str) -> str | None:
    return redis_client.get(f"{REDIS_VIDEO_PREFIX}:{video_name}")

def set_cached_playlist(video_name: str, m3u8_text: str):
    redis_client.setex(f"{REDIS_VIDEO_PREFIX}:{video_name}", timedelta(seconds=REDIS_TTL_SECONDS), m3u8_text)

def invalidate_playlist_cache(video_name: str):
    return redis_client.delete(f"{REDIS_VIDEO_PREFIX}:{video_name}")

# image asset Management
REDIS_IMAGE_PREFIX = "vod:thumbnail"

def get_cached_thumbnail(img_key: str) -> str | None:
    return redis_client.get(f"{REDIS_IMAGE_PREFIX}:{img_key}")

def set_cached_thumbnail(img_key: str, url: str):
    redis_client.setex(f"{REDIS_IMAGE_PREFIX}:{img_key}", timedelta(seconds=REDIS_TTL_SECONDS), url)

def invalidate_thumbnail_cache(img_key: str):
    return redis_client.delete(f"{REDIS_IMAGE_PREFIX}:{img_key}")