from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio
from minio.deleteobjects import DeleteObject
from datetime import timedelta
from dotenv import load_dotenv
import os, logging, time, requests
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from redis_adapter import get_cached_playlist, set_cached_playlist, invalidate_playlist_cache, get_cached_thumbnail, set_cached_thumbnail, invalidate_thumbnail_cache

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# MinIO Config
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT")
MINIO_PUBLIC_HOST = os.getenv("MINIO_PUBLIC_HOST")
MINIO_USR = os.getenv("MINIO_USR")
MINIO_PWD = os.getenv("MINIO_PWD")
MINIO_BUCKET_VOD = os.getenv("MINIO_BUCKET_VOD")

# MinIO Client
client_minio = Minio(
    endpoint=MINIO_ENDPOINT.replace("http://", "").replace("https://", ""),
    access_key=MINIO_USR,
    secret_key=MINIO_PWD,
    # secure=MINIO_ENDPOINT.startswith("https")
    secure= False
)

# To allow .ts endpoints to be reachable by browser and video streaming libraries
client_minio_public = Minio(
    endpoint=MINIO_PUBLIC_HOST.replace("http://", "").replace("https://", ""),
    access_key=MINIO_USR,
    secret_key=MINIO_PWD,
    secure=False
)

TRANSCODE_API_KEY = os.getenv("TRANSCODE_API_KEY")
TRANSCODE_API_URL = os.getenv("TRANSCODE_API_URL")


# Rewrite signed .ts url to be reached from browser by using nginx reverse-proxy host
def replace_signed_url_host(signed_url: str, public_host: str) -> str:
    parsed = urlparse(signed_url)
    return urlunparse(parsed._replace(netloc=public_host))

def auto_transcode(video_bucket: str, video_path: str, reencode: bool = False) -> bool:
    """
    To allow "lazy-transcoding" or "on-demand processing"
    Useful to do transcoding if a video requested is not already transcoded
    """
    try:
        payload = {
            "asset_bucket": video_bucket,
            "asset_object": video_path,
            "reencode": reencode
        }
        headers = {"x-api-key": TRANSCODE_API_KEY}
        r = requests.post(TRANSCODE_API_URL, json=payload, headers=headers)
        r.raise_for_status()
        return True
    except Exception as e:
        log.info(f"Auto-transcoding failed: {e}")
        return False
    
# FastAPI App
app = FastAPI(
    title="VOD Main Server",
    description="This is a FastAPI server that allows Video On Demand by using HLS (HTTP Live Streaming).",
    version="1.0.0"
)

# Allow API to be called from Web App
# origins = ['null']

ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/video/{video_name}/playlist.m3u8")
def serve_signed_playlist(video_name: str):
    """
    Gets .m3u8 playlist file from a specific bucket in MinIO
    """
    # Try to return from Redis cache
    cached = get_cached_playlist(video_name)
    if cached:
        log.info(f"cache:hit")
        return PlainTextResponse(content=cached, media_type="application/vnd.apple.mpegurl")

    # Fetch .m3u8 playlist from MinIO
    playlist_key = f"{video_name}/index.m3u8"
    try:
        response = client_minio.get_object(MINIO_BUCKET_VOD, playlist_key)
        m3u8_text = response.read().decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Cannot fetch playlist: {e}")

    # Rewrite .ts segment lines with signed URLs
    signed_lines = []
    for line in m3u8_text.splitlines():
        if line.strip().endswith(".ts"):
            ts_key = f"{video_name}/{line.strip()}"
            try:
                signed_url = client_minio.presigned_get_object(
                    MINIO_BUCKET_VOD,
                    ts_key,
                    expires=timedelta(seconds=3600)  # 1 hour signed URL
                )
                signed_url = replace_signed_url_host(signed_url, MINIO_PUBLIC_HOST)
                signed_lines.append(signed_url)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Error signing {ts_key}: {e}")
        else:
            signed_lines.append(line)

    # Build and cache the final playlist
    final_m3u8 = "\n".join(signed_lines)
    set_cached_playlist(video_name, final_m3u8)
    log.info(f"cache:set for {video_name}")

    return PlainTextResponse(content=final_m3u8, media_type="application/vnd.apple.mpegurl")

