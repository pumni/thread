from __future__ import annotations

import argparse
import json
import re
import shutil
from importlib import metadata
from pathlib import Path, PurePosixPath

from packaging.markers import default_environment
from packaging.requirements import Requirement


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()

    locked_packages: dict[str, str] = {}
    for raw_line in args.requirements.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        requirement = line.removesuffix("\\").strip()
        parsed = Requirement(requirement)
        if parsed.marker and not parsed.marker.evaluate(default_environment()):
            continue
        versions = list(parsed.specifier)
        exact = next(
            (specifier.version for specifier in versions if specifier.operator == "=="), None
        )
        if exact:
            locked_packages[normalized_name(parsed.name)] = exact

    installed = {
        normalized_name(distribution.metadata.get("Name", "")): distribution
        for distribution in metadata.distributions()
    }
    records: list[dict[str, str]] = []
    license_root = args.destination / "licenses" / "python"
    license_root.mkdir(parents=True, exist_ok=True)

    for name, version in sorted(locked_packages.items()):
        distribution = installed.get(name)
        if distribution is None or distribution.version != version:
            raise SystemExit(
                f"Locked runtime dependency is missing or mismatched: {name}=={version}"
            )

        record = {
            "name": distribution.metadata.get("Name", name),
            "version": distribution.version,
            "license": distribution.metadata.get("License", "See included license files"),
        }
        records.append(record)
        target_root = license_root / f"{name}-{version}"
        for package_path in distribution.files or ():
            normalized_path = PurePosixPath(str(package_path))
            filename = normalized_path.name.lower()
            if not any(marker in filename for marker in ("license", "copying", "notice")):
                continue
            source = distribution.locate_file(package_path)
            if not source.is_file():
                continue
            destination = target_root / Path(*normalized_path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    (args.destination / "python-dependency-licenses.json").write_text(
        json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
