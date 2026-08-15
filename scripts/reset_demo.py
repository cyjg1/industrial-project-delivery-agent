from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.init_demo import STORE_DIR, initialize_demo  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset the local synthetic demo workspace.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm removal of the ignored local data/store directory.",
    )
    args = parser.parse_args()
    if not args.yes:
        parser.error("Pass --yes to confirm resetting the local synthetic demo data.")

    target = STORE_DIR.resolve()
    expected_parent = (PROJECT_ROOT / "data").resolve()
    if target.parent != expected_parent or target.name != "store":
        raise RuntimeError(f"Refusing to remove unexpected path: {target}")
    if target.exists():
        shutil.rmtree(target)
    initialize_demo(target)
    print(f"Synthetic demo reset: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

