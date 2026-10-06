from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
THREADS_PACKAGE_ROOT = SOURCE_ROOT / "threads_platform"
BROWSER_PORT = "threads_platform.application.ports.browser"
PROCESS_LOCK_PORT = "threads_platform.application.ports.process_lock"
ENVIRONMENT_CREDENTIALS = "threads_platform.infrastructure.threads_api.environment_credentials"

STANDALONE_FORBIDDEN_IMPORTS = (
    "threads_platform.workers",
    "threads_platform.infrastructure.worker_agent",
    "threads_platform.domain.workers",
    "sqlalchemy",
    "threads_platform.infrastructure.persistence",
    "threads_platform.application.ports.repositories",
    "threads_platform.application.commands",
    "threads_platform.application.worker_jobs",
    "threads_platform.infrastructure.threads_api.composition",
    "threads_platform.transport",
)
STANDALONE_FORBIDDEN_SYMBOLS = frozenset(
    {
        "CommandRuntime",
        "CommandRuntimeComposition",
        "WorkerJob",
        "WorkerJobService",
        "UnitOfWork",
        "UnitOfWorkFactory",
        "SQLAlchemyUnitOfWorkFactory",
    }
)
BROWSER_INFRASTRUCTURE_FORBIDDEN_IMPORTS = (
    "threads_platform.workers",
    "threads_platform.domain",
    "threads_platform.standalone",
    "threads_platform.infrastructure.worker_agent",
)
SHARED_PORT_FORBIDDEN_IMPORTS = (
    "threads_platform.workers",
    "threads_platform.standalone",
    "threads_platform.infrastructure",
    "threads_platform.transport",
    "threads_platform.domain",
    "threads_platform.application.ports.repositories",
)
LOCAL_PROCESS_LOCK_FORBIDDEN_IMPORTS = (
    "threads_platform.workers",
    "threads_platform.infrastructure.worker_agent",
    "threads_platform.domain.workers",
)
ENVIRONMENT_CREDENTIALS_FORBIDDEN_IMPORTS = (
    "threads_platform.application.ports.repositories",
    "threads_platform.domain.accounts",
    "threads_platform.domain.time",
    "threads_platform.infrastructure.persistence",
    "threads_platform.infrastructure.threads_api.credentials",
)
LIFECYCLE_SYMBOLS = frozenset(
    {
        "UnitOfWork",
        "UnitOfWorkFactory",
        "SQLAlchemyUnitOfWorkFactory",
        "PersistentThreadsAccessTokenProvider",
    }
)
MOVED_BROWSER_SYMBOLS = frozenset(
    {
        "BROWSER_FEED_ORIGIN",
        "BROWSER_FEED_CANDIDATE_BOUND",
        "BrowserAdapterError",
        "BrowserContractError",
        "LocatorNotFound",
        "SessionExpired",
        "ChallengeDetected",
        "RemoteSessionStateUncertain",
        "NavigationTimeout",
        "MediaUploadFailed",
        "UnsupportedUIState",
        "BrowserProcessCrashed",
        "BrowserRuntimeUnavailable",
        "BrowserNetworkRouteUnsupported",
        "BrowserSurface",
        "PreparedMediaComposer",
        "BrowserNetworkProtocol",
        "BrowserNetworkRoute",
        "BrowserProxyCredentials",
        "BrowserLaunchRequest",
        "BrowserEngineSession",
        "BrowserFeedEngineSession",
        "BrowserThreadOpenEngineSession",
        "BrowserProfileOpenEngineSession",
        "BrowserMediaEngineSession",
        "BrowserEngine",
    }
)
ENVIRONMENT_CREDENTIAL_SYMBOLS = frozenset(
    {
        "THREADS_TOKEN_ENV_PREFIX",
        "_MAX_ENV_NAME_LENGTH",
        "_MAX_TOKEN_LENGTH",
        "_ENV_SUFFIX",
        "validate_threads_credential_ref",
        "normalize_threads_credential_ref",
        "_valid_secret_value",
        "EnvironmentThreadsCredentialSecretResolver",
    }
)
PERSISTENT_PROVIDER_ENVIRONMENT_IMPORTS = frozenset(
    {"validate_threads_credential_ref", "_valid_secret_value"}
)
LAUNCH_REQUEST_FIELDS = (
    "profile_directory",
    "network_route",
    "proxy_credentials",
    "headless",
)
LAUNCH_REQUEST_ANNOTATIONS = {
    "profile_directory": "Path",
    "network_route": "BrowserNetworkRoute",
    "proxy_credentials": "BrowserProxyCredentials | None",
    "headless": "bool",
}
LAUNCH_REQUEST_DEFAULTS = {
    "profile_directory": None,
    "network_route": None,
    "proxy_credentials": "field(default=None, repr=False)",
    "headless": "False",
}
NETWORK_ROUTE_FIELDS = ("protocol", "host", "port")
NETWORK_ROUTE_ANNOTATIONS = {
    "protocol": "BrowserNetworkProtocol",
    "host": "str | None",
    "port": "int | None",
}
NETWORK_ROUTE_DEFAULTS = {name: None for name in NETWORK_ROUTE_FIELDS}
PROXY_CREDENTIAL_FIELDS = ("username", "password")
PROXY_CREDENTIAL_ANNOTATIONS = {name: "str | None" for name in PROXY_CREDENTIAL_FIELDS}
PROXY_CREDENTIAL_DEFAULTS = {
    name: "field(default=None, repr=False)" for name in PROXY_CREDENTIAL_FIELDS
}
EMPTY_SYMBOLS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ImportReference:
    primary_target: str
    module_targets: tuple[str, ...]
    imported_symbols: tuple[str, ...]
    bound_symbols: tuple[str, ...]
    bindings: tuple[tuple[str, str], ...]
    module_bindings: tuple[tuple[str, str], ...]
    wildcard: bool


