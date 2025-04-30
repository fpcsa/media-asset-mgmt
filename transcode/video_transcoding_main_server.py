from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv
from minio import Minio
import os, requests, logging, uuid, shutil, ffmpeg, imageio_ffmpeg

# === Config ===
load_dotenv()

# Env variables
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT")
MINIO_USR = os.getenv("MINIO_USR")
MINIO_PWD = os.getenv("MINIO_PWD")
MINIO_BUCKET_VOD = os.getenv("MINIO_BUCKET_VOD")
CACHE_INVALIDATION_URL = os.getenv("CACHE_INVALIDATION_URL")
TRANSCODE_API_KEY = os.getenv("TRANSCODE_API_KEY")

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# Create MinIO client with access and secret key.
client_minio = Minio(
    endpoint=MINIO_ENDPOINT, 
    access_key=MINIO_USR, 
    secret_key=MINIO_PWD,
    secure=False
    )

# FastAPI
app = FastAPI()

app = FastAPI(
    title="Transcode Main Server",
    description="This is a FastAPI server that allows Video Transcoding and Remuxing.",
    version="1.0.0"
)

# Allow API to be called from Web App
origins = ['null']         
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request model
class TranscodeRequest(BaseModel):
    asset_bucket: str
    asset_object: str
    reencode: bool = False

class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None

# === Helpers ===

def download_file(asset_bucket: str, asset_object: str) -> tuple[Path, Path]:

    # Get data of an object in MinIO
    try:
        # Download data of an object
        unique_id = uuid.uuid4().hex[:6]
        tmp_data_folder = Path(f"tmp_{unique_id}")
        tmp_data_folder.mkdir(parents=True, exist_ok=True)

        tmp_data = tmp_data_folder / asset_object
        client_minio.fget_object(asset_bucket, asset_object, tmp_data)
        tmp_data.parent.mkdir(parents=True, exist_ok=True)  # In case asset_object has subdirs

        client_minio.fget_object(asset_bucket, asset_object, str(tmp_data))

        return tmp_data_folder, tmp_data

    except Exception as e:
        # If there's an error, delete the folder if it exists
        if tmp_data_folder.exists():
            shutil.rmtree(tmp_data_folder)
        log.error(f"Error downloading object from MinIO: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(error="Error downloading object from MinIO", detail=str(e)).model_dump()
        )   
    
def convert_to_hls(input_file: Path, output_dir: Path, reencode: bool = False):
    """
    Converts video for HTTP Live Streaming
    """
    # output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "index.m3u8"

    ffmpeg_exec = imageio_ffmpeg.get_ffmpeg_exe()

    # log.info(f"Using ffmpeg binary at: {ffmpeg_exec}")

    try:
        if reencode:
            log.info("Re-encoding to H.264 + AAC...")
            """
            stream = (
                ffmpeg
                .input(str(input_file))
                .output(
                    str(output_path),
                    format='hls',
                    hls_time=10,
                    hls_list_size=0,
                    c_v='libx264',
                    preset='veryfast',
                    crf=23,
                    c_a='aac',
                    b_a='128k'
                )
            )
            """
            stream = (
                ffmpeg
                .input(str(input_file))
                .output(
                    str(output_path),
                    format='hls',
                    hls_time=10,
                    hls_list_size=0,
                    vcodec='libx264',
                    preset='veryfast',
                    crf=23,
                    acodec='aac',
                    **{'b:a': '128k'}
                )
            )

        else:
            log.info("Remuxing without re-encoding...")
            stream = (
                ffmpeg
                .input(str(input_file))
                .output(
                    str(output_path),
                    format='hls',
                    hls_time=10,
                    hls_list_size=0,
                    codec='copy',
                    start_number=0
                )
            )

        # Run the stream using the bundled ffmpeg binary
        # ffmpeg.run(stream, cmd=ffmpeg_exec, capture_stdout=True, capture_stderr=True) # To avoid logging transcoding output
        ffmpeg.run(stream, cmd=ffmpeg_exec)
        log.info("FFmpeg conversion complete.")

    except ffmpeg.Error as e:
        err_msg = e.stderr.decode() if e.stderr else str(e)
        log.error(f"FFmpeg failed:\n{err_msg}")
        raise RuntimeError(f"FFmpeg error: {err_msg}")

def upload_file(local_file: Path, bucket_name: str, object_name: str):
    """
    Uploads single file in MinIO
    """
    try:
        client_minio.fput_object(
            bucket_name,
            object_name,
            str(local_file),
            content_type="application/octet-stream"
        )
        log.info(f"Uploaded {local_file} to minio://{bucket_name}/{object_name}")
        return object_name

    except Exception as e:
        log.error(f"Error uploading {local_file} to MinIO: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(error="Error uploading file to MinIO", detail=str(e)).model_dump()
        )

def upload_folder(local_dir: Path, bucket_name: str, object_prefix: str):
    """
    Uploads folders in MinIO 
    e.g. playlist .m3u8 and .ts files
    """
    for file in local_dir.rglob("*"):
        if file.is_file():
            relative_path = file.relative_to(local_dir)
            object_name = f"{object_prefix}/{relative_path}".replace("\\", "/")  # cross-platform
            upload_file(file, bucket_name, object_name)

# === API Endpoint ===

@app.post("/transcode")
def transcode_video(request: TranscodeRequest, x_api_key: str = Header(None)):
    if x_api_key != TRANSCODE_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    video_name = Path(request.asset_object).stem

    try:
        log.info(f"Starting transcoding for: {video_name}")

        tmp_video_folder, tmp_video_data = download_file(asset_bucket=request.asset_bucket, asset_object=request.asset_object)

        local_hls = tmp_video_folder / video_name
        local_hls.mkdir(parents=True, exist_ok=True)  # Create output HLS folder

        convert_to_hls(input_file=tmp_video_data, output_dir=local_hls, reencode=request.reencode)

        # upload_file(local_file=local_hls, bucket_name=MINIO_BUCKET_VOD, object_name=video_name)
        upload_folder(local_dir=local_hls, bucket_name=MINIO_BUCKET_VOD, object_prefix=video_name)

        # invalidate_cache(video_name)
        # local_mp4.unlink(missing_ok=True)

        log.info(f"Transcoding completed: {video_name}")
        return JSONResponse(status_code=200, content={"status": "success", "video": video_name})
    except Exception as e:
        log.error(f"Error during transcoding: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Delete the unique tmp directory and its contents
        if tmp_video_folder.exists():
            shutil.rmtree(tmp_video_folder)