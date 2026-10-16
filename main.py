from fastapi import FastAPI, HTTPException, Request
from auto_window import auto_extract_best_clip
from contextlib import asynccontextmanager
from test_perch import res_with_real_audio, MODEL_PATH
from io import BytesIO
from pathlib import Path
import asyncio
import wave
from uuid import uuid4
import MongoCRUD as crud
import logging
from datetime import datetime, timezone, date
logging.basicConfig(level=logging.INFO,filename="log.txt",filemode="w",format="%(levelname)s | %(name)s\n%(message)s")
logger = logging.getLogger(__name__)

CHUNK_DURATION_SECONDS = 5
RECORDING_DURATION_SECONDS = 60
CHUNKS_PER_RECORDING = RECORDING_DURATION_SECONDS // CHUNK_DURATION_SECONDS
RECENT_AUDIO_DIR = Path(__file__).resolve().parent / "recent"
COMBINED_AUDIO_DIR = Path(__file__).resolve().parent / "combined"
upload_lock = asyncio.Lock()


def _read_wav_chunk(audio: bytes) -> tuple[dict, bytes]:
    try:
        with wave.open(BytesIO(audio), "rb") as wav_file:
            audio_format = {
                "channels": wav_file.getnchannels(),
                "sample_width": wav_file.getsampwidth(),
                "sample_rate": wav_file.getframerate(),
                "compression_type": wav_file.getcomptype(),
            }
            frame_count = wav_file.getnframes()
            if frame_count > wav_file.getframerate() * CHUNK_DURATION_SECONDS:
                raise HTTPException(
                    status_code=400,
                    detail="Each uploaded WAV file must be less than 5 seconds long.",
                )
            if wav_file.getcomptype() != "NONE":
                raise HTTPException(
                    status_code=400,
                    detail="Only uncompressed PCM WAV files are supported.",
                )
            frames = wav_file.readframes(frame_count)
            expected_size = (
                frame_count * wav_file.getnchannels() * wav_file.getsampwidth()
            )
            if len(frames) != expected_size:
                raise HTTPException(
                    status_code=400, detail="The uploaded WAV file is incomplete."
                )
            return audio_format, frames
    except HTTPException:
        raise
    except (wave.Error, EOFError, OSError) as exc:
        raise HTTPException(status_code=400, detail="The upload is not a valid WAV file.") from exc


def _combine_wav_chunks(chunks: list) -> bytes:
    audio_formats = [chunk["audio_format"] for chunk in chunks]
    audio_format = audio_formats[0]
    if any(current_format != audio_format for current_format in audio_formats[1:]):
        raise RuntimeError("Pending WAV chunks have different audio formats.")

    frames = [_read_wav_chunk(chunk["audio"])[1] for chunk in chunks]
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(audio_format["channels"])
        wav_file.setsampwidth(audio_format["sample_width"])
        wav_file.setframerate(audio_format["sample_rate"])
        wav_file.writeframes(b"".join(frames))
    return output.getvalue()


def _complete_pending_recordings() -> list[str]:
    completed_ids = []
    while True:
        chunks = crud.get_pending_audio_chunks(CHUNKS_PER_RECORDING)
        if len(chunks) < CHUNKS_PER_RECORDING:
            return completed_ids

        combined_audio = _combine_wav_chunks(chunks)
        chunk_ids = [chunk["_id"] for chunk in chunks]
        combined_path = COMBINED_AUDIO_DIR / f"{chunk_ids[0]}.wav"
        combined_path.write_bytes(combined_audio)
        recording_id = crud.save_audio_recording(
            combined_path,
            chunks[0]["audio_format"],
            chunk_ids,
            len(chunks) * CHUNK_DURATION_SECONDS,
        )
        
        #put into anlysis
        res = process_audio(combined_path.as_posix(),"auto")
        res.update(filename = f"{chunk_ids[0]}.wav", file_path = combined_path.as_posix(), recording_id = recording_id, model=MODEL_PATH)
        crud.put_analysis(res)
        
        crud.mark_audio_chunks_batched(chunk_ids, recording_id)
        with open("recording.wav", "wb") as recording:
            recording.write(combined_audio)
        completed_ids.append(recording_id)


@asynccontextmanager
async def MongoDB_lifespan(app: FastAPI):
    # Startup code
    ok = crud.establishDB()
    if ok:
        logger.info("Successfully connected to MongoDB")
        print("Successfully connected to MongoDB")
    else:
        print("CRITICAL")
        logger.critical("Unable to connect to MongoDB!",stack_info=True)
        raise RuntimeError("MongoDB must be available to accept audio uploads.")

    yield

    # Shutdown code
    crud.disconnectDB()
    logger.info("Disconnected from MongoDB")



app = FastAPI(lifespan=MongoDB_lifespan)

def process_audio(path: str, processing_mode: str) -> dict[str,list|str]:
    """Processes the audio and returns the analysis of it using the model in test_perch.py"""
    auto_extract_best_clip(path,"best_audio.wav")
    res = res_with_real_audio("best_audio.wav")
    res["created_at"] = datetime.now(timezone.utc)
    res["processing_mode"] = processing_mode
    return res

@app.post("/upload")
async def upload_audio(request: Request):

    audio = await request.body()
    audio_format, _ = _read_wav_chunk(audio)
    RECENT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    COMBINED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    async with upload_lock:
        completed_recordings = _complete_pending_recordings()
        pending_chunks = crud.get_pending_audio_chunks(CHUNKS_PER_RECORDING - 1)
        if any(
            chunk["audio_format"] != audio_format for chunk in pending_chunks
        ):
            raise HTTPException(
                status_code=400,
                detail="WAV format must match the other chunks in the current minute.",
            )

        filename = f"{uuid4().hex}.wav"
        audio_path = RECENT_AUDIO_DIR / filename
        audio_path.write_bytes(audio)
        try:
            upload_id = crud.save_audio_chunk(audio_path, audio_format)
        except Exception:
            audio_path.unlink(missing_ok=True)
            raise
        with open("recording.wav", "wb") as recording:
            recording.write(audio)
        completed_recordings.extend(_complete_pending_recordings())

    logger.debug("Received %d bytes as audio chunk %s", len(audio), upload_id)

    return {
        "success": True,
        "filename": filename,
        "size": len(audio),
        "upload_id": str(upload_id),
        "recording_complete": bool(completed_recordings),
        "recording_ids": completed_recordings,
        "combined_filenames": [
            f"{recording_id}.wav" for recording_id in completed_recordings
        ],
    }

@app.get("/process_latest")
async def display_latest(request: Request):
    latest = crud.get_latest_combined()
    res = process_audio(latest["file_path"],"on_demand")
    res.update(filename = latest["filename"], file_path = latest["file_path"], recording_id = latest["_id"], model=MODEL_PATH)
    crud.put_analysis(res)
    return res