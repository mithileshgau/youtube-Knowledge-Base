import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rocketride import RocketRideClient
import requests
from dotenv import load_dotenv

load_dotenv()

# ── shared state ──────────────────────────────────────────────────────────────
rr_client: RocketRideClient | None = None
pipeline_token: str | None = None
webhook_url: str | None = None
webhook_auth: str | None = None

jobs: dict[str, dict[str, Any]] = {}   # job_id → {status, result, error}


# ── RocketRide startup / shutdown ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global rr_client, pipeline_token, webhook_url, webhook_auth
    try:
        rr_client = RocketRideClient()
        await rr_client.connect()
        print("[Startup] Connected to RocketRide")

        # ── Study-guide pipeline ─────────────────────────────────────────────
        result = await rr_client.use(
            filepath="pipelines/youtube-url-to-study-guide.pipe",
            use_existing=False,
        )
        pipeline_token = result.get("token")
        webhook_url = result.get("webhook_url") or "http://localhost:5565/webhook"
        webhook_auth = result.get("publicToken")
        print(f"[Startup] Study-guide pipeline token: {pipeline_token}")
        print(f"[Startup] Study-guide webhook: {webhook_url}")

        if not pipeline_token:
            raise RuntimeError("Failed to get study-guide pipeline token")

    except Exception as e:
        print(f"[Startup] ERROR: {e}")
        import traceback
        traceback.print_exc()
        raise

    yield

    try:
        if pipeline_token:
            await rr_client.terminate(pipeline_token)
        await rr_client.disconnect()
    except Exception as e:
        print(f"[Shutdown] Error: {e}")


# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="YouTube Knowledge Base", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── background worker ─────────────────────────────────────────────────────────
def _download_video(url: str, job_id: str) -> str:
    """Download a YouTube video to a temp file. Returns the absolute file path."""
    import tempfile
    import yt_dlp

    out_dir = tempfile.mkdtemp(prefix=f"ykb_{job_id}_")
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "outtmpl": f"{out_dir}/%(id)s.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = ydl.prepare_filename(info)
        metadata = {
            "title": info.get("title"),
            "duration_seconds": info.get("duration"),
            "chapters": info.get("chapters", []),
            "webpage_url": info.get("webpage_url"),
        }
    print(f"[Job {job_id}] Downloaded video to: {file_path}")
    return file_path, metadata


def _extract_thumbnail(video_path: str, job_id: str) -> str | None:
    """Extract a thumbnail frame from the video, save to static/thumbnails/, return URL path."""
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        # Seek to 1 second in (or frame 0 if video is very short)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps))
        ret, frame = cap.read()
        cap.release()
        if not ret:
            # Fallback: read the very first frame
            cap = cv2.VideoCapture(video_path)
            ret, frame = cap.read()
            cap.release()
        if ret and frame is not None:
            thumb_name = f"{job_id}.jpg"
            thumb_path = f"static/thumbnails/{thumb_name}"
            cv2.imwrite(thumb_path, frame)
            print(f"[Job {job_id}] Thumbnail saved to {thumb_path}")
            return f"/static/thumbnails/{thumb_name}"
    except Exception as e:
        print(f"[Job {job_id}] Thumbnail extraction failed: {e}")
    return None


