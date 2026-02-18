import json
from pathlib import Path
import requests
import textwrap
import time

def write_dict_to_json(data: dict, filepath: str, indent: int = 2) -> None:
    """Write a dictionary to a JSON file."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)


def read_dict_from_json(filepath: str) -> dict:
    """Read a dictionary from a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def update_json_with_dict(filepath: str | Path, updates: dict, indent: int = 2) -> dict:
    """
    Update a JSON file with values from `updates`.
    Existing keys are overwritten, new keys are added.
    If the file doesn't exist, it is created.

    Returns the updated dictionary.
    """
    filepath = Path(filepath)

    # Load existing data (or start fresh)
    if filepath.exists():
        with filepath.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("JSON root must be a dictionary")
    else:
        data = {}

    # Update / add keys
    data.update(updates)

    # Write back
    with filepath.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)

    return data

""" DISCORD as communication chanell """

# 2000 discord limit. leave room for formatting.
MAX_LEN = 1900  

def send_discord(message, webhookurl):
    
    chunks = textwrap.wrap(message, MAX_LEN)
    for i, chunk in enumerate(chunks, 1):
        while True:
            response = requests.post(
                webhookurl,
                json={"content": chunk}
            )
            print(f"Chunk {i}/{len(chunks)} → Status:", response.status_code)

            if response.status_code == 429:
                retry_after = response.json().get("retry_after", 1)
                print(f"Rate limited. Sleeping {retry_after} seconds...")
                time.sleep(retry_after)
                continue
            break