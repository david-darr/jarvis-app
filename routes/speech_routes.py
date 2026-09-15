"""Speech-to-text HTTP surface for chat dictation and Open Mic.

Thin adapter over core/speech.py, matching the shape routes/cookbook_routes.py
already uses for local model downloads: list, start, poll progress.

Transcription is require_user rather than require_admin: dictating into your
own chat is an ordinary per-session action, the same as sending a message.
Downloading a model is admin-gated, because it writes several hundred
megabytes into the shared data directory.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from core import speech
from core.middleware import require_admin, require_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/speech", tags=["speech"])


@router.get("/status")
async def speech_status(user: str = Depends(require_user)) -> dict:
    """Whether dictation can work at all, and which models are on disk.

    `engine_available` and `active_model` are reported separately on purpose:
    a missing binding and a missing model are different problems with
    different fixes, and collapsing them into one boolean would leave the UI
    unable to say which.
    """
    return speech.status()


@router.post("/models/{name}/download")
async def download_model(name: str, user: str = Depends(require_admin)) -> dict:
    try:
        speech.start_download(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@router.get("/models/{name}/progress")
async def download_progress(name: str, user: str = Depends(require_user)) -> dict:
    return speech.get_download_progress(name)


@router.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), user: str = Depends(require_user)) -> dict:
    """16 kHz mono WAV in, text out.

    Read with an explicit ceiling rather than straight into memory: this
    accepts an upload, and an unbounded read is how a large or hostile one
    would pin the backend. core/speech.py re-checks the same limit, since it
    is also reachable from other callers.
    """
    data = await audio.read(speech.MAX_AUDIO_BYTES + 1)
    if len(data) > speech.MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="That recording is too large to transcribe.")
    try:
        text = speech.transcribe(data)
    except speech.SpeechUnavailable as e:
        # 422 with the message as-is: every SpeechUnavailable is written to be
        # shown to the user, so wrapping it would lose the useful part.
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("speech: unexpected transcription failure")
        raise HTTPException(status_code=500, detail="Transcription failed.")
    # An empty string is a real, successful answer meaning "no speech found"
    # (silence, a mis-click, background noise), not an error. The client
    # decides whether to say anything about it.
    return {"text": text}
