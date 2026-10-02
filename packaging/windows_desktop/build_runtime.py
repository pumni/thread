from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "download-manifest.json"
PG_FILENAME = "postgresql-17.11-4-windows-x64-binaries.zip"
LAYOUTS: dict[str, tuple[tuple[str, str], ...]] = {
    "shared": (("threads-runtime", "runtime_entry.py"),),
    "split": (
        ("threads-http", "http_entry.py"),
        ("threads-scheduler", "scheduler_entry.py"),
    ),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_postgres(destination: Path, url: str, expected_hash: str, content_bytes: int) -> None:
    if destination.is_file() and sha256_file(destination) == expected_hash:
        print(f"Using hash-verified PostgreSQL archive: {destination}")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        import urllib.request

        request = urllib.request.Request(url, headers={"User-Agent": "pumni-thread-desktop-spike"})
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
    else:
        chunk_size = 8 * 1024 * 1024
        parts = (content_bytes + chunk_size - 1) // chunk_size
        chunks = destination.with_suffix(destination.suffix + ".parts")
        chunks.mkdir(parents=True, exist_ok=True)

        def download_range(index: int) -> Path:
            start = index * chunk_size
            end = min(start + chunk_size, content_bytes) - 1
            chunk = chunks / f"part-{index:03}.bin"
            expected_size = end - start + 1
            if chunk.is_file() and chunk.stat().st_size == expected_size:
                return chunk
            subprocess.run(
                [
                    curl,
                    "--silent",
                    "--show-error",
                    "--fail",
                    "--location",
                    "--retry",
                    "3",
                    "--max-time",
                    "900",
                    "--range",
                    f"{start}-{end}",
                    "--output",
                    str(chunk),
                    url,
                ],
                check=True,
            )
            if not chunk.is_file() or chunk.stat().st_size != expected_size:
                raise RuntimeError(f"Invalid archive range {index}: size mismatch")
            return chunk

        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(download_range, index) for index in range(parts)]
            for completed, future in enumerate(as_completed(futures), start=1):
                future.result()
                if completed == parts or completed % 4 == 0:
                    print(f"Downloaded PostgreSQL archive ranges {completed}/{parts}", flush=True)

        with partial.open("wb") as output:
            for index in range(parts):
                with (chunks / f"part-{index:03}.bin").open("rb") as source:
                    shutil.copyfileobj(source, output)
        if partial.stat().st_size != content_bytes:
            partial.unlink(missing_ok=True)
            raise SystemExit("PostgreSQL archive length mismatch after range assembly")
        shutil.rmtree(chunks)

    actual_hash = sha256_file(partial)
    if actual_hash != expected_hash:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"PostgreSQL archive SHA-256 mismatch: {actual_hash}")
    partial.replace(destination)


def safe_extract_postgres(archive: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted_licenses: list[Path] = []
    root = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            normalized = Path(*Path(member.filename.replace("\\", "/")).parts)
            target = (destination / normalized).resolve()
            if root not in target.parents and target != root:
                raise SystemExit(f"Unsafe path in PostgreSQL archive: {member.filename}")
            if member.is_dir():
                continue
            basename = target.name.lower()
            is_license = any(tag in basename for tag in ("license", "copying", "notice"))
            relative = Path(*normalized.parts)
            include_runtime = (
                relative.parts
                and relative.parts[0].lower() == "pgsql"
                and (
                    len(relative.parts) > 1 and relative.parts[1].lower() in {"bin", "lib", "share"}
                )
            )
            if not is_license and not include_runtime:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            if is_license:
                extracted_licenses.append(target)

    pg_root = destination / "pgsql"
    for required in (
        pg_root / "bin" / "initdb.exe",
        pg_root / "bin" / "pg_ctl.exe",
        pg_root / "bin" / "postgres.exe",
    ):
        if not required.is_file():
            raise SystemExit(
                f"PostgreSQL archive missing required file: {required.relative_to(destination)}"
            )
    if not extracted_licenses:
        raise SystemExit("PostgreSQL archive had no license/notice files to preserve")
    return extracted_licenses


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_file(item)))
    return digest.hexdigest()


