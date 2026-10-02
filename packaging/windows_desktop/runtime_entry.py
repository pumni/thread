from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="threads-runtime")
    parser.add_argument("mode", choices=("http", "scheduler", "migrate", "version"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.mode == "version":
        print(f"threads-runtime 0.1.0; Python {sys.version.split()[0]}")
        return 0
    if args.mode == "http":
        from runtime_http import main as run_http

        run_http(args.host, args.port)
    elif args.mode == "scheduler":
        from runtime_scheduler import main as run_scheduler

        run_scheduler()
    else:
        from runtime_migrate import main as run_migrations

        run_migrations()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
