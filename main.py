from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from typing import Literal
from uuid import uuid4
import asyncio
import logging
import math
import wave
from io import BytesIO

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool


import MongoCRUD as crud
from auto_window import auto_extract_best_clip
from test_perch import LABELS_PATH, MODEL_PATH, load_labels, res_with_real_audio

logging.basicConfig(
    level=logging.INFO,
    filename="log.txt",
    filemode="w",
    format="%(levelname)s | %(name)s\n%(message)s",
)
logger = logging.getLogger(__name__)

MODEL_NAME = "Google Perch"
MODEL_VERSION = "v2"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
CONFIDENCE_THRESHOLD = 0.5
HIGH_CONFIDENCE_THRESHOLD = 0.8
PROJECT_DIR = Path(__file__).resolve().parent
BEST_AUDIO_PATH = PROJECT_DIR / "best_audio.wav"
analysis_lock = asyncio.Lock()
router = APIRouter(prefix="/api")


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _confidence_from_logit(logit: object) -> float:
    if not isinstance(logit, (int, float)):
        return 0.0
    score = float(logit)
    if score >= 0:
        return 1.0 / (1.0 + math.exp(-min(score, 709)))
    exp_score = math.exp(max(score, -709))
    return exp_score / (1.0 + exp_score)


def _species_from_prediction(prediction: dict | None) -> tuple[dict, float]:
    if not prediction or not isinstance(prediction.get("label"), str):
        return {
            "common_name": None,
            "scientific_name": None,
            "group": "Unknown",
        }, 0.0

    confidence = _confidence_from_logit(prediction.get("logit"))
    if confidence < CONFIDENCE_THRESHOLD:
        return {
            "common_name": None,
            "scientific_name": None,
            "group": "Unknown",
        }, confidence

    return {
        "common_name": None,
        "scientific_name": prediction["label"],
        "group": "Unknown",
    }, confidence


def _station_summary(document: dict | None) -> dict | None:
    if document is None:
        return None
    station_id = document.get("id", document.get("_id"))
    if station_id is None:
        return None
    return {"id": str(station_id), "name": document.get("name")}


def _serialize_detection(document: dict) -> dict:
    detected_at = _as_datetime(document.get("detected_at") or document.get("created_at"))
    species = document.get("species")
    confidence = document.get("confidence")
    if not isinstance(species, dict):
        predictions = document.get("top")
        prediction = predictions[0] if isinstance(predictions, list) and predictions else None
        species, confidence = _species_from_prediction(prediction)
    if not isinstance(confidence, (int, float)):
        confidence = 0.0
    else:
        confidence = min(1.0, max(0.0, float(confidence)))

    classification = document.get("classification")
    if classification not in ("identified", "unclassified"):
        classification = (
            "identified"
            if species.get("scientific_name") is not None
            else "unclassified"
        )
    station = document.get("station")
    if not isinstance(station, dict):
        station = None

    raw_audio = document.get("audio")
    if not isinstance(raw_audio, dict):
        raw_audio = {}
    audio = {
        "filename": raw_audio.get("filename", document.get("filename")),
        "duration_seconds": raw_audio.get("duration_seconds", document.get("duration_seconds")),
    }
    if raw_audio.get("sample_rate_hz") is not None:
        audio["sample_rate_hz"] = raw_audio["sample_rate_hz"]

    record_id = document.get("id")
    if record_id is None:
        record_id = f"FF-{document.get('_id', uuid4().hex)}"
    return {
        "id": str(record_id),
        "species": species,
        "confidence": confidence,
        "classification": classification,
        "review_required": bool(
            document.get("review_required", classification == "unclassified")
        ),
        "station": station,
        "audio": audio,
        "detected_at": _utc_iso(detected_at),
    }


def _all_detections() -> list[dict]:
    return [_serialize_detection(record) for record in crud.get_analyses()]


def _require_database(request: Request) -> None:
    if not request.app.state.mongo_ready:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SERVICE_UNAVAILABLE",
                "message": "Detection storage is unavailable.",
            },
        )


def _validate_period(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if (start is not None and start.tzinfo is None) or (
        end is not None and end.tzinfo is None
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Date and time filters must include a timezone.",
            },
        )
    if start is not None:
        start = start.astimezone(timezone.utc)
    if end is not None:
        end = end.astimezone(timezone.utc)
    if end is None:
        end = datetime.combine(now.date(), time.max, tzinfo=timezone.utc)
    if start is None:
        start = datetime.combine(end.date(), time.min, tzinfo=timezone.utc)
    if start > end:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "'from' must be earlier than or equal to 'to'.",
            },
        )
    return start, end


