"""Write a local submission manifest bound to the minimal JSONL example."""
import hashlib
import json
from pathlib import Path
import sys


def main():
    folder = Path(__file__).resolve().parent
    agent = folder / "minimal_agent.py"
    submission = {
        "team_id": "example-team",
        "track": "interception",
        "resource_group": "cpu",
        "protocol": "openmd-competition@0.1",
        "entrypoint": [sys.executable, str(agent)],
        "artifacts": {agent.name: hashlib.sha256(agent.read_bytes()).hexdigest()},
    }
    destination = folder / "submission.json"
    destination.write_text(json.dumps(submission, indent=2) + "\n", encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
