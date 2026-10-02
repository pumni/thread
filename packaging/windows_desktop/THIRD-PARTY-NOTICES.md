# Windows Desktop runtime notices

The PostgreSQL runtime is the unmodified EDB Windows x64 binary ZIP listed in `download-manifest.json`. Preserve the archive's upstream license and all bundled component license notices in the resulting bundle. PostgreSQL is distributed under the [PostgreSQL License](https://www.postgresql.org/about/licence/).

Python dependencies are resolved from the repository's pinned `uv.lock`. `build_runtime.py` copies license/readme/copying/notice files from installed distributions into each candidate bundle and records package versions and declared license metadata in `python-dependency-licenses.json`. The shipped bundle must retain those notices alongside the executables.

This spike does not copy a user database, Python installation, uv cache, `.env` file, credentials, or developer profile into a package.
