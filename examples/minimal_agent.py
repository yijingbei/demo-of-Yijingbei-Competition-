"""Language-neutral protocol example in Python; no engine or competition imports."""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "close": break
    print(json.dumps({"protocol": "openmd-competition@0.1", "batch": None}), flush=True)