def _model_available() -> bool:
    return (PROJECT_DIR / MODEL_PATH).is_file() and (PROJECT_DIR / LABELS_PATH).is_file()


def _error_code(status_code: int) -> str:
    return {
        400: "VALIDATION_ERROR",
        404: "NOT_FOUND",
        413: "FILE_TOO_LARGE",
        415: "UNSUPPORTED_MEDIA_TYPE",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMITED",
        503: "SERVICE_UNAVAILABLE",
    }.get(status_code, "INTERNAL_ERROR")


@asynccontextmanager
async def MongoDB_lifespan(app: FastAPI):
    app.state.mongo_ready = crud.establishDB()
    if app.state.mongo_ready:
        logger.info("Successfully connected to MongoDB")
    else:
        logger.error("MongoDB is unavailable; data endpoints will return 503.")
    yield
    if app.state.mongo_ready:
        crud.disconnectDB()
        logger.info("Disconnected from MongoDB")


app = FastAPI(lifespan=MongoDB_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins= [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    details = None
    code = _error_code(exc.status_code)
    message = str(exc.detail)
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", code)
        message = exc.detail.get("message", message)
        details = exc.detail.get("details")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {"code": code, "message": message, "details": details},
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Invalid request parameters.",
                "details": [
                    {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
                    for error in exc.errors()
                ],
            },
        },
    )


@app.exception_handler(Exception)
async def internal_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled API error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal server error occurred.",
                "details": None,
            },
        },
    )


@router.get("/health")
async def health() -> dict:
    model_ready = _model_available()
    return {
        "status": "ok" if model_ready else "degraded",
        "service": "forest-fauna-api",
        "model": {
            "name": MODEL_NAME,
            "version": MODEL_VERSION,
            "status": "ready" if model_ready else "unavailable",
        },
        "timestamp": _utc_iso(datetime.now(timezone.utc)),
    }


@router.post("/detections/analyze", status_code=201)
async def analyze_detection(
    request: Request,
    file: UploadFile | None = File(default=None),
    station_id: str | None = Form(default=None),
    recorded_at: datetime | None = Form(default=None),
) -> dict:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_MEDIA_TYPE",
                "message": "Audio must be submitted as multipart/form-data.",
            },
        )
    if file is None or not file.filename:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "A valid audio file is required.",
            },
        )
    if not _model_available():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SERVICE_UNAVAILABLE",
                "message": "Audio analysis is unavailable because the detection model is not configured.",
            },
        )
    if recorded_at is not None and recorded_at.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "'recorded_at' must include a timezone.",
            },
        )
    filename = Path(file.filename).name
    if Path(filename).suffix.lower() != ".wav":
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_MEDIA_TYPE",
                "message": "Only uncompressed PCM WAV audio is supported.",
            },
        )

    audio_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "FILE_TOO_LARGE",
                "message": f"Audio files must not exceed {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            },
        )
    try:
        with wave.open(BytesIO(audio_bytes), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise ValueError("Compressed WAV is not supported.")
            frame_count = wav_file.getnframes()
            sample_rate = wav_file.getframerate()
            if sample_rate <= 0 or frame_count <= 0:
                raise ValueError("Audio must contain PCM frames and a valid sample rate.")
            audio_metadata = {
                "filename": filename,
                "duration_seconds": frame_count / sample_rate,
                "sample_rate_hz": sample_rate,
            }
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_MEDIA_TYPE",
                "message": "The uploaded file is not a valid uncompressed PCM WAV recording.",
            },
        ) from exc

    _require_database(request)
    station = None
    if station_id is not None:
        station = _station_summary(crud.get_station(station_id))
        if station is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "NOT_FOUND",
                    "message": f"Monitoring station '{station_id}' was not found.",
                },
            )

    if not audio_bytes:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_MEDIA_TYPE",
                "message": "The uploaded audio file is empty.",
            },
        )

    started_at = perf_counter()
    try:
        async with analysis_lock:
            with NamedTemporaryFile(
                suffix=".wav", dir=PROJECT_DIR, delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(audio_bytes)
            try:
                model_result = await run_in_threadpool(
                    process_audio, str(temporary_path), "on_demand"
                )
            finally:
                temporary_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.exception("Audio analysis failed")
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ANALYSIS_FAILED",
                "message": "Audio analysis failed.",
            },
        ) from exc

    predictions = model_result.get("top")
    prediction = predictions[0] if isinstance(predictions, list) and predictions else None
    species, confidence = _species_from_prediction(prediction)
    classification = (
        "identified"
        if species["scientific_name"] is not None
        else "unclassified"
    )
    detected_at = datetime.now(timezone.utc)
    detection = {
        "id": f"FF-{uuid4().hex[:12].upper()}",
        "species": species,
        "confidence": confidence,
        "classification": classification,
        "review_required": classification == "unclassified",
        "audio": audio_metadata,
        "station": station,
        "model": {"name": MODEL_NAME, "version": MODEL_VERSION},
        "processing_time_ms": round((perf_counter() - started_at) * 1000),
        "detected_at": _utc_iso(detected_at),
    }
    crud.put_analysis(
        {
            **detection,
            "recorded_at": recorded_at.astimezone(timezone.utc) if recorded_at else None,
            "created_at": detected_at,
            "top": predictions or [],
            "model_path": str(MODEL_PATH),
        }
    )
    return {"success": True, "detection": detection}