@app.get("/stream/{video_bucket}/{video_path:path}/playlist.m3u8")
def serve_signed_playlist(video_bucket: str, video_path: str, reencode: bool = Query(False, description="Whether to re-encode the video to H.264 + AAC")):
    """
    Given video_bucket and video_path
    If any, gets .m3u8 playlist file from a specific bucket in MinIO
    Otherwise, fetches the video itself from the path, does lazy-transcoding, and gets .m3u8 playlist file
    """
    video_name = Path(video_path).stem
    playlist_key = f"{video_name}/index.m3u8"

    # Try cache first
    cached = get_cached_playlist(video_name)
    if cached:
        log.info(f"cache:hit for {video_name}")
        return PlainTextResponse(content=cached, media_type="application/vnd.apple.mpegurl")

    # Try to fetch the playlist
    try:
        response = client_minio.get_object(MINIO_BUCKET_VOD, playlist_key)
        m3u8_text = response.read().decode("utf-8")
    except Exception:
        log.warning(f"Playlist missing, triggering lazy transcode for {video_path}")
        try:
            if not auto_transcode(video_bucket, video_path, reencode):
                raise HTTPException(status_code=500, detail="Auto-transcoding failed")

            # Poll MinIO for the .m3u8 playlist file for up to 10 seconds,
            # waiting for the transcoding process to finish and write the file.
            # If the file isn't available after 10 attempts, raise a 504 error.
            for _ in range(10):
                try:
                    response = client_minio.get_object(MINIO_BUCKET_VOD, playlist_key)
                    m3u8_text = response.read().decode("utf-8")
                    break
                except:
                    time.sleep(1)
            else:
                raise HTTPException(status_code=504, detail="Playlist still not available after transcoding")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Transcoding exception: {e}")

    # Sign .ts segments
    signed_lines = []
    for line in m3u8_text.splitlines():
        if line.strip().endswith(".ts"):
            ts_key = f"{video_name}/{line.strip()}"
            try:
                signed_url = client_minio.presigned_get_object(
                    MINIO_BUCKET_VOD, ts_key, expires=timedelta(seconds=3600)
                )
                signed_url = replace_signed_url_host(signed_url, MINIO_PUBLIC_HOST)
                signed_lines.append(signed_url)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Error signing {ts_key}: {e}")
        else:
            signed_lines.append(line)

    final_m3u8 = "\n".join(signed_lines)
    set_cached_playlist(video_name, final_m3u8)
    log.info(f"cache:set for {video_name}")
    
    return PlainTextResponse(content=final_m3u8, media_type="application/vnd.apple.mpegurl")

@app.delete("/cache/video/{video_name}")
def delete_cache_video(video_name: str, x_api_key: str = Header(None)):
    if x_api_key != TRANSCODE_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    if invalidate_playlist_cache(video_name):
        log.info(f"cache:delete for {video_name}")
        return {"message": f"Cache cleared for '{video_name}'"}
    else:
        log.info(f"cache:error for {video_name}")
        return {"message": f"Cache error for '{video_name}'"}