def repository_path(path: Path | str) -> str:
    if isinstance(path, str):
        return path.replace("\\", "/")
    return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()


def package_for_source(path: Path) -> str:
    parts = path.relative_to(SOURCE_ROOT).with_suffix("").parts
    if parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def resolve_relative_module(package: str, level: int, module: str | None) -> str:
    if level == 0:
        return module or ""
    package_parts = package.split(".") if package else []
    parents_to_remove = level - 1
    if parents_to_remove > len(package_parts):
        return ""
    prefix = package_parts[: len(package_parts) - parents_to_remove]
    suffix = module.split(".") if module else []
    return ".".join((*prefix, *suffix))


def importlib_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    module_aliases = {"importlib"}
    function_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    function_aliases.add(alias.asname or alias.name)
    return module_aliases, function_aliases


def dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        if prefix is not None:
            return f"{prefix}.{node.attr}"
    return None


def literal_dynamic_import_target(
    node: ast.Call,
    importlib_module_aliases: set[str],
    importlib_function_aliases: set[str],
) -> str | None:
    is_builtin_import = isinstance(node.func, ast.Name) and node.func.id == "__import__"
    is_importlib_import = (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "import_module"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in importlib_module_aliases
    )
    is_importlib_function = (
        isinstance(node.func, ast.Name) and node.func.id in importlib_function_aliases
    )
    if not (is_builtin_import or is_importlib_import or is_importlib_function):
        return None
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    target = node.args[0].value
    return target if isinstance(target, str) else None


def import_reference(
    primary_target: str,
    imported_symbols: tuple[str, ...] = (),
    bound_symbols: tuple[str, ...] = (),
    bindings: tuple[tuple[str, str], ...] = (),
    module_bindings: tuple[tuple[str, str], ...] = (),
    *,
    from_import: bool = False,
    wildcard: bool = False,
) -> ImportReference:
    targets = [primary_target] if primary_target else []
    if from_import and primary_target:
        targets.extend(f"{primary_target}.{symbol}" for symbol in imported_symbols if symbol != "*")
    return ImportReference(
        primary_target=primary_target,
        module_targets=tuple(dict.fromkeys(targets)),
        imported_symbols=imported_symbols,
        bound_symbols=bound_symbols,
        bindings=bindings,
        module_bindings=module_bindings,
        wildcard=wildcard,
    )


