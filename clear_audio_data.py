"""Clear audio metadata and project audio files after an explicit confirmation."""

import argparse
import os
from pathlib import Path

from pymongo import MongoClient


PROJECT_DIR = Path(__file__).resolve().parent
DATABASE_NAME = "ES_project"
AUDIO_COLLECTIONS = (
    "audio_chunks",
    "audio_recordings",
    "audio_files.files",
    "audio_files.chunks",
)
AUDIO_DIRECTORIES = (
    PROJECT_DIR / "recent",
    PROJECT_DIR / "combined",
)


def _files_in(directory: Path) -> list[Path]:
    if not directory.exists():
        return []

    files = []
    for current, subdirectories, filenames in os.walk(
        directory, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        subdirectories[:] = [
            name
            for name in subdirectories
            if not (current_path / name).is_symlink()
        ]
        files.extend(current_path / filename for filename in filenames)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Delete audio-related MongoDB collections and files under the "
            "project's recent/ and combined/ folders."
        )
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Perform the deletions. Without this flag, only show the cleanup plan.",
    )
    args = parser.parse_args()

    print(f"MongoDB database: {DATABASE_NAME}")
    print(f"Collections to clear: {', '.join(AUDIO_COLLECTIONS)}")
    print("Project folders to clear:")
    for directory in AUDIO_DIRECTORIES:
        print(f"  {directory}")

    files_to_remove = [
        file_path
        for directory in AUDIO_DIRECTORIES
        for file_path in _files_in(directory)
    ]
    print(f"Project files to remove: {len(files_to_remove)}")

    if not args.confirm:
        print("Dry run only. Re-run with --confirm to perform this cleanup.")
        return

    mongodb_uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/")
    with MongoClient(mongodb_uri, serverSelectionTimeoutMS=5000) as client:
        client.admin.command("ping")
        database = client[DATABASE_NAME]
        existing_collections = set(database.list_collection_names())
        for collection_name in AUDIO_COLLECTIONS:
            if collection_name in existing_collections:
                database.drop_collection(collection_name)
                print(f"Dropped MongoDB collection: {collection_name}")

    for file_path in files_to_remove:
        file_path.unlink()
    print(f"Removed {len(files_to_remove)} project file(s).")
    print("Audio cleanup complete.")


if __name__ == "__main__":
    main()
