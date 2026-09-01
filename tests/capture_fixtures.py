"""Helper script to copy captured live probes into test fixtures."""

import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "tests" / "data"
FIXTURES = PROJECT / "tests" / "fixtures" / "generated"

SAMPLES = {
    "list_8678795_probe.json": FIXTURES / "lists" / "list_8678795.json",
    "movie_9013_probe.json": FIXTURES / "movies" / "movie_9013.json",
    "movie_475557_probe.json": FIXTURES / "movies" / "movie_475557.json",
    "movie_398978_probe.json": FIXTURES / "movies" / "movie_398978.json",
    "movie_769_probe.json": FIXTURES / "movies" / "movie_769.json",
    "movie_1013601_probe.json": FIXTURES / "movies" / "movie_1013601.json",
    "search_inception_probe.json": FIXTURES / "search" / "inception.json",
    "find_imdb_probe.json": FIXTURES / "search" / "find_imdb.json",
    "collection_633215_probe.json": FIXTURES / "collections" / "collection_633215.json",
    "collection_987044_probe.json": FIXTURES / "collections" / "collection_987044.json",
}


def main() -> None:
    for src_name, dst in SAMPLES.items():
        src = DATA_DIR / src_name
        if src.exists():
            payload = json.loads(src.read_text(encoding="utf-8"))
            dst.write_text(
                json.dumps(payload["body"], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print("wrote", dst.name)
        else:
            print("missing", src_name)


if __name__ == "__main__":
    main()
