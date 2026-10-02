from __future__ import annotations

import sys

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "version":
        print(f"threads-scheduler 0.1.0; Python {sys.version.split()[0]}")
    elif len(sys.argv) == 1:
        from runtime_scheduler import main

        main()
    else:
        raise SystemExit("usage: threads-scheduler [version]")
