from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml


def test_compose_http_waits_for_tls_prepare_and_keeps_keys_isolated() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "compose.yaml"
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = document["services"]
    http = services["http"]
    scheduler = services["scheduler"]
    tls_admin = services["tls-admin"]
    tls_prepare = services["tls-prepare"]

    assert tls_admin["profiles"] == ["tls-admin"]
    assert "profiles" not in tls_prepare
    assert "ensure" in tls_prepare["command"]
    assert http["depends_on"]["tls-prepare"]["condition"] == "service_completed_successfully"
    assert http["environment"]["THREADS_PLATFORM_TLS_CERTFILE"].endswith("/leaf-fullchain.pem")
    assert http["environment"]["THREADS_PLATFORM_TLS_KEYFILE"].endswith("/leaf-key.pem")

    http_volumes = http.get("volumes", [])
    scheduler_volumes = scheduler.get("volumes", [])
    prepare_volumes = tls_prepare["volumes"]
    admin_volumes = tls_admin["volumes"]
    assert all("controller-tls-admin" not in str(volume) for volume in http_volumes)
    assert all("controller-tls" not in str(volume) for volume in scheduler_volumes)
    assert any(
        isinstance(volume, str)
        and volume.split(":", maxsplit=1)[0] == "controller_tls_admin"
        and volume.endswith(":ro")
        for volume in prepare_volumes
    )
    assert any(
        isinstance(volume, str) and volume.split(":", maxsplit=1)[0] == "controller_tls_serving"
        for volume in prepare_volumes
    )
    assert any("controller_tls_admin" in str(volume) for volume in admin_volumes)

    for service in (http, scheduler):
        environment = service.get("environment", {})
        assert isinstance(environment, dict)
        environment_map = cast(dict[str, object], environment)
        assert all(
            not any(marker in str(name).upper() for marker in ("ROOT_KEY", "PRIVATE_KEY_BYTES"))
            for name in environment_map
        )
