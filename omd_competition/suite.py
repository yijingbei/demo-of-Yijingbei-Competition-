import json
from .runtime import suite
if __name__=='__main__':
    print(json.dumps(suite(),indent=2,ensure_ascii=False))
