"""Hold one host command until admission, then retain its process-group leader."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    payload = json.loads(sys.argv[1])
    if sys.stdin.buffer.read(1) != b'G':
        raise SystemExit(1)
    child = subprocess.Popen(payload['argv'], cwd=payload['cwd'], stdin=subprocess.DEVNULL)
    code = child.wait()
    status = Path(payload['status'])
    temporary = status.with_suffix('.writing')
    temporary.write_text(json.dumps({'exit_code': code}))
    os.replace(temporary, status)
    while not Path(payload['release']).exists():
        time.sleep(.025)


if __name__ == '__main__':
    main()
