from pymongo import MongoClient,ASCENDING,DESCENDING
from pymongo.errors import PyMongoError
import logging
import re
from datetime import datetime, timezone, date
import random
from itertools import combinations
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent




# ── Connection ────────────────────────────────────────────────────────────────

def establishDB():
    """Connect to MongoDB and initialise collection handles. Returns True on success."""
    try:
        global client, db, audio_chunks, audio_recordings, analysis, stations

        client = MongoClient(
            "mongodb://localhost:27017/", serverSelectionTimeoutMS=2000
        )
        client.admin.command("ping")
        db = client["ES_project"]
        audio_chunks = db["audio_chunks"]
        audio_recordings = db["audio_recordings"]
        analysis = db["analysis"]
        stations = db["stations"]
    
        logger.debug("Connected to MongoDB client")
        print("Connected to MongoDB client")
        return True
    except Exception as e:
        logger.exception("MongoDB connection failed")
        print("[!] Unable to connect to MongoDB")
        return False


def save_audio_chunk(
    audio_path: Path, audio_format: dict
) -> object:
    """Persist metadata and a project-relative path for one uploaded WAV chunk."""
    relative_path = audio_path.resolve().relative_to(PROJECT_DIR).as_posix()
    created_at = datetime.now(timezone.utc)
    result = audio_chunks.insert_one(
        {
            "file_path": relative_path,
            "filename": audio_path.name,
            "audio_format": audio_format,
            "duration_seconds": 5,
            "created_at": created_at,
            "uploaded_at": created_at,
            "status": "pending",
        }
    )
    return result.inserted_id


def get_pending_audio_chunks(limit: int) -> list:
    """Return the oldest pending WAV chunks."""
    chunks = list(
        audio_chunks
        .find({"status": "pending"})
        .sort("_id", ASCENDING)
        .limit(limit)
    )
    for chunk in chunks:
        file_path = chunk.get("file_path")
        if file_path is None:
            file_path = (Path("recent") / chunk["filename"]).as_posix()
        chunk["audio"] = (PROJECT_DIR / file_path).read_bytes()
    return chunks


def save_audio_recording(
    audio_path: Path,
    audio_format: dict,
    chunk_ids: list,
    duration_seconds: int,
) -> str:
    """Store metadata and a project-relative path for a combined recording."""
    recording_id = str(chunk_ids[0])
    recordings = audio_recordings
    existing = recordings.find_one({"_id": recording_id}, {"file_path": 1})
    if existing is not None and existing.get("file_path") is not None:
        return recording_id

    relative_path = audio_path.resolve().relative_to(PROJECT_DIR).as_posix()
    recordings.update_one(
        {"_id": recording_id},
        {
            "$setOnInsert": {
                "audio_format": audio_format,
                "duration_seconds": duration_seconds,
                "chunk_count": len(chunk_ids),
                "chunk_ids": [str(chunk_id) for chunk_id in chunk_ids],
                "created_at": datetime.now(timezone.utc),
            },
            "$set": {
                "file_path": relative_path,
                "filename": audio_path.name,
            },
            "$unset": {"audio_file_id": ""},
        },
        upsert=True,
    )
    return recording_id

def get_latest_combined():
    """Get the latest processed audio"""
    logger.debug("get_latest_combined() called")
    return audio_recordings.find_one(sort=[('_id',DESCENDING)])


def mark_audio_chunks_batched(chunk_ids: list, recording_id: str) -> None:
    """Mark the source chunks as included in a combined recording."""
    audio_chunks.update_many(
        {"_id": {"$in": chunk_ids}, "status": "pending"},
        {"$set": {"status": "batched", "recording_id": recording_id}},
    )
    logger.info(f"Audio chunks marked for {chunk_ids} as {recording_id}")


def disconnectDB():
    """Close the MongoDB connection."""
    client.close()
    logger.debug("Disconnected from MongoDB")
    print("Disconnected from MongoDB")



# ── Generic helpers ───────────────────────────────────────────────────────────

def _insert_one(collection, document: dict):
    """Insert a single document. Returns the inserted_id or None."""
    try:
        result = collection.insert_one(document)
        logger.debug("Inserted document into %s", collection.name)
        return result.inserted_id
    except PyMongoError:
        logger.exception("Insert failed on %s", collection.name)
        return None


def _find_one(collection, query: dict):
    """Return the first document matching *query*, or None."""
    try:
        return collection.find_one(query)
    except PyMongoError:
        logger.exception("find_one failed on %s", collection.name)
        return None


def _find_many(collection, query: dict):
    """Return a list of all documents matching *query* (default: all documents)."""
    try:
        return list(collection.find(query or {}))
    except PyMongoError:
        logger.exception("find_many failed on %s", collection.name)
        return []


def _update_one(collection, query: dict, update_fields: dict):
    """Update the first document matching *query*. Returns modified_count."""
    try:
        result = collection.update_one(query, {"$set": update_fields})
        logger.debug("Updated %d doc(s) in %s", result.modified_count, collection.name)
        return result.modified_count
    except PyMongoError:
        logger.exception("update_one failed on %s", collection.name)
        return 0


def _delete_one(collection, query: dict):
    """Delete the first document matching *query*. Returns deleted_count."""
    try:
        result = collection.delete_one(query)
        logger.debug("Deleted %d doc(s) from %s", result.deleted_count, collection.name)
        return result.deleted_count
    except PyMongoError:
        logger.exception("delete_one failed on %s", collection.name)
        return 0


def _delete_many(collection, query: dict):
    """Delete all documents matching *query*. Returns deleted_count."""
    try:
        result = collection.delete_many(query)
        logger.debug("Deleted %d doc(s) from %s", result.deleted_count, collection.name)
        return result.deleted_count
    except PyMongoError:
        logger.exception("delete_many failed on %s", collection.name)
        return 0

def _count_documents(collection, query: dict = {}):
    """Return the number of documents in *collection* matching *query* (default: all documents)."""
    try:
        count = collection.count_documents(query or {})
        logger.debug("Counted %d doc(s) in %s", count, collection.name)
        return count
    except PyMongoError:
        logger.exception("count_documents failed on %s", collection.name)
        return 0
# ── Specific data commands ──────────────────────────────────────────────────────────

def put_analysis(entry: dict[str, Any]):
    """Persist an analysis and propagate storage failures to the API."""
    result = analysis.insert_one(entry.copy())
    logger.debug("Uploaded analysis %s", result.inserted_id)
    return result.inserted_id


def get_analyses() -> list[dict]:
    """Return stored analyses newest first; database errors are handled by the API."""
    return list(
        analysis.find({}).sort(
            [("detected_at", DESCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)]
        )
    )


def get_stations() -> list[dict]:
    """Return configured monitoring stations without synthesizing telemetry."""
    return list(stations.find({}).sort("_id", ASCENDING))


def get_station(station_id: str) -> dict | None:
    """Find a station by its public ID or MongoDB string ID."""
    return stations.find_one({"$or": [{"id": station_id}, {"_id": station_id}]})