@router.get("/detections")
async def list_detections(
    request: Request,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    group: Literal["Bird", "Mammal", "Amphibian", "Insect", "Unknown"] | None = None,
    search: str | None = None,
    station_id: str | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    review_required: bool | None = None,
) -> dict:
    _require_database(request)
    start, end = _validate_period(from_, to) if from_ is not None or to is not None else (None, None)
    normalized_search = search.casefold().strip() if search else None
    detections = []
    for detection in _all_detections():
        timestamp = _as_datetime(detection["detected_at"])
        if start is not None and (
            timestamp is None
            or timestamp < start
            or (end is not None and timestamp > end)
        ):
            continue
        if group is not None and detection["species"].get("group") != group:
            continue
        if station_id is not None and (detection["station"] or {}).get("id") != station_id:
            continue
        if review_required is not None and detection["review_required"] != review_required:
            continue
        if normalized_search and not any(
            normalized_search in str(detection["species"].get(field) or "").casefold()
            for field in ("common_name", "scientific_name")
        ):
            continue
        detections.append(detection)
    detections.sort(
        key=lambda item: _as_datetime(item["detected_at"]) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    total_records = len(detections)
    offset = (page - 1) * limit
    return {
        "success": True,
        "data": detections[offset : offset + limit],
        "pagination": {
            "page": page,
            "limit": limit,
            "total_records": total_records,
            "total_pages": math.ceil(total_records / limit),
        },
    }


@router.get("/stats")
async def statistics(
    request: Request,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
) -> dict:
    _require_database(request)
    period_start, period_end = _validate_period(from_, to)
    all_detections = _all_detections()
    in_period = [
        detection
        for detection in all_detections
        if (timestamp := _as_datetime(detection["detected_at"])) is not None
        and period_start <= timestamp <= period_end
    ]
    period_duration = period_end - period_start
    previous_start = period_start - period_duration - timedelta(microseconds=1)
    previous_end = period_start - timedelta(microseconds=1)
    previous = [
        detection
        for detection in all_detections
        if (timestamp := _as_datetime(detection["detected_at"])) is not None
        and previous_start <= timestamp <= previous_end
    ]
    identified = [
        detection
        for detection in in_period
        if detection["species"].get("scientific_name") is not None
    ]
    previous_identified = [
        detection
        for detection in previous
        if detection["species"].get("scientific_name") is not None
    ]
    species_counts: dict[str, dict] = {}
    activity: dict[str, int] = {}
    for detection in identified:
        scientific_name = detection["species"]["scientific_name"]
        species_info = species_counts.setdefault(
            scientific_name,
            {
                "common_name": detection["species"].get("common_name"),
                "scientific_name": scientific_name,
                "group": detection["species"].get("group", "Unknown"),
                "detection_count": 0,
            },
        )
        species_info["detection_count"] += 1
    for detection in in_period:
        timestamp = _as_datetime(detection["detected_at"])
        if timestamp is not None:
            hour = timestamp.strftime("%H:00")
            activity[hour] = activity.get(hour, 0) + 1

    previous_names = {
        detection["species"]["scientific_name"]
        for detection in previous_identified
    }
    station_documents = crud.get_stations()
    active_stations = sum(
        1
        for station in station_documents
        if station.get("status") in ("online", "attention")
    )
    current_high_confidence = (
        sum(
            1
            for detection in in_period
            if detection["confidence"] >= HIGH_CONFIDENCE_THRESHOLD
        )
        / len(in_period)
        if in_period
        else None
    )
    previous_high_confidence = (
        sum(
            1
            for detection in previous
            if detection["confidence"] >= HIGH_CONFIDENCE_THRESHOLD
        )
        / len(previous)
        if previous
        else None
    )
    detections_change = (
        (len(in_period) - len(previous)) / len(previous) * 100
        if previous
        else None
    )
    confidence_change = (
        (current_high_confidence - previous_high_confidence) * 100
        if current_high_confidence is not None and previous_high_confidence is not None
        else None
    )
    return {
        "success": True,
        "period": {"from": _utc_iso(period_start), "to": _utc_iso(period_end)},
        "summary": {
            "total_detections": len(in_period),
            "unique_species": len(species_counts),
            "active_stations": active_stations,
            "total_stations": len(station_documents),
            "high_confidence_rate": current_high_confidence,
            "new_species_this_period": len(set(species_counts) - previous_names),
        },
        "changes": {
            "detections_percent": detections_change,
            "confidence_percentage_points": confidence_change,
        },
        "activity": [
            {"time": hour, "count": activity[hour]}
            for hour in sorted(activity)
        ],
        "top_species": sorted(
            species_counts.values(),
            key=lambda species: species["detection_count"],
            reverse=True,
        )[:5],
    }


@router.get("/species")
async def species_catalog(
    request: Request,
    search: str | None = None,
    group: Literal["Bird", "Mammal", "Amphibian", "Insect", "Unknown"] | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    _require_database(request)
    if not (PROJECT_DIR / LABELS_PATH).is_file():
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SERVICE_UNAVAILABLE",
                "message": "The model species catalog is unavailable.",
            },
        )
    detections = _all_detections()
    detection_totals: dict[str, dict] = {}
    for detection in detections:
        scientific_name = detection["species"].get("scientific_name")
        if scientific_name is None:
            continue
        entry = detection_totals.setdefault(
            scientific_name, {"count": 0, "last_detected_at": None}
        )
        entry["count"] += 1
        timestamp = _as_datetime(detection["detected_at"])
        current_last = _as_datetime(entry.get("last_detected_at"))
        if timestamp is not None and (current_last is None or timestamp > current_last):
            entry["last_detected_at"] = _utc_iso(timestamp)

    normalized_search = search.casefold().strip() if search else None
    catalog = []
    for index, scientific_name in enumerate(load_labels(LABELS_PATH), start=1):
        if normalized_search and normalized_search not in scientific_name.casefold():
            continue
        if group is not None and group != "Unknown":
            continue
        totals = detection_totals.get(scientific_name, {})
        catalog.append(
            {
                "id": f"SPEC-{index:05d}",
                "common_name": None,
                "scientific_name": scientific_name,
                "group": "Unknown",
                "description": None,
                "conservation_status": None,
                "detection_count": totals.get("count", 0),
                "last_detected_at": totals.get("last_detected_at"),
            }
        )
    total_records = len(catalog)
    offset = (page - 1) * limit
    return {
        "success": True,
        "data": catalog[offset : offset + limit],
        "pagination": {
            "page": page,
            "limit": limit,
            "total_records": total_records,
            "total_pages": math.ceil(total_records / limit),
        },
    }