@app.delete("/stream/{video_bucket}/{video_path:path}/playlist.m3u8")
def delete_stream_video(video_bucket: str, video_path: str, x_api_key: str = Header(None)):
    if x_api_key != TRANSCODE_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        video_name = Path(video_path).stem
        playlist_folder = f"{video_name}"

        # Invalidate cache
        if invalidate_playlist_cache(video_name):
            log.info(f"cache:delete for {video_name}")
            cache_response = {"message": f"Cache cleared for '{video_name}'"}
        else:
            log.info(f"cache:error for {video_name}")
            cache_response = {"message": f"Cache error for '{video_name}'"}

        # List all objects under the playlist folder
        objects_to_delete = [
            obj.object_name
            for obj in client_minio.list_objects(video_bucket, prefix=playlist_folder, recursive=True)
        ]

        if not objects_to_delete:
            raise HTTPException(status_code=404, detail=f"No files found in '{video_bucket}/{playlist_folder}'")

        # Convert object names to DeleteObject instances
        delete_object_list = [DeleteObject(obj_name) for obj_name in objects_to_delete]

        # Perform deletion
        delete_result = client_minio.remove_objects(
            bucket_name=video_bucket,
            delete_object_list=delete_object_list
        )

        # Collect any deletion errors
        errors = []
        for del_err in delete_result:
            errors.append(f"{del_err.object_name}: {del_err.error}")

        if errors:
            log.error(f"Deletion errors in MinIO: {errors}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete one or more files: {errors}"
            )

        log.info(f"stream:deleted {playlist_folder} in {video_bucket}")

        return {
            "delete_message": f"Stream '{video_bucket}/{playlist_folder}' deleted",
            "cache_message": cache_response["message"]
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        log.error(f"stream:delete error for {video_bucket}/{video_path}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Exception: {str(e)}")


@app.get("/asset/{bucket_name}/{img_path:path}/thumbnail")
def serve_signed_thumbnail(bucket_name: str, img_path: str):
    """
    Serves a signed URL to an image thumbnail stored in MinIO,
    with Redis caching to reduce MinIO access.
    """
    # Use the full path as the unique Redis key
    img_redis_key = f"{bucket_name}/{img_path}"

    # Try to get from cache
    cached_url = get_cached_playlist(img_redis_key)
    if cached_url:
        log.info(f"cache:hit for {img_redis_key}")
        return PlainTextResponse(content=cached_url, media_type="text/plain")

    try:
        # Generate signed URL
        signed_url = client_minio.presigned_get_object(
            bucket_name,
            img_path,
            expires=timedelta(hours=1)
        )
        signed_url = replace_signed_url_host(signed_url, MINIO_PUBLIC_HOST)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Error getting signed thumbnail: {e}")

    # Cache it
    set_cached_playlist(img_redis_key, signed_url)

    return PlainTextResponse(content=signed_url, media_type="text/plain")

@app.get("/stream/{bucket_name}/{img_path:path}/thumbnail")
def stream_thumbnail_image(bucket_name: str, img_path: str):
    """
    Streams the actual thumbnail image using a cached MinIO signed URL.
    """
    img_redis_key = f"{bucket_name}/{img_path}"

    # Get signed URL from cache (or generate and cache it)
    signed_url = get_cached_thumbnail(img_redis_key)
    if not signed_url:
        try:
            signed_url = client_minio.presigned_get_object(
                bucket_name,
                img_path,
                expires=timedelta(hours=1)
            )
            signed_url = replace_signed_url_host(signed_url, MINIO_PUBLIC_HOST)
            set_cached_thumbnail(img_redis_key, signed_url)
            log.info(f"cache:set for {img_redis_key}")
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Error generating signed URL: {e}")
    else:
        log.info(f"cache:hit for {img_redis_key}")

    # Use the signed URL to fetch the image content
    try:
        r = requests.get(signed_url, stream=True)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch image from MinIO: {e}")

    # Detect MIME type or default to image/jpeg
    content_type = r.headers.get("Content-Type", "image/jpeg")

    return StreamingResponse(r.raw, media_type=content_type)

@app.delete("/cache/img/{img_path:path}")
def delete_cache_img(img_path: str, x_api_key: str = Header(None)):
    if x_api_key != TRANSCODE_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    if invalidate_thumbnail_cache(img_path):
        log.info(f"cache:delete for {img_path}")
        return {"message": f"Cache cleared for '{img_path}'"}
    else:
        log.info(f"cache:error for {img_path}")
        return {"message": f"Cache error for '{img_path}'"}
