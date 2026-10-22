# 🌿 Forest Fauna — Bioacoustic Wildlife Monitoring API

A FastAPI backend for real-time forest wildlife detection using **Google Perch v2**, designed as the software backbone for an ESP32-based field monitoring system. Upload a WAV recording from any station, and the API automatically locates the highest-energy audio window, runs it through the Perch acoustic model, and persists the identified species to MongoDB alongside station telemetry.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Server](#running-the-server)
- [API Reference](#api-reference)
- [Audio Pipeline](#audio-pipeline)
- [Project Structure](#project-structure)
- [Utilities](#utilities)
- [Development Notes](#development-notes)

---

## Overview

Forest Fauna is built around the **Google Perch v2** acoustic embedding model (ONNX), which can identify **14,795 animal species** (birds, mammals, amphibians, and more) from a 5-second audio clip sampled at 32 kHz. The system is structured around three concerns:

1. **Audio ingestion** — accepting WAV uploads from remote ESP32 field stations
2. **Classification** — extracting the loudest 5-second clip and running Perch inference
3. **Persistence & analytics** — storing detections, station status, and species statistics in MongoDB

---

## Architecture

```
ESP32 Station  ──[WAV upload]──▶  FastAPI  ──▶  auto_window (energy peak)
                                     │                │
                                     │           Perch v2 ONNX
                                     │                │
                                  MongoDB  ◀──  Detection result
                                     │
                              REST clients (dashboard, mobile, etc.)
```

| Layer | Technology |
|---|---|
| API framework | FastAPI + Uvicorn |
| ML inference | ONNX Runtime + Google Perch v2 |
| Audio processing | librosa, soundfile, numpy |
| Database | MongoDB (PyMongo) |
| Species catalogue | `labels.csv` — 14,795 iNaturalist 2024 + FSD50K labels |

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.10+ |
| MongoDB | 6.x (running locally on port 27017) |
| `perch_v2.onnx` | Downloaded separately (see below) |

### Download the Perch model

The ONNX model file is **not** included in this repository due to its size.

1. Go to: <https://huggingface.co/justinchuby/Perch-onnx/tree/main>
2. Download `perch_v2.onnx`
3. Place it in the project root alongside `main.py` and `labels.csv`

---

## Installation

```bash
git clone https://github.com/Infra-Creeper/ES-project.git
cd ES-project

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

---

## Configuration

| Setting | Default | Description |
|---|---|---|
| MongoDB URI | `mongodb://localhost:27017/` | Override with `MONGODB_URI` env var (for `clear_audio_data.py`) |
| Database name | `ES_project` | Hardcoded in `MongoCRUD.py` |
| Max upload size | 25 MiB | Enforced on the `/api/detections/analyze` endpoint |
| Confidence threshold | 0.25 | Below this, detection is marked `unclassified` |
| High-confidence threshold | 0.80 | Used for dashboard statistics |
| Audio sample rate | 32 000 Hz | Required input for Perch; librosa resamples on load |
| Clip length | 5 seconds | Perch expects exactly 160 000 samples |

No `.env` file is required for local development; MongoDB is assumed to be running on localhost.

---

## Running the Server

```bash
uvicorn main:app --reload
```

The API is available at `http://localhost:8000`. Interactive docs are at `http://localhost:8000/docs`.

### Verify the setup

Before connecting hardware, confirm Perch loads correctly:

```bash
python test_perch.py
```

This runs a random-noise input through the model and prints the top-5 output indices. A clean run means the full inference pipeline is functional.

---

## API Reference

All responses share a common shape. Errors always return:

```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "A valid audio file is required.",
    "details": null
  }
}
```

---

### `GET /api/health`

Returns service status and model availability.

```bash
curl http://localhost:8000/api/health
```

```json
{
  "status": "ok",
  "service": "forest-fauna-api",
  "model": { "name": "Google Perch", "version": "v2", "status": "ready" },
  "timestamp": "2026-10-16T12:00:00Z"
}
```

Returns `"status": "degraded"` and `"model.status": "unavailable"` when `perch_v2.onnx` or `labels.csv` are missing.

---

### `POST /api/detections/analyze`

Upload an uncompressed PCM WAV file for species classification. Accepts multipart form data.

```bash
curl -X POST http://localhost:8000/api/detections/analyze \
  -F "file=@forest_recording.wav" \
  -F "station_id=ST-003" \
  -F "recorded_at=2026-10-16T11:59:00Z"
```

**Form fields**

| Field | Required | Description |
|---|---|---|
| `file` | ✅ | Uncompressed PCM WAV, max 25 MiB |
| `station_id` | ✅ | ID of the transmitting station |
| `recorded_at` | ✅ | ISO 8601 timestamp of the recording |

**Response**

```json
{
  "success": true,
  "detection": {
    "id": "FF-6A7B8C9D0E1F",
    "species": {
      "common_name": null,
      "scientific_name": "Passer domesticus",
      "group": "Unknown"
    },
    "confidence": 0.88,
    "classification": "identified",
    "review_required": false,
    "audio": {
      "filename": "forest_recording.wav",
      "duration_seconds": 12.5,
      "sample_rate_hz": 16000
    },
    "station": { "id": "ST-003", "name": "North Ridge" },
    "model": { "name": "Google Perch", "version": "v2" },
    "processing_time_ms": 842,
    "detected_at": "2026-10-16T12:00:00Z"
  }
}
```

> **Confidence** is a softmax-normalized relative score over the model's top-5 logits (numerically stabilised), not a calibrated probability. When confidence is below the 0.25 threshold, `classification` is `"unclassified"`, species names are null, and `review_required` is `true`.

---

### `GET /api/detections`

Paginated detection history with optional filters.

```bash
curl "http://localhost:8000/api/detections?page=1&limit=20&group=Unknown&search=Passer&station_id=ST-003&review_required=false"
```

**Query parameters**

| Param | Type | Description |
|---|---|---|
| `page` | int | Page number (default: 1) |
| `limit` | int | Records per page (default: 20, max: 100) |
| `from` | ISO 8601 | Start of time range |
| `to` | ISO 8601 | End of time range |
| `group` | string | Filter by animal group |
| `search` | string | Partial-match on scientific name |
| `station_id` | string | Filter by station |
| `review_required` | bool | Filter unconfident detections |

---

### `GET /api/stats`

Dashboard statistics for a time period, with period-over-period comparison.

```bash
curl "http://localhost:8000/api/stats?from=2026-10-16T00:00:00Z&to=2026-10-16T23:59:59Z"
```

Both `from` and `to` are optional; defaults to the current UTC day. `changes.*` values are `null` when no prior-period data exists for comparison.

---

### `GET /api/species`

Browse the full Perch species catalogue (14,795 entries from `labels.csv`) enriched with detection counts.

```bash
curl "http://localhost:8000/api/species?search=Passer&page=1&limit=20"
```

**Query parameters:** `search`, `group`, `page`, `limit` — same semantics as `/api/detections`.

---

### `GET /api/stations`

List all monitoring stations with live telemetry from MongoDB.

```bash
curl http://localhost:8000/api/stations
```

```json
{
  "success": true,
  "data": [
    {
      "id": "ST-003",
      "name": "North Ridge",
      "status": "online",
      "last_seen_at": "2026-10-16T12:00:00Z",
      "battery_percentage": 94,
      "location": { "latitude": 10.25, "longitude": 77.15 },
      "detections_today": 1
    }
  ]
}
```

Station `status` must be one of `"online"`, `"attention"`, or `"offline"`.

---

## Audio Pipeline

```
Uploaded WAV
     │
     ▼
auto_window.py — slide 250 ms RMS energy window across recording
                 → identify peak-energy centre point
                 → extract 5-second clip centred on peak (pad if file is short)
                 → save as best_audio.wav
     │
     ▼
test_perch.py  — load best_audio.wav at 32 kHz
                 → run Perch v2 ONNX inference
                 → return top-5 (label, logit) pairs
     │
     ▼
main.py        — softmax-normalise top-5 logits
                 → apply confidence threshold (0.25)
                 → build detection record → persist to MongoDB
```

This mirrors the logic an ESP32 will eventually run in real time: compute short-term energy and transmit only when a peak is detected, then send the clip to this API for classification.

---

## Project Structure

```
ES-project/
├── main.py               # FastAPI app — all routes and request handling
├── MongoCRUD.py          # MongoDB connection and CRUD helpers
├── auto_window.py        # Energy-based best-clip extractor (librosa)
├── test_perch.py         # Perch model loader and sanity-check runner
├── clear_audio_data.py   # Utility: wipe audio collections and project files
├── labels.csv            # 14 795-class species label list (iNat2024 + FSD50K)
├── requirements.txt      # Pinned Python dependencies
└── API.md                # Detailed API examples with curl commands
```

---

## Utilities

### `clear_audio_data.py`

Drops the MongoDB audio collections and removes files under `recent/` and `combined/`. Runs as a **dry run** by default.

```bash
# Preview what will be deleted
python clear_audio_data.py

# Actually delete
python clear_audio_data.py --confirm
```

### `test_perch.py` (standalone)

```bash
# Sanity check with random noise (no audio file needed)
python test_perch.py          # calls test_with_random_input() by default

# Classify a real recording
# Edit the __main__ block to set your filename, then:
python test_perch.py
```

---

## Development Notes

- The API uses a single `asyncio.Lock` (`analysis_lock`) to serialise concurrent inference requests — Perch inference is CPU-bound and not safe to run in parallel on the same process.
- Logs are written to `log.txt` in the project root (overwritten each run).
- Station records must be pre-inserted into MongoDB's `stations` collection; the API never fabricates telemetry.
- Common names and conservation status in the species catalogue are `null` until a verified metadata source is wired in.
- The `best_audio.wav` file in the project root is overwritten on every analysis request.