async def _run_analysis(job_id: str, url: str) -> None:
    import os
    try:
        print(f"[Job {job_id}] Starting analysis for URL: {url}")

        if not webhook_url or not webhook_auth:
            raise ValueError("Webhook URL or auth token not configured")

        # Step 1: Resolve video path — local file OR YouTube URL to download
        is_local = os.path.isabs(url) or os.path.exists(url)
        if is_local:
            video_path = url
            cleanup = False
            print(f"[Job {job_id}] Using local file: {video_path}")
            # Basic metadata for local files
            jobs[job_id]["metadata"] = {
                "title": os.path.basename(video_path),
                "duration_seconds": 0,
                "chapters": []
            }
        else:
            # Download from YouTube (blocking I/O -> run in thread pool)
            jobs[job_id]["status"] = "downloading"
            loop = asyncio.get_event_loop()
            video_path, metadata = await loop.run_in_executor(None, _download_video, url, job_id)
            jobs[job_id]["metadata"] = metadata
            cleanup = True

        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        # Extract thumbnail before uploading (non-blocking is fine for small files)
        thumbnail_url = _extract_thumbnail(video_path, job_id)

        # Step 2: POST raw video bytes to the RocketRide webhook.
        # The webhook node emits video + audio lanes natively from binary content.
        jobs[job_id]["status"] = "processing"
        file_size = os.path.getsize(video_path)
        print(f"[Job {job_id}] POSTing {file_size/1024/1024:.1f} MB video to {webhook_url}...")

        with open(video_path, "rb") as fh:
            response = requests.post(
                webhook_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Authorization": f"Bearer {webhook_auth}",
                },
                data=fh,
                timeout=1800,  # 30 min - video processing is slow
            )

        # Clean up temp files only if we downloaded them
        if cleanup:
            try:
                os.remove(video_path)
                os.rmdir(os.path.dirname(video_path))
            except Exception:
                pass


        response.raise_for_status()
        print(f"[Job {job_id}] Pipeline responded with status {response.status_code}")

        result = response.json()
        print(f"[Job {job_id}] Response top-level keys: {list(result.keys())}")

        # RocketRide often wraps the actual pipeline result in a 'data' key
        # especially in newer versions or batch/webhook modes.
        pipeline_data = result
        if "data" in result and isinstance(result["data"], dict) and "objectsRequested" in result["data"]:
            pipeline_data = result["data"]
            print(f"[Job {job_id}] Found nested pipeline data in 'data' key")

        # Handle both snake_case and camelCase for result types
        result_types = pipeline_data.get("result_types") or pipeline_data.get("resultTypes") or {}
        print(f"[Job {job_id}] Result types: {result_types}")

        # Log full response for debugging if no result types found
        if not result_types:
            print(f"[Job {job_id}] WARNING: result_types is empty! Full response: {json.dumps(result, indent=2, default=str)[:3000]}")
        else:
            print(f"[Job {job_id}] Full response (first 2000 chars): {json.dumps(result, indent=2, default=str)[:2000]}")

        # Check if there was a pipeline error
        if result.get("status") == "Error" or pipeline_data.get("status") == "Error":
            error_msg = (result.get("error") or pipeline_data.get("error", {})).get("message", "Unknown pipeline error")
            print(f"[Job {job_id}] Pipeline error: {error_msg}")
            raise ValueError(f"Pipeline execution error: {error_msg}")

        # Find the actual results
        answers = None
        if result_types:
            # Find the key that corresponds to 'answers' lane type (which is what response_answers outputs)
            answers_key = None
            for key, lane_type in result_types.items():
                if lane_type == "answers" or key == "study_guide":
                    answers_key = key
                    break

            if answers_key:
                # First check at the level of pipeline_data
                answers = pipeline_data.get(answers_key)
                
                # If not found there, it might be nested inside the objects map
                if not answers and "objects" in pipeline_data and isinstance(pipeline_data["objects"], dict):
                    # Try to find the answer in any of the objects
                    for obj_name, obj_data in pipeline_data["objects"].items():
                        if isinstance(obj_data, dict) and answers_key in obj_data:
                            print(f"[Job {job_id}] Found result for key '{answers_key}' in object '{obj_name}'")
                            answers = obj_data[answers_key]
                            break

            if not answers_key:
                print(f"[Job {job_id}] No answers lane found in result_types. Keys: {result_types}")
        
        # Fallback search if no specific answer key found or result_types is empty
        if not answers:
            # Try to find common result keys, excluding metadata keys
            metadata_keys = {"status", "name", "path", "objectId", "result_types", "resultTypes", "objectsRequested", "objectsCompleted", "objects", "data"}
            
            # Check pipeline_data and also check inside objects
            search_targets = [pipeline_data]
            if "objects" in pipeline_data and isinstance(pipeline_data["objects"], dict):
                search_targets.extend(pipeline_data["objects"].values())

            for target in search_targets:
                if not isinstance(target, dict): continue
                for key in ["study_guide", "answers", "output", "result"]:
                    if key in target and target[key]:
                        print(f"[Job {job_id}] Found data in key: {key}")
                        answers = target[key]
                        break
                if answers: break
            
            if not answers:
                # Try to find any key with actual content that isn't metadata
                for target in search_targets:
                    if not isinstance(target, dict): continue
                    for key, value in target.items():
                        if key not in metadata_keys:
                            if value and isinstance(value, (str, list, dict)):
                                print(f"[Job {job_id}] Found potential data key: {key}")
                                answers = value
                                break
                    if answers: break

            if not answers:
                print(f"[Job {job_id}] Response metadata: status={pipeline_data.get('status')}, objectsCompleted={pipeline_data.get('objectsCompleted')}, objectsRequested={pipeline_data.get('objectsRequested')}")
                raise ValueError(f"Pipeline did not return any results. resultTypes was empty and no output lanes found. Response: {json.dumps(result, default=str)}")

        # The pipeline might return multiple parts (e.g. if the LLM thinks it's a multi-part response)
        # We look for the first part that contains valid JSON.
        if not isinstance(answers, list):
            answers = [answers]

        data = None
        for idx, raw in enumerate(answers):
            if not isinstance(raw, str):
                data = raw
                print(f"[Job {job_id}] Found non-string answer at index {idx}, type: {type(raw)}")
                break

            raw = raw.strip()
            print(f"[Job {job_id}] Raw answer {idx} length: {len(raw)} chars")

            # Try to find a JSON-like block within the string
            start = raw.find('{')
            end = raw.rfind('}')
            if start != -1 and end != -1 and end > start:
                json_str = raw[start:end+1]
                print(f"[Job {job_id}] Extracted JSON block {idx} length: {len(json_str)} chars")
                try:
                    data = json.loads(json_str)
                    print(f"[Job {job_id}] Successfully parsed JSON with keys: {list(data.keys())}")
                    if isinstance(data, dict):
                        print(f"[Job {job_id}] Quiz count: {len(data.get('quiz', []))}")
                        print(f"[Job {job_id}] Flashcard count: {len(data.get('flashcards', []))}")
                        print(f"[Job {job_id}] Glossary count: {len(data.get('glossary', []))}")
                    break
                except json.JSONDecodeError as e:
                    print(f"[Job {job_id}] JSON parse error at index {idx}: {str(e)}")
                    continue

        if data is None:
            print(f"[Job {job_id}] Failed to parse JSON from any answer block")
            print(f"[Job {job_id}] First answer preview: {str(answers[0])[:1000] if answers else 'No answers'}")
            raise ValueError(f"Could not find valid JSON in pipeline answers. Raw: {str(answers[0])[:500] if answers else 'empty'}...")

        # Inject metadata into the result
        if isinstance(data, dict):
            # Ensure metadata object exists
            if "metadata" not in data:
                data["metadata"] = {}
            
            # Merge metadata: prioritize job (download) metadata, then fall back to pipeline/llm data
            current_job = jobs[job_id]
            job_meta = current_job.get("metadata", {})
            pipe_meta = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}
            
            data["metadata"].update({
                "title": pipe_meta.get("title") or job_meta.get("title") or data.get("title") or "Unknown Title",
                "duration_seconds": job_meta.get("duration_seconds") or pipe_meta.get("duration_seconds") or data.get("duration_seconds"),
                "thumbnail_url": thumbnail_url or job_meta.get("thumbnail_url") or pipe_meta.get("thumbnail_url"),
                "chapters": pipe_meta.get("chapters") or job_meta.get("chapters") or []
            })

        jobs[job_id] = {"status": "done", "result": data}
        print(f"[Job {job_id}] Analysis complete")

    except Exception as exc:
        import traceback
        print(f"[Job {job_id}] ERROR: {exc}")
        print(traceback.format_exc())
        jobs[job_id] = {"status": "error", "error": str(exc)}




# ── routes ────────────────────────────────────────────────────────────────────
class AnalyzeRequest(BaseModel):
    url: str


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/webhook-info")
async def get_webhook_info():
    """Return the webhook URL and auth token for external callers."""
    if not webhook_url or not webhook_auth:
        raise HTTPException(503, "Webhook not configured")
    return {"webhook_url": webhook_url, "webhook_token": webhook_auth}


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    if not rr_client or not pipeline_token:
        raise HTTPException(503, "Pipeline not ready")
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "processing"}
    asyncio.create_task(_run_analysis(job_id, req.url))
    return {"job_id": job_id}


@app.get("/status/{job_id}")
async def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


