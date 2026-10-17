# Forest Fauna API examples

Run the application with `uvicorn main:app --reload`. Data endpoints require
MongoDB at `mongodb://localhost:27017/`. Audio analysis additionally requires
`perch_v2.onnx` and `labels.csv` next to `main.py`; it accepts uncompressed PCM
WAV files up to 25 MiB.

All error responses use the same shape:

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

## Health

```sh
curl http://localhost:8000/api/health
```

```json
{
  "status": "ok",
  "service": "forest-fauna-api",
  "model": {
    "name": "Google Perch",
    "version": "v2",
    "status": "ready"
  },
  "timestamp": "2026-10-16T12:00:00Z"
}
```

When the model files are unavailable, health returns `"status": "degraded"` and
`"model.status": "unavailable"`.

## Analyze an audio recording

```sh
curl -X POST http://localhost:8000/api/detections/analyze \
  -F "file=@forest_recording.wav" \
  -F "station_id=ST-003" \
  -F "recorded_at=2026-10-16T11:59:00Z"
```

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
    "station": {
      "id": "ST-003",
      "name": "North Ridge"
    },
    "model": {
      "name": "Google Perch",
      "version": "v2"
    },
    "processing_time_ms": 842,
    "detected_at": "2026-10-16T12:00:00Z"
  }
}
```

The species label is the model's prediction; unavailable common names and
animal groups are not inferred. An uncertain prediction has
`classification: "unclassified"`, `review_required: true`, null species names,
and group `"Unknown"`.

## Detection history

```sh
curl "http://localhost:8000/api/detections?page=1&limit=20&group=Unknown&search=Passer&station_id=ST-003&review_required=false"
```

```json
{
  "success": true,
  "data": [
    {
      "id": "FF-6A7B8C9D0E1F",
      "species": {
        "common_name": null,
        "scientific_name": "Passer domesticus",
        "group": "Unknown"
      },
      "confidence": 0.88,
      "classification": "identified",
      "review_required": false,
      "station": {
        "id": "ST-003",
        "name": "North Ridge"
      },
      "audio": {
        "filename": "forest_recording.wav",
        "duration_seconds": 12.5
      },
      "detected_at": "2026-10-16T12:00:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "limit": 20,
    "total_records": 1,
    "total_pages": 1
  }
}
```

Optional filters are `from` and `to` (ISO 8601 with timezone), as well as the
shown `group`, `search`, `station_id`, and `review_required` parameters.

## Dashboard statistics

```sh
curl "http://localhost:8000/api/stats?from=2026-10-16T00:00:00Z&to=2026-10-16T23:59:59Z"
```

```json
{
  "success": true,
  "period": {
    "from": "2026-10-16T00:00:00Z",
    "to": "2026-10-16T23:59:59Z"
  },
  "summary": {
    "total_detections": 1,
    "unique_species": 1,
    "active_stations": 1,
    "total_stations": 1,
    "high_confidence_rate": 1.0,
    "new_species_this_period": 1
  },
  "changes": {
    "detections_percent": null,
    "confidence_percentage_points": null
  },
  "activity": [
    {
      "time": "12:00",
      "count": 1
    }
  ],
  "top_species": [
    {
      "common_name": null,
      "scientific_name": "Passer domesticus",
      "group": "Unknown",
      "detection_count": 1
    }
  ]
}
```

Both period parameters are optional; omitted values select the current UTC day.
Comparison values are null when no prior-period data is available.

## Species catalog

```sh
curl "http://localhost:8000/api/species?search=Passer&page=1&limit=20"
```

```json
{
  "success": true,
  "data": [
    {
      "id": "SPEC-00001",
      "common_name": null,
      "scientific_name": "Passer domesticus",
      "group": "Unknown",
      "description": null,
      "conservation_status": null,
      "detection_count": 1,
      "last_detected_at": "2026-10-16T12:00:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "limit": 20,
    "total_records": 1,
    "total_pages": 1
  }
}
```

Use `group` to filter as well as `search`, `page`, and `limit`. Conservation
and descriptive information remain null until a verified metadata source is
configured.

## Monitoring stations

```sh
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
      "location": {
        "latitude": 10.25,
        "longitude": 77.15
      },
      "detections_today": 1
    }
  ]
}
```

Station records are read from MongoDB's `stations` collection. With no
configured station records the response contains an empty `data` array;
telemetry and location are never fabricated.
