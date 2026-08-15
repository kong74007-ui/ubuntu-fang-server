# Copyright (C) 2025 AIDC-AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Video generation endpoints

Supports both synchronous and asynchronous video generation.
"""

import os
import uuid
from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from api.dependencies import PixelleVideoDep
from api.schemas.video import (
    VideoGenerateRequest,
    VideoGenerateResponse,
    VideoGenerateAsyncResponse,
)
from api.tasks import task_manager, TaskType
from api.task_capacity import TaskQueueFullError, video_task_capacity
from api import external_audio

router = APIRouter(prefix="/video", tags=["Video Generation"])


def _lease_external_segments(request_body: VideoGenerateRequest, task_id: str):
    segments = request_body.narration_segments or []
    if not segments:
        return [], []
    try:
        asset_ids, resolved = external_audio.lease_narration_segments(segments, task_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="narration audio asset not found") from exc
    except external_audio.AudioLeaseError as exc:
        raise HTTPException(status_code=409, detail="narration audio asset is already in use") from exc
    return asset_ids, resolved


def path_to_url(request: Request, file_path: str) -> str:
    """
    Convert file path to accessible URL
    
    Handles both absolute and relative paths, extracting the path relative
    to the output directory for URL construction.
    
    Args:
        request: FastAPI Request object (provides base_url from actual request)
        file_path: Absolute or relative file path
    
    Returns:
        Full URL to access the file
    
    Examples:
        Windows: G:\\...\\output\\20251205_233630_c939\\final.mp4
              -> http://localhost:8000/api/files/20251205_233630_c939/final.mp4
        
        Linux:   /home/user/.../output/20251205_233630_c939/final.mp4
              -> http://localhost:8000/api/files/20251205_233630_c939/final.mp4
        
        Domain:  With domain request -> https://your-domain.com/api/files/...
    """
    from pathlib import Path
    import os
    
    # Normalize path separators to forward slashes first (for cross-platform compatibility)
    file_path = file_path.replace("\\", "/")
    
    # Check if it's an absolute path (works for both Windows and Linux)
    is_absolute = os.path.isabs(file_path) or Path(file_path).is_absolute()
    
    if is_absolute:
        # Find "output" in the path and get everything after it
        # Split by / to work with normalized paths
        parts = file_path.split("/")
        try:
            output_idx = parts.index("output")
            # Get all parts after "output" and join them
            relative_parts = parts[output_idx + 1:]
            file_path = "/".join(relative_parts)
        except ValueError:
            # If "output" not in path, use the filename only
            file_path = Path(file_path).name
    else:
        # If relative path starting with "output/", remove it
        if file_path.startswith("output/"):
            file_path = file_path[7:]  # Remove "output/"
    
    # Build URL using request's base_url (automatically matches the request host)
    base_url = str(request.base_url).rstrip('/')
    return f"{base_url}/api/files/{file_path}"


@router.post("/generate/sync", response_model=VideoGenerateResponse)
async def generate_video_sync(
    request_body: VideoGenerateRequest,
    pixelle_video: PixelleVideoDep,
    request: Request
):
    """
    Generate video synchronously
    
    This endpoint blocks until video generation is complete.
    Suitable for small videos (< 30 seconds).
    
    **Note**: May timeout for large videos. Use `/generate/async` instead.
    
    Request body includes all video generation parameters.
    See VideoGenerateRequest schema for details.
    
    Returns path to generated video, duration, and file size.
    """
    asset_ids = []
    try:
        logger.info(f"Sync video generation: {request_body.text[:50]}...")
        
        # Auto-determine media_width and media_height from template meta tags (required)
        if not request_body.frame_template:
            raise ValueError("frame_template is required to determine media size")
        
        from pixelle_video.services.frame_html import HTMLFrameGenerator
        from pixelle_video.utils.template_util import resolve_template_path
        template_path = resolve_template_path(request_body.frame_template)
        generator = HTMLFrameGenerator(template_path)
        media_width, media_height = generator.get_media_size()
        logger.debug(f"Auto-determined media size from template: {media_width}x{media_height}")
        
        # Build video generation parameters
        video_params = {
            "text": request_body.text,
            "mode": request_body.mode,
            "title": request_body.title,
            "n_scenes": request_body.n_scenes,
            "min_narration_words": request_body.min_narration_words,
            "max_narration_words": request_body.max_narration_words,
            "min_image_prompt_words": request_body.min_image_prompt_words,
            "max_image_prompt_words": request_body.max_image_prompt_words,
            "media_width": media_width,
            "media_height": media_height,
            "media_workflow": request_body.media_workflow,
            "video_fps": request_body.video_fps,
            "frame_template": request_body.frame_template,
            "prompt_prefix": request_body.prompt_prefix,
            "bgm_path": request_body.bgm_path,
            "bgm_volume": request_body.bgm_volume,
        }
        asset_ids, external_segments = _lease_external_segments(
            request_body, f"sync-{uuid.uuid4().hex}"
        )
        if request_body.tts_speed is not None:
            video_params["tts_speed"] = request_body.tts_speed
        if external_segments:
            video_params["external_narration_segments"] = external_segments
        
        # Add TTS workflow if specified
        if request_body.tts_workflow:
            video_params["tts_workflow"] = request_body.tts_workflow
        
        # Add ref_audio if specified
        if request_body.ref_audio:
            video_params["ref_audio"] = request_body.ref_audio
        
        # Legacy voice_id support (deprecated)
        if request_body.voice_id:
            logger.warning("voice_id parameter is deprecated, please use tts_workflow instead")
            video_params["voice_id"] = request_body.voice_id
        
        # Add custom template parameters if specified
        if request_body.template_params:
            video_params["template_params"] = request_body.template_params
        
        # Call video generator service
        async with video_task_capacity.slot():
            result = await pixelle_video.generate_video(**video_params)
        
        # Get file size
        file_size = os.path.getsize(result.video_path) if os.path.exists(result.video_path) else 0
        
        # Convert path to URL
        video_url = path_to_url(request, result.video_path)
        
        return VideoGenerateResponse(
            video_url=video_url,
            duration=result.duration,
            file_size=file_size
        )
        
    except TaskQueueFullError as e:
        logger.warning("Sync video task queue is full")
        raise HTTPException(
            status_code=429,
            detail={"code": e.code, "message": "Video task queue is full"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Sync video generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if asset_ids:
            external_audio.release_audio_assets(asset_ids)


@router.post("/generate/async", response_model=VideoGenerateAsyncResponse)
async def generate_video_async(
    request_body: VideoGenerateRequest,
    pixelle_video: PixelleVideoDep,
    request: Request
):
    """
    Generate video asynchronously
    
    Creates a background task for video generation.
    Returns immediately with a task_id for tracking progress.
    
    **Workflow:**
    1. Submit video generation request
    2. Receive task_id in response
    3. Poll `/api/tasks/{task_id}` to check status
    4. When status is "completed", retrieve video from result
    
    Request body includes all video generation parameters.
    See VideoGenerateRequest schema for details.
    
    Returns task_id for tracking progress.
    """
    reservation = None
    asset_ids = []
    lease_owned_by_route = False
    try:
        logger.info(f"Async video generation: {request_body.text[:50]}...")

        # Reserve capacity before returning a task ID so overflow is immediate.
        
        try:
            task, reservation, asset_ids, external_segments = (
                await external_audio.prepare_async_audio_submission(
                    request_body.narration_segments,
                    video_task_capacity.reserve,
                    lambda: task_manager.create_task(
                        task_type=TaskType.VIDEO_GENERATION,
                        request_params=request_body.model_dump(),
                    ),
                )
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail="narration audio asset not found"
            ) from exc
        except external_audio.AudioLeaseError as exc:
            raise HTTPException(
                status_code=409, detail="narration audio asset is already in use"
            ) from exc
        lease_owned_by_route = bool(asset_ids)
        
        # Define async execution function
        async def execute_video_generation():
            """Execute video generation in background"""
            # Auto-determine media_width and media_height from template meta tags (required)
            if not request_body.frame_template:
                raise ValueError("frame_template is required to determine media size")
            
            from pixelle_video.services.frame_html import HTMLFrameGenerator
            from pixelle_video.utils.template_util import resolve_template_path
            template_path = resolve_template_path(request_body.frame_template)
            generator = HTMLFrameGenerator(template_path)
            media_width, media_height = generator.get_media_size()
            logger.debug(f"Auto-determined media size from template: {media_width}x{media_height}")
            
            # Build video generation parameters
            video_params = {
                "text": request_body.text,
                "mode": request_body.mode,
                "title": request_body.title,
                "n_scenes": request_body.n_scenes,
                "min_narration_words": request_body.min_narration_words,
                "max_narration_words": request_body.max_narration_words,
                "min_image_prompt_words": request_body.min_image_prompt_words,
                "max_image_prompt_words": request_body.max_image_prompt_words,
                "media_width": media_width,
                "media_height": media_height,
                "media_workflow": request_body.media_workflow,
                "video_fps": request_body.video_fps,
                "frame_template": request_body.frame_template,
                "prompt_prefix": request_body.prompt_prefix,
                "bgm_path": request_body.bgm_path,
                "bgm_volume": request_body.bgm_volume,
                # Progress callback can be added here if needed
                # "progress_callback": lambda event: task_manager.update_progress(...)
            }
            if request_body.tts_speed is not None:
                video_params["tts_speed"] = request_body.tts_speed
            if external_segments:
                video_params["external_narration_segments"] = external_segments
            
            # Add TTS workflow if specified
            if request_body.tts_workflow:
                video_params["tts_workflow"] = request_body.tts_workflow
            
            # Add ref_audio if specified
            if request_body.ref_audio:
                video_params["ref_audio"] = request_body.ref_audio
            
            # Legacy voice_id support (deprecated)
            if request_body.voice_id:
                logger.warning("voice_id parameter is deprecated, please use tts_workflow instead")
                video_params["voice_id"] = request_body.voice_id
            
            # Add custom template parameters if specified
            if request_body.template_params:
                video_params["template_params"] = request_body.template_params
            
            result = await pixelle_video.generate_video(**video_params)
            
            # Get file size
            file_size = os.path.getsize(result.video_path) if os.path.exists(result.video_path) else 0
            
            # Convert path to URL
            video_url = path_to_url(request, result.video_path)
            
            return {
                "video_url": video_url,
                "duration": result.duration,
                "file_size": file_size
            }
        
        async def execute_video_generation_with_capacity(
            reserved_slot=reservation,
        ):
            try:
                async with reserved_slot:
                    return await execute_video_generation()
            finally:
                if asset_ids:
                    external_audio.release_audio_assets(asset_ids)

        # Start execution and transfer reservation ownership to the task.
        await task_manager.execute_task(
            task_id=task.task_id,
            coro_func=execute_video_generation_with_capacity
        )
        reservation = None
        lease_owned_by_route = False
        
        return VideoGenerateAsyncResponse(
            task_id=task.task_id
        )
        
    except TaskQueueFullError as e:
        logger.warning("Async video task queue is full")
        raise HTTPException(
            status_code=429,
            detail={"code": e.code, "message": "Video task queue is full"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Async video generation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if lease_owned_by_route and asset_ids:
            external_audio.release_audio_assets(asset_ids)
        if reservation is not None:
            await reservation.release()