def command_output(*command: str) -> str:
    result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def run_pyinstaller(entry: Path, name: str, output: Path, work: Path) -> Path:
    dist = output / "python"
    work.mkdir(parents=True, exist_ok=True)
    command = [
        "uv",
        "run",
        "--locked",
        "--no-dev",
        "--group",
        "packaging",
        "pyinstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        name,
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(work),
        "--paths",
        str(ROOT / "src"),
        "--paths",
        str(HERE),
        "--add-data",
        f"{ROOT / 'alembic.ini'};.",
        "--add-data",
        f"{ROOT / 'migrations'};migrations",
        "--collect-all",
        "asyncpg",
        "--hidden-import",
        "sqlalchemy.dialects.postgresql.asyncpg",
        str(entry),
    ]
    print(f"Building {name} with PyInstaller onedir", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    return dist / name


def build_layout(
    layout: str,
    output_root: Path,
    python_licenses: Path,
    postgres_archive: Path,
    postgres_manifest: dict[str, Any],
    postgres_licenses: list[Path],
) -> dict[str, Any]:
    candidate = output_root / "candidates" / layout
    if candidate.exists():
        shutil.rmtree(candidate)
    candidate.mkdir(parents=True)
    stage = output_root / "stage" / layout
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    executables: list[dict[str, Any]] = []
    for name, entry in LAYOUTS[layout]:
        built = run_pyinstaller(
            HERE / entry, name, stage / name, output_root / "work" / layout / name
        )
        destination = candidate / name
        shutil.copytree(built, destination)
        executable = destination / f"{name}.exe"
        if not executable.is_file():
            raise SystemExit(f"PyInstaller output missing: {executable}")
        executables.append(
            {
                "name": name,
                "executable_bytes": executable.stat().st_size,
                "executable_sha256": sha256_file(executable),
            }
        )

    postgres_root = candidate / "postgresql"
    extraction_root = output_root / "stage" / "postgresql-extract"
    if not extraction_root.exists():
        safe_extract_postgres(postgres_archive, extraction_root)
    shutil.copytree(extraction_root / "pgsql", postgres_root)

    license_destination = candidate / "licenses" / "postgresql"
    license_destination.mkdir(parents=True, exist_ok=True)
    for source in postgres_licenses:
        relative = source.relative_to(extraction_root)
        destination = license_destination / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    shutil.copytree(python_licenses / "licenses", candidate / "licenses" / "python")
    shutil.copy2(python_licenses / "python-dependency-licenses.json", candidate)
    python_license_records = json.loads(
        (candidate / "python-dependency-licenses.json").read_text(encoding="utf-8")
    )
    python_license_file_count = sum(
        path.is_file() for path in (candidate / "licenses" / "python").rglob("*")
    )
    shutil.copy2(HERE / "THIRD-PARTY-NOTICES.md", candidate)

    head = command_output("git", "rev-parse", "HEAD")
    status = command_output("git", "status", "--porcelain")
    manifest = {
        "source_revision": head,
        "source_tree_dirty": bool(status),
        "layout": layout,
        "python": command_output(
            "uv", "run", "--locked", "--no-dev", "--group", "packaging", "python", "--version"
        ),
        "pyinstaller": command_output(
            "uv", "run", "--locked", "--no-dev", "--group", "packaging", "pyinstaller", "--version"
        ),
        "project_lock_sha256": sha256_file(ROOT / "uv.lock"),
        "postgresql": {
            "version": postgres_manifest["version"],
            "archive_url": postgres_manifest["url"],
            "archive_sha256": sha256_file(postgres_archive),
            "license_files": len(postgres_licenses),
        },
        "python_dependencies": {
            "locked_packages": len(python_license_records),
            "license_files": python_license_file_count,
        },
        "executables": executables,
        "candidate_tree_sha256": tree_sha256(candidate),
        "candidate_bytes": directory_size(candidate),
        "database_data_included": False,
        "secrets_included": False,
    }
    manifest_path = candidate / "runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (candidate / "runtime-manifest.json.sha256").write_text(
        f"{sha256_file(manifest_path)}  runtime-manifest.json\n", encoding="ascii"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build Windows Desktop runtime packaging candidates"
    )
    parser.add_argument("--layout", choices=("shared", "split", "all"), default="all")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build" / "windows-desktop")
    parser.add_argument("--postgres-archive", type=Path)
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    postgres = manifest["postgresql"]
    archive = args.postgres_archive or (ROOT / "build" / "downloads" / PG_FILENAME)
    download_postgres(
        archive,
        postgres["url"],
        postgres["sha256"],
        postgres["content_bytes"],
    )
    postgres_stage = args.output_dir / "stage" / "postgresql-extract"
    if postgres_stage.exists():
        shutil.rmtree(postgres_stage)
    postgres_licenses = safe_extract_postgres(archive, postgres_stage)

    requirements = args.output_dir / "runtime-requirements.txt"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    python_licenses = args.output_dir / "python-notices"
    python_licenses.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "--no-dev",
            "--group",
            "packaging",
            "python",
            str(HERE / "write_python_notices.py"),
            "--requirements",
            str(requirements),
            "--destination",
            str(python_licenses),
        ],
        cwd=ROOT,
        check=True,
    )

    layouts = ("shared", "split") if args.layout == "all" else (args.layout,)
    results = [
        build_layout(
            layout,
            args.output_dir,
            python_licenses,
            archive,
            postgres,
            postgres_licenses,
        )
        for layout in layouts
    ]
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