def collect_import_references(
    tree: ast.Module, current_package: str
) -> tuple[ImportReference, ...]:
    references: list[ImportReference] = []
    module_aliases, function_aliases = importlib_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                module_bindings = ((bound, alias.name),) if alias.asname else ()
                references.append(
                    import_reference(
                        alias.name,
                        bound_symbols=(bound,),
                        bindings=((alias.name, bound),),
                        module_bindings=module_bindings,
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            target = resolve_relative_module(current_package, node.level, node.module)
            imported = tuple(alias.name for alias in node.names)
            bound = tuple(alias.asname or alias.name for alias in node.names)
            bindings = tuple(zip(imported, bound, strict=True))
            module_bindings = tuple(
                (binding, f"{target}.{symbol}")
                for symbol, binding in bindings
                if symbol != "*" and target
            )
            references.append(
                import_reference(
                    target,
                    imported_symbols=imported,
                    bound_symbols=bound,
                    bindings=bindings,
                    module_bindings=module_bindings,
                    from_import=True,
                    wildcard="*" in imported,
                )
            )
        elif isinstance(node, ast.Call):
            target = literal_dynamic_import_target(node, module_aliases, function_aliases)
            if target is not None:
                references.append(import_reference(target))
    return tuple(references)


def module_matches(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(prefix + ".")


def import_violations(
    path: Path | str,
    references: tuple[ImportReference, ...],
    *,
    forbidden_prefixes: tuple[str, ...] = (),
    forbidden_symbols: frozenset[str] = frozenset(),
    reject_wildcards: bool = False,
    wildcard_prefixes: tuple[str, ...] = (),
) -> list[str]:
    label = repository_path(path)
    violations: list[str] = []
    for reference in references:
        blocked_target = next(
            (
                target
                for target in reference.module_targets
                if any(module_matches(target, prefix) for prefix in forbidden_prefixes)
            ),
            None,
        )
        if blocked_target is not None:
            violations.append(f"{label}: forbidden import target {blocked_target}")
        blocked_symbols = sorted(
            (set(reference.imported_symbols) | set(reference.bound_symbols)) & forbidden_symbols
        )
        violations.extend(
            f"{label}: forbidden imported symbol {symbol}" for symbol in blocked_symbols
        )
        if reference.wildcard and reject_wildcards:
            violations.append(
                f"{label}: forbidden wildcard import target {reference.primary_target}"
            )
        elif reference.wildcard and any(
            module_matches(target, prefix)
            for target in reference.module_targets
            for prefix in wildcard_prefixes
        ):
            target = next(
                target
                for target in reference.module_targets
                if any(module_matches(target, prefix) for prefix in wildcard_prefixes)
            )
            violations.append(f"{label}: forbidden wildcard import target {target}")
    return list(dict.fromkeys(violations))


def parse_source(path: Path) -> tuple[ast.Module, str]:
    source = path.read_text(encoding="utf-8")
    return ast.parse(source, filename=repository_path(path)), package_for_source(path)


def source_files(directory: Path) -> tuple[Path, ...]:
    return tuple(sorted(directory.rglob("*.py")))


def class_definition(tree: ast.Module, name: str) -> ast.ClassDef | None:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    return matches[0] if len(matches) == 1 else None


def dataclass_decorator(class_node: ast.ClassDef) -> bool:
    for decorator in class_node.decorator_list:
        value = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = dotted_name(value)
        if name == "dataclass" or name is not None and name.endswith(".dataclass"):
            return True
    return False


def class_contract_violations(
    path: Path | str,
    tree: ast.Module,
    class_name: str,
    *,
    expected_fields: tuple[str, ...] | None = None,
    expected_annotations: Mapping[str, str] | None = None,
    expected_defaults: Mapping[str, str | None] | None = None,
    expected_methods: tuple[str, ...] | None = None,
    expected_bases: tuple[str, ...] | None = None,
    forbidden_members: frozenset[str] = frozenset(),
    require_dataclass: bool = False,
) -> list[str]:
    label = repository_path(path)
    class_node = class_definition(tree, class_name)
    if class_node is None:
        return [f"{label}: required architecture contract {class_name}"]
    violations: list[str] = []
    field_names_list: list[str] = []
    actual_annotations: dict[str, str] = {}
    actual_defaults: dict[str, str | None] = {}
    for statement in class_node.body:
        if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
            continue
        field_name = statement.target.id
        field_names_list.append(field_name)
        actual_annotations[field_name] = ast.unparse(statement.annotation)
        actual_defaults[field_name] = (
            ast.unparse(statement.value) if statement.value is not None else None
        )
    field_names = tuple(field_names_list)
    if expected_fields is not None and field_names != expected_fields:
        violations.append(f"{label}: forbidden {class_name} field shape")
    elif expected_fields is not None:
        if expected_annotations is not None and actual_annotations != expected_annotations:
            violations.append(f"{label}: forbidden {class_name} field annotations")
        if expected_defaults is not None and actual_defaults != expected_defaults:
            violations.append(f"{label}: forbidden {class_name} field defaults")
    if require_dataclass and not dataclass_decorator(class_node):
        violations.append(f"{label}: forbidden {class_name} dataclass contract")
    if expected_bases is not None:
        actual_bases = tuple(dotted_name(base) or "<non-name-base>" for base in class_node.bases)
        if actual_bases != expected_bases:
            violations.append(f"{label}: forbidden {class_name} base shape")
    if expected_methods is not None:
        methods = tuple(
            statement.name
            for statement in class_node.body
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        if methods != expected_methods:
            violations.append(f"{label}: forbidden {class_name} operation shape")
    for member in sorted(forbidden_members):
        if member in _class_member_names(class_node):
            violations.append(f"{label}: forbidden {class_name} member {member}")
    return violations


def _class_member_names(class_node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for statement in class_node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(statement.name)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                names.update(_assignment_names(target))
    return names


def _assignment_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Attribute):
        return {target.attr}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for item in target.elts:
            names.update(_assignment_names(item))
        return names
    if isinstance(target, ast.Starred):
        return _assignment_names(target.value)
    return set()


def bound_symbol_names(tree: ast.Module, current_package: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(_assignment_names(target))
    for reference in collect_import_references(tree, current_package):
        names.update(reference.bound_symbols)
    return names


def literal_all_exports(tree: ast.Module) -> set[str]:
    exports: set[str] = set()
    for node in ast.walk(tree):
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
            value = node.value
        if value is None or not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in targets
        ):
            continue
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    exports.add(element.value)
    return exports


def ownership_violations(
    path: Path | str,
    tree: ast.Module,
    current_package: str,
    forbidden_symbols: frozenset[str],
    *,
    reject_wildcards: bool = False,
    wildcard_prefixes: tuple[str, ...] = (),
) -> list[str]:
    label = repository_path(path)
    violations = [
        f"{label}: forbidden owned or bound symbol {symbol}"
        for symbol in sorted(bound_symbol_names(tree, current_package) & forbidden_symbols)
    ]
    violations.extend(
        f"{label}: forbidden __all__ symbol {symbol}"
        for symbol in sorted(literal_all_exports(tree) & forbidden_symbols)
    )
    violations.extend(
        import_violations(
            path,
            collect_import_references(tree, current_package),
            reject_wildcards=reject_wildcards,
            wildcard_prefixes=wildcard_prefixes,
        )
    )
    return list(dict.fromkeys(violations))


def browser_launch_request_call_violations(
    path: Path | str,
    tree: ast.Module,
    current_package: str,
) -> list[str]:
    references = collect_import_references(tree, current_package)
    request_names = {"BrowserLaunchRequest"}
    browser_module_aliases: set[str] = set()
    for reference in references:
        for imported, bound in reference.bindings:
            if reference.primary_target == BROWSER_PORT and imported == "BrowserLaunchRequest":
                request_names.add(bound)
            if f"{reference.primary_target}.{imported}" == BROWSER_PORT:
                browser_module_aliases.add(bound)
        for bound, module in reference.module_bindings:
            if module == BROWSER_PORT:
                browser_module_aliases.add(bound)
    violations: list[str] = []
    label = repository_path(path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = dotted_name(node.func)
        is_request = callee in request_names or callee == f"{BROWSER_PORT}.BrowserLaunchRequest"
        if callee is not None and callee.endswith(".BrowserLaunchRequest"):
            module_name = callee[: -len(".BrowserLaunchRequest")]
            is_request = is_request or module_name in browser_module_aliases
        if not is_request:
            continue
        for keyword in node.keywords:
            if keyword.arg is None:
                violations.append(
                    f"{label}: forbidden BrowserLaunchRequest dynamic keyword mapping"
                )
            elif keyword.arg not in LAUNCH_REQUEST_FIELDS:
                violations.append(f"{label}: forbidden BrowserLaunchRequest keyword {keyword.arg}")
        if len(node.args) > len(LAUNCH_REQUEST_FIELDS):
            violations.append(
                f"{label}: forbidden BrowserLaunchRequest positional identity payload"
            )
    return list(dict.fromkeys(violations))


def assert_no_violations(violations: list[str]) -> None:
    assert not violations, "\n".join(violations)


def test_production_import_boundaries() -> None:
    violations: list[str] = []
    standalone_root = THREADS_PACKAGE_ROOT / "standalone"
    for path in source_files(standalone_root):
        tree, package = parse_source(path)
        references = collect_import_references(tree, package)
        violations.extend(
            import_violations(
                path,
                references,
                forbidden_prefixes=STANDALONE_FORBIDDEN_IMPORTS,
                forbidden_symbols=STANDALONE_FORBIDDEN_SYMBOLS,
            )
        )
    browser_root = THREADS_PACKAGE_ROOT / "infrastructure" / "browser"
    for path in source_files(browser_root):
        tree, package = parse_source(path)
        violations.extend(
            import_violations(
                path,
                collect_import_references(tree, package),
                forbidden_prefixes=BROWSER_INFRASTRUCTURE_FORBIDDEN_IMPORTS,
            )
        )
    for relative in (
        "src/threads_platform/application/ports/browser.py",
        "src/threads_platform/application/ports/process_lock.py",
    ):
        path = REPOSITORY_ROOT / relative
        tree, package = parse_source(path)
        violations.extend(
            import_violations(
                path,
                collect_import_references(tree, package),
                forbidden_prefixes=SHARED_PORT_FORBIDDEN_IMPORTS,
            )
        )
    local_lock = THREADS_PACKAGE_ROOT / "infrastructure" / "local" / "process_lock.py"
    tree, package = parse_source(local_lock)
    violations.extend(
        import_violations(
            local_lock,
            collect_import_references(tree, package),
            forbidden_prefixes=LOCAL_PROCESS_LOCK_FORBIDDEN_IMPORTS,
        )
    )
    old_lock = THREADS_PACKAGE_ROOT / "infrastructure" / "worker_agent" / "process_lock.py"
    if old_lock.exists():
        violations.append(
            f"{repository_path(old_lock)}: forbidden legacy Worker process-lock module"
        )
    worker_agent_ports = THREADS_PACKAGE_ROOT / "application" / "ports" / "worker_agent.py"
    tree, package = parse_source(worker_agent_ports)
    violations.extend(
        ownership_violations(
            worker_agent_ports,
            tree,
            package,
            frozenset({"WorkerProcessLock", "WorkerProcessAlreadyRunning"}),
        )
    )
    environment_credentials = (
        THREADS_PACKAGE_ROOT / "infrastructure" / "threads_api" / "environment_credentials.py"
    )
    tree, package = parse_source(environment_credentials)
    violations.extend(
        import_violations(
            environment_credentials,
            collect_import_references(tree, package),
            forbidden_prefixes=ENVIRONMENT_CREDENTIALS_FORBIDDEN_IMPORTS,
            forbidden_symbols=LIFECYCLE_SYMBOLS,
        )
    )
    assert_no_violations(violations)


def test_package_init_relative_worker_import_is_resolved_and_rejected() -> None:
    init_path = THREADS_PACKAGE_ROOT / "standalone" / "__init__.py"
    package = package_for_source(init_path)
    assert package == "threads_platform.standalone"

    tree = ast.parse("from ..workers.browser import WorkerBrowserSession")
    references = collect_import_references(tree, package)
    violations = import_violations(
        init_path,
        references,
        forbidden_prefixes=("threads_platform.workers",),
    )
    assert any("threads_platform.workers.browser" in violation for violation in violations), (
        violations
    )


def test_shared_browser_contract_shapes() -> None:
    browser_port = THREADS_PACKAGE_ROOT / "application" / "ports" / "browser.py"
    tree, _ = parse_source(browser_port)
    violations: list[str] = []
    violations.extend(
        class_contract_violations(
            browser_port,
            tree,
            "BrowserLaunchRequest",
            expected_fields=LAUNCH_REQUEST_FIELDS,
            expected_annotations=LAUNCH_REQUEST_ANNOTATIONS,
            expected_defaults=LAUNCH_REQUEST_DEFAULTS,
            expected_bases=(),
            require_dataclass=True,
        )
    )
    violations.extend(
        class_contract_violations(
            browser_port,
            tree,
            "BrowserEngineSession",
            expected_bases=("Protocol",),
            expected_methods=("navigate", "close"),
            forbidden_members=frozenset({"inspect_surface"}),
        )
    )
    violations.extend(
        class_contract_violations(
            browser_port,
            tree,
            "BrowserFeedEngineSession",
            expected_bases=("BrowserEngineSession", "Protocol"),
            expected_methods=("collect_feed_permalinks", "scroll_feed"),
        )
    )
    violations.extend(
        class_contract_violations(
            browser_port,
            tree,
            "BrowserThreadOpenEngineSession",
            expected_bases=("BrowserEngineSession", "Protocol"),
            expected_methods=("verify_thread_target",),
        )
    )
    violations.extend(
        class_contract_violations(
            browser_port,
            tree,
            "BrowserNetworkRoute",
            expected_fields=NETWORK_ROUTE_FIELDS,
            expected_annotations=NETWORK_ROUTE_ANNOTATIONS,
            expected_defaults=NETWORK_ROUTE_DEFAULTS,
            expected_bases=(),
            require_dataclass=True,
        )
    )
    violations.extend(
        class_contract_violations(
            browser_port,
            tree,
            "BrowserProxyCredentials",
            expected_fields=PROXY_CREDENTIAL_FIELDS,
            expected_annotations=PROXY_CREDENTIAL_ANNOTATIONS,
            expected_defaults=PROXY_CREDENTIAL_DEFAULTS,
            expected_bases=(),
            require_dataclass=True,
        )
    )
    assert_no_violations(violations)


def test_standalone_browser_request_is_identity_free() -> None:
    standalone_root = THREADS_PACKAGE_ROOT / "standalone"
    violations: list[str] = []
    for path in source_files(standalone_root):
        tree, package = parse_source(path)
        violations.extend(browser_launch_request_call_violations(path, tree, package))
    assert_no_violations(violations)


def test_moved_symbols_have_no_compatibility_owners() -> None:
    worker_browser = THREADS_PACKAGE_ROOT / "workers" / "browser.py"
    tree, package = parse_source(worker_browser)
    violations = ownership_violations(
        worker_browser,
        tree,
        package,
        MOVED_BROWSER_SYMBOLS,
        wildcard_prefixes=(BROWSER_PORT,),
    )

    worker_sessions = THREADS_PACKAGE_ROOT / "workers" / "sessions.py"
    tree, package = parse_source(worker_sessions)
    violations.extend(
        ownership_violations(
            worker_sessions,
            tree,
            package,
            frozenset({"NetworkRoute", "ProxyCredentials"}),
            reject_wildcards=True,
        )
    )

    credential_module = THREADS_PACKAGE_ROOT / "infrastructure" / "threads_api" / "credentials.py"
    tree, package = parse_source(credential_module)
    violations.extend(
        ownership_violations(
            credential_module,
            tree,
            package,
            ENVIRONMENT_CREDENTIAL_SYMBOLS - PERSISTENT_PROVIDER_ENVIRONMENT_IMPORTS,
        )
    )
    for reference in collect_import_references(tree, package):
        if reference.primary_target != ENVIRONMENT_CREDENTIALS:
            continue
        unexpected = set(reference.imported_symbols) - PERSISTENT_PROVIDER_ENVIRONMENT_IMPORTS
        if reference.wildcard or unexpected:
            target = "*" if reference.wildcard else sorted(unexpected)[0]
            violations.append(
                f"{repository_path(credential_module)}: forbidden "
                f"environment credential import {target}"
            )
    assert_no_violations(violations)


@pytest.mark.parametrize(
    ("case", "source", "package", "forbidden_prefixes", "forbidden_symbols", "expected"),
    [
        (
            "absolute Worker import",
            "import threads_platform.workers.browser",
            "threads_platform.standalone",
            ("threads_platform.workers",),
            EMPTY_SYMBOLS,
            "threads_platform.workers",
        ),
        (
            "relative Worker import",
            "from ..workers.browser import WorkerBrowserSession",
            "threads_platform.standalone",
            ("threads_platform.workers",),
            EMPTY_SYMBOLS,
            "threads_platform.workers.browser",
        ),
        (
            "literal __import__ Worker import",
            "__import__('threads_platform.workers.browser')",
            "threads_platform.standalone",
            ("threads_platform.workers",),
            EMPTY_SYMBOLS,
            "threads_platform.workers.browser",
        ),
        (
            "literal importlib Worker import",
            "import importlib\nimportlib.import_module('threads_platform.workers.browser')",
            "threads_platform.standalone",
            ("threads_platform.workers",),
            EMPTY_SYMBOLS,
            "threads_platform.workers.browser",
        ),
        (
            "Standalone persistence and CommandRuntime import",
            "from threads_platform.application.commands.runtime import CommandRuntime",
            "threads_platform.standalone",
            ("threads_platform.application.commands",),
            STANDALONE_FORBIDDEN_SYMBOLS,
            "CommandRuntime",
        ),
        (
            "generic browser to domain import",
            "from threads_platform.domain.workers import NetworkProtocol",
            "threads_platform.infrastructure.browser",
            ("threads_platform.domain",),
            EMPTY_SYMBOLS,
            "threads_platform.domain.workers",
        ),
        (
            "environment credential lifecycle import",
            "from threads_platform.application.ports.repositories import UnitOfWork",
            "threads_platform.infrastructure.threads_api",
            ("threads_platform.application.ports.repositories",),
            LIFECYCLE_SYMBOLS,
            "UnitOfWork",
        ),
    ],
    ids=(
        "absolute-worker-import",
        "relative-worker-import",
        "literal-__import__",
        "literal-importlib-import",
        "standalone-command-runtime",
        "browser-domain-import",
        "environment-uow-import",
    ),
)
def test_import_collector_rejects_synthetic_boundaries(
    case: str,
    source: str,
    package: str,
    forbidden_prefixes: tuple[str, ...],
    forbidden_symbols: frozenset[str],
    expected: str,
) -> None:
    tree = ast.parse(source)
    violations = import_violations(
        f"synthetic/{case.replace(' ', '_')}.py",
        collect_import_references(tree, package),
        forbidden_prefixes=forbidden_prefixes,
        forbidden_symbols=forbidden_symbols,
    )
    assert any(expected in violation for violation in violations), violations


@pytest.mark.parametrize(
    ("case", "source", "class_name", "expected_fields", "expected_methods", "target"),
    [
        (
            "BrowserLaunchRequest worker_id field",
            "@dataclass\nclass BrowserLaunchRequest:\n"
            "    profile_directory: Path\n    network_route: Route\n"
            "    proxy_credentials: object | None = None\n    headless: bool = False\n"
            "    worker_id: str = 'fake'",
            "BrowserLaunchRequest",
            LAUNCH_REQUEST_FIELDS,
            None,
            "BrowserLaunchRequest field shape",
        ),
        (
            "BrowserEngineSession inspect_surface",
            "class BrowserEngineSession:\n"
            "    async def navigate(self): ...\n    async def close(self): ...\n"
            "    async def inspect_surface(self): ...",
            "BrowserEngineSession",
            None,
            ("navigate", "close"),
            "BrowserEngineSession operation shape",
        ),
        (
            "BrowserNetworkRoute account_id field",
            "@dataclass\nclass BrowserNetworkRoute:\n"
            "    protocol: object\n    host: str | None\n    port: int | None\n"
            "    account_id: str",
            "BrowserNetworkRoute",
            NETWORK_ROUTE_FIELDS,
            None,
            "BrowserNetworkRoute field shape",
        ),
    ],
    ids=("launch-worker-id-field", "session-inspect-surface", "route-account-id-field"),
)
def test_contract_helpers_reject_synthetic_shape_violations(
    case: str,
    source: str,
    class_name: str,
    expected_fields: tuple[str, ...] | None,
    expected_methods: tuple[str, ...] | None,
    target: str,
) -> None:
    tree = ast.parse(source)
    violations = class_contract_violations(
        f"synthetic/{case.replace(' ', '_')}.py",
        tree,
        class_name,
        expected_fields=expected_fields,
        expected_methods=expected_methods,
    )
    assert any(target in violation for violation in violations), violations


@pytest.mark.parametrize(
    ("case", "source", "class_name", "expected_bases", "expected_methods"),
    [
        (
            "BrowserLaunchRequest inherits worker identity",
            "from dataclasses import dataclass, field\n"
            "@dataclass\n"
            "class BrowserLaunchRequest(WorkerIdentityBase):\n"
            "    profile_directory: Path\n"
            "    network_route: BrowserNetworkRoute\n"
            "    proxy_credentials: BrowserProxyCredentials | None = "
            "field(default=None, repr=False)\n"
            "    headless: bool = False",
            "BrowserLaunchRequest",
            (),
            None,
        ),
        (
            "BrowserEngineSession inherits worker surface inspection",
            "class BrowserEngineSession(WorkerSurfaceEngineSession, Protocol):\n"
            "    async def navigate(self, url: str, *, "
            "allowed_origins: frozenset[str]) -> None: ...\n"
            "    async def close(self) -> None: ...",
            "BrowserEngineSession",
            ("Protocol",),
            ("navigate", "close"),
        ),
    ],
    ids=("launch-request-base", "engine-session-base"),
)
def test_contract_helper_rejects_synthetic_inheritance(
    case: str,
    source: str,
    class_name: str,
    expected_bases: tuple[str, ...],
    expected_methods: tuple[str, ...] | None,
) -> None:
    tree = ast.parse(source)
    if class_name == "BrowserLaunchRequest":
        violations = class_contract_violations(
            f"synthetic/{case.replace(' ', '_')}.py",
            tree,
            class_name,
            expected_fields=LAUNCH_REQUEST_FIELDS,
            expected_annotations=LAUNCH_REQUEST_ANNOTATIONS,
            expected_defaults=LAUNCH_REQUEST_DEFAULTS,
            expected_bases=expected_bases,
            require_dataclass=True,
        )
    else:
        violations = class_contract_violations(
            f"synthetic/{case.replace(' ', '_')}.py",
            tree,
            class_name,
            expected_bases=expected_bases,
            expected_methods=expected_methods,
        )
    assert any(f"forbidden {class_name} base shape" in violation for violation in violations), (
        violations
    )


@pytest.mark.parametrize(
    ("case", "source", "expected"),
    [
        (
            "BrowserLaunchRequest worker_id keyword",
            "BrowserLaunchRequest(profile_directory=p, network_route=r, worker_id='fake')",
            "BrowserLaunchRequest keyword worker_id",
        ),
        (
            "BrowserLaunchRequest expanded keywords",
            "BrowserLaunchRequest(**request_values)",
            "BrowserLaunchRequest dynamic keyword mapping",
        ),
    ],
    ids=("launch-worker-id-keyword", "launch-expanded-keywords"),
)
def test_standalone_call_helper_rejects_synthetic_host_identity(
    case: str,
    source: str,
    expected: str,
) -> None:
    tree = ast.parse(source)
    violations = browser_launch_request_call_violations(
        f"synthetic/standalone/{case.replace(' ', '_')}.py",
        tree,
        "threads_platform.standalone",
    )
    assert any(expected in violation for violation in violations), violations


@pytest.mark.parametrize(
    ("case", "source", "expected"),
    [
        (
            "compatibility alias",
            "BrowserAdapterError = _BrowserAdapterError",
            "forbidden owned or bound symbol BrowserAdapterError",
        ),
        (
            "wildcard compatibility import",
            "from threads_platform.application.ports.browser import *",
            "forbidden wildcard import target threads_platform.application.ports.browser",
        ),
        (
            "literal __all__ compatibility re-export",
            "__all__ = ['BrowserAdapterError']",
            "forbidden __all__ symbol BrowserAdapterError",
        ),
    ],
    ids=("alias", "wildcard-import", "explicit-all-export"),
)
def test_ownership_helper_rejects_synthetic_compatibility_exports(
    case: str,
    source: str,
    expected: str,
) -> None:
    tree = ast.parse(source)
    violations = ownership_violations(
        f"synthetic/threads_platform/workers/browser_{case.replace(' ', '_')}.py",
        tree,
        "threads_platform.workers",
        MOVED_BROWSER_SYMBOLS,
        wildcard_prefixes=(BROWSER_PORT,),
    )
    assert any(expected in violation for violation in violations), violations