@router.get("/stations")
async def monitoring_stations(request: Request) -> dict:
    _require_database(request)
    today = datetime.now(timezone.utc).date()
    today_detections = [
        detection
        for detection in _all_detections()
        if (timestamp := _as_datetime(detection["detected_at"])) is not None
        and timestamp.date() == today
    ]
    result = []
    for document in crud.get_stations():
        status = document.get("status")
        if status not in ("online", "attention", "offline"):
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "INVALID_STATION_DATA",
                    "message": "A monitoring station has an invalid operational status.",
                },
            )
        station_id = str(document.get("id", document.get("_id", "")))
        raw_location = document.get("location")
        location = None
        if isinstance(raw_location, dict):
            latitude = raw_location.get("latitude")
            longitude = raw_location.get("longitude")
            if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                location = {"latitude": latitude, "longitude": longitude}
        battery = document.get("battery_percentage")
        if not isinstance(battery, (int, float)) or not 0 <= battery <= 100:
            battery = None
        last_seen = _as_datetime(document.get("last_seen_at"))
        result.append(
            {
                "id": station_id,
                "name": document.get("name"),
                "status": status,
                "last_seen_at": _utc_iso(last_seen),
                "battery_percentage": battery,
                "location": location,
                "detections_today": sum(
                    1
                    for detection in today_detections
                    if (detection["station"] or {}).get("id") == station_id
                ),
            }
        )
    return {"success": True, "data": result}


def process_audio(path: str, processing_mode: str) -> dict:
    """Select the loudest five-second clip and classify it with the real Perch model."""
    auto_extract_best_clip(path, str(BEST_AUDIO_PATH))
    result = res_with_real_audio(str(BEST_AUDIO_PATH))
    if result.get("err") or not result.get("top"):
        raise RuntimeError("The model did not return a usable species prediction.")
    result["created_at"] = datetime.now(timezone.utc)
    result["processing_mode"] = processing_mode
    return result


app.include_router(router)
