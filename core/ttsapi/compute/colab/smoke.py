"""One explicitly requested T4 validation; run under a bounded systemd user unit."""

import argparse
import hashlib
import signal
import tempfile
from pathlib import Path

from . import ColabPool


def interrupted(signum, frame):
    raise SystemExit(128 + signum)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("state", type=Path)
    parser.add_argument("python", type=Path)
    parser.add_argument("--reap", action="store_true")
    args = parser.parse_args()
    pool = ColabPool(args.state, python=args.python)
    if args.reap:
        pool.reap()
        return
    signal.signal(signal.SIGTERM, interrupted)
    before = set(pool.cli.api("default", "ps"))
    if before:
        raise SystemExit("active assignments make the budget ambiguous")
    with pool.db() as db:
        if db.execute("SELECT count(*) FROM usage").fetchone()[0]:
            raise SystemExit("use a fresh smoke ledger")
    with tempfile.TemporaryDirectory() as tmp:
        result = Path(tmp) / "result.bin"
        with pool.start("T4", task_id="T4-download-smoke", estimated_minutes=5) as run:
            print("T4 allocated", flush=True)
            output = run.execute("""import hashlib, os
h = hashlib.sha256()
with open('/content/tts-result.bin', 'wb') as stream:
    for _ in range(150):
        block = os.urandom(1024*1024)
        stream.write(block)
        h.update(block)
print(h.hexdigest())
""")
            run.download("/content/tts-result.bin", result)
            digest = hashlib.sha256()
            with result.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            assert (
                result.stat().st_size == 150 * 1024 * 1024
                and digest.hexdigest() in output
            )
            print(
                "150 MiB downloaded in three parts; independent VM sha256 matches",
                flush=True,
            )
        pool.cli.call(run.account_id, "release", run.name, run.endpoint)
        print("release on already absent endpoint succeeded", flush=True)
    after = set(pool.cli.api("default", "ps"))
    with pool.db() as db:
        (row,) = db.execute("SELECT stopped,actual_units FROM usage").fetchall()
    assert before == after and row["stopped"] is not None
    print(
        "remaining VMs",
        len(after),
        "usage rows 1",
        "actual units",
        row["actual_units"],
        flush=True,
    )
    assert 0 <= row["actual_units"] <= 0.3


if __name__ == "__main__":
    main()
