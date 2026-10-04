"""Android targets and the staged exporter implementation."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from infernux.engine.build import (
    BuildArtifact,
    BuildConfiguration,
    BuildDiagnostic,
    BuildPlan,
    BuildRequest,
    BuildResult,
    BuildStep,
    BuildOption,
    BuildOptionChoice,
    BuildOptionKind,
    BuildTarget,
    CapabilityReport,
    DiagnosticSeverity,
    PlatformCapabilities,
    PlatformExporter,
)
from .native_payload import inspect_native_payload, stage_native_payload

from .doctor import (
    ANDROID_BUILD_TOOLS,
    ANDROID_GRADLE_PLUGIN,
    ANDROID_NDK,
    inspect_android_toolchain,
)
from .runtime_manifest import validate_runtime_manifest


_ANDROID_CAPABILITIES = PlatformCapabilities(
    graphics_api="vulkan",
    threads=True,
    dynamic_loading=True,
    filesystem=True,
    network=True,
    audio=True,
    pointer_input=True,
    text_input=True,
    gamepad_input=True,
    python_native_modules=True,
    cpu_jit=False,
    persistent_storage=True,
    features=frozenset(
        {
            "android-lifecycle",
            "density-aware-viewport",
            "gpu-compute-aot",
            "multi-touch",
            "software-keyboard",
        }
    ),
)

_ANDROID_PYTHON_SERIES = "3.13"
_ANDROID_MINIMUM_API = 26
_ANDROID_STDLIB_IGNORES = (
    "__pycache__",
    "*.pyc",
    "_test*.so",
    "_xxtestfuzz*.so",
    "xxlimited*.so",
    "test",
    "idlelib",
    "tkinter",
    "turtledemo",
    "_pyrepl",
    "pydoc_data",
    "ensurepip",
    f"config-{_ANDROID_PYTHON_SERIES}-*",
)
_NUMPY_RUNTIME_EXCLUDED_PREFIXES = (
    "numpy/random/_examples/",
    "numpy/_core/tests/",
    "numpy/fft/tests/",
    "numpy/lib/tests/",
    "numpy/linalg/tests/",
    "numpy/ma/tests/",
    "numpy/polynomial/tests/",
    "numpy/random/tests/",
    "numpy/testing/",
    "numpy/tests/",
    "numpy/typing/tests/",
    "numpy/f2py/",
)
_ANDROID_ARCHIVE_ABIS = frozenset({"arm64-v8a", "armeabi-v7a", "x86", "x86_64"})
_ANDROID_FORBIDDEN_DISTRIBUTIONS = frozenset(
    {"llvmlite", "numba", "torch", "torchaudio", "torchvision"}
)


class _UnsupportedJitImportError(ValueError):
    """A selected Player script imports a compiler absent from Android."""


def _reject_android_jit_imports(python_sources: tuple[Path, ...]) -> None:
    """Check the same GUID-selected source closure used by the content cook."""
    unsupported: dict[str, set[str]] = {}
    for source_value in python_sources:
        source_path = Path(source_value).resolve()
        if not source_path.is_file() or source_path.suffix.casefold() != ".py":
            raise ValueError(
                f"Android Player Python source closure contains an invalid file: {source_path}"
            )
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"), str(source_path))
        except (OSError, UnicodeError, SyntaxError) as error:
            raise ValueError(
                f"Android dependency scan failed for {source_path}: {error}"
            ) from error
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = (alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = (node.module.split(".", 1)[0],)
            else:
                continue
            for name in names:
                if name in {"numba", "llvmlite"}:
                    unsupported.setdefault(name, set()).add(str(source_path))
    if unsupported:
        imports = ", ".join(sorted(unsupported))
        sources = ", ".join(sorted({path for paths in unsupported.values() for path in paths}))
        raise _UnsupportedJitImportError(
            f"Android Player cannot package direct {imports} imports in selected "
            f"scripts ({sources}): the Android runtime has no CPU JIT compiler. "
            "Use infernux.jit.compile for ordinary Python execution on Android."
        )


class AndroidPlatformExporter(PlatformExporter):
    @property
    def exporter_id(self) -> str:
        return "infernux/platform-android"

    def targets(self):
        return (
            BuildTarget(
                "android-x64-emulator",
                "Android x86_64 Emulator",
                "android",
                "x86_64",
                _ANDROID_CAPABILITIES,
            ),
            BuildTarget(
                "android-arm64",
                "Android arm64",
                "android",
                "arm64-v8a",
                _ANDROID_CAPABILITIES,
            ),
        )

    def build_options(self, target):
        if target.id not in {"android-arm64", "android-x64-emulator"}:
            raise ValueError(f"Unsupported Android build target: {target.id}")
        return (
            BuildOption(
                "android_artifact", "build.android_artifact", BuildOptionKind.ENUM,
                "apk",
                choices=(
                    BuildOptionChoice("apk", "APK"),
                    BuildOptionChoice("aab", "AAB"),
                ),
            ),
            BuildOption(
                "android_orientation", "build.android_orientation",
                BuildOptionKind.ENUM, "landscape",
                choices=(
                    BuildOptionChoice("landscape", "build.android_orientation_landscape"),
                    BuildOptionChoice("portrait", "build.android_orientation_portrait"),
                    BuildOptionChoice("sensor", "build.android_orientation_sensor"),
                ),
            ),
            BuildOption(
                "android_resolution_scaling", "build.android_resolution_scaling",
                BuildOptionKind.ENUM, "fixed_dpi",
                choices=(
                    BuildOptionChoice("fixed_dpi", "build.android_fixed_dpi"),
                    BuildOptionChoice("disabled", "build.android_native_resolution"),
                ),
            ),
            BuildOption(
                "android_target_dpi", "build.android_target_dpi",
                BuildOptionKind.INTEGER, 320, minimum=120, maximum=1000, step=10,
                visible_when={"android_resolution_scaling": "fixed_dpi"},
            ),
            BuildOption("display_mode", "Display mode", BuildOptionKind.ENUM,
                "fullscreen_borderless", choices=(
                    BuildOptionChoice("fullscreen_borderless", "Fullscreen"),
                ), editor_visible=False),
            BuildOption("window_width", "Reference width", BuildOptionKind.INTEGER,
                1280, minimum=1, editor_visible=False),
            BuildOption("window_height", "Reference height", BuildOptionKind.INTEGER,
                720, minimum=1, editor_visible=False),
            BuildOption("window_resizable", "Resizable", BuildOptionKind.BOOLEAN,
                False, editor_visible=False),
            BuildOption("android_keystore", "Android keystore", BuildOptionKind.PATH,
                "", editor_visible=False),
            BuildOption("android_key_alias", "Android key alias", BuildOptionKind.STRING,
                "", editor_visible=False),
            BuildOption("android_keystore_password_env", "Keystore password environment",
                BuildOptionKind.STRING, "", editor_visible=False),
            BuildOption("android_key_password_env", "Key password environment",
                BuildOptionKind.STRING, "", editor_visible=False),
            BuildOption("android_python_prefix", "Android Python prefix",
                BuildOptionKind.PATH, "", editor_visible=False),
            BuildOption("build_cache_root", "Build cache root", BuildOptionKind.PATH,
                "", editor_visible=False),
            BuildOption("android_numpy_wheel", "Android NumPy wheel",
                BuildOptionKind.PATH, "", editor_visible=False),
        )

    def doctor(self, request: BuildRequest) -> CapabilityReport:
        toolchain = inspect_android_toolchain(request.target)
        if not toolchain.available:
            return toolchain

        abi = (
            "x86_64"
            if request.target == "android-x64-emulator"
            else "arm64-v8a"
        )
        python_prefix = _python_prefix(request, abi)
        details = dict(toolchain.details)
        if python_prefix is None:
            suffix = "X86_64" if abi == "x86_64" else "ARM64"
            return CapabilityReport(
                False,
                (
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.python.runtime-missing",
                        "Install an Android CPython 3.13 runtime, then set "
                        "android_python_prefix in the build profile or "
                        f"INFERNUX_ANDROID_PYTHON_PREFIX_{suffix}.",
                        source=self.exporter_id,
                        detail={"abi": abi},
                    ),
                ),
                details,
            )
        details["python_prefix"] = str(python_prefix)
        try:
            validate_runtime_manifest(
                python_prefix,
                expected_abi=abi,
                expected_python_series=_ANDROID_PYTHON_SERIES,
                application_minimum_android_api=_ANDROID_MINIMUM_API,
            )
        except (OSError, ValueError) as error:
            return CapabilityReport(
                False,
                (
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.python.runtime-invalid",
                        str(error),
                        source=self.exporter_id,
                        detail={"abi": abi, "python_prefix": str(python_prefix)},
                    ),
                ),
                details,
            )
        try:
            payload = Path(__file__).with_name("player")
            native = inspect_native_payload(payload, abi=abi)
        except (OSError, ValueError) as error:
            return CapabilityReport(False, (
                BuildDiagnostic(DiagnosticSeverity.ERROR, "android.player.runtime-invalid",
                                str(error), source=self.exporter_id),
            ), details)
        details["player_payload"] = str(payload)
        details["native_configuration"] = native["configuration"]
        return CapabilityReport(True, toolchain.diagnostics, details)

    def create_plan(self, request: BuildRequest) -> BuildPlan:
        architecture = (
            "x86_64" if request.target == "android-x64-emulator" else "arm64-v8a"
        )
        return BuildPlan(
            request.target,
            (
                BuildStep("cook", "Cook project content", "cook"),
                BuildStep("imports", "Analyze Python imports", "analyze"),
                BuildStep(
                    "native",
                    f"Stage precompiled Android runtime ({architecture})",
                    "prepare",
                    {"abi": architecture},
                ),
                BuildStep("package", "Assemble Android package", "package"),
                BuildStep("audit", "Audit Android package", "audit"),
            ),
            {"abi": architecture, "graphics_api": "vulkan"},
        )

    def execute(self, request: BuildRequest, plan: BuildPlan) -> BuildResult:
        started = time.perf_counter()
        report = self.doctor(request)
        if not report.available:
            return BuildResult(
                request.target,
                False,
                diagnostics=report.diagnostics,
                manifest={"toolchain": dict(report.details)},
            )
        details = dict(report.details)
        engine_package = _engine_package()
        sdk_root = Path(str(details["sdk_root"]))
        abi = str(plan.metadata["abi"])
        output_root = Path(request.output_dir).resolve()
        staging = _android_staging_directory(request)
        signing_environment: dict[str, str] = {}
        signing_diagnostics: tuple[BuildDiagnostic, ...] = ()
        release_signed = False
        if request.profile.configuration is BuildConfiguration.RELEASE:
            try:
                signing_environment = _android_signing_environment(request)
            except ValueError as error:
                return BuildResult(
                    request.target,
                    False,
                    diagnostics=(
                        BuildDiagnostic(
                            DiagnosticSeverity.ERROR,
                            "android.signing.invalid",
                            str(error),
                            source=self.exporter_id,
                        ),
                    ),
                    elapsed_seconds=time.perf_counter() - started,
                )
            release_signed = bool(signing_environment)
            if not release_signed:
                signing_diagnostics = (
                    BuildDiagnostic(
                        DiagnosticSeverity.WARNING,
                        "android.signing.not-configured",
                        "Release package is unsigned. Configure Android signing "
                        "before store publication or device bundle installation.",
                        source=self.exporter_id,
                    ),
                )
        try:
            artifact_kind, configuration_name, source_artifact = (
                _android_artifact_plan(
                    request,
                    staging,
                    release_signed=release_signed,
                )
            )
        except ValueError as error:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.artifact.invalid",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                elapsed_seconds=time.perf_counter() - started,
            )
        request.report("prepare", 0, 1, "Preparing Android SDL host project")
        staging.parent.mkdir(parents=True, exist_ok=True)
        _stage_host_template(
            Path(__file__).with_name("templates") / "host",
            staging,
        )
        python_prefix = _python_prefix(request, abi)
        if python_prefix is None:
            suffix = "X86_64" if abi == "x86_64" else "ARM64"
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.python.runtime-missing",
                        "Provide an Android CPython prefix through the build profile option "
                        "android_python_prefix or "
                        f"INFERNUX_ANDROID_PYTHON_PREFIX_{suffix}.",
                        source=self.exporter_id,
                        detail={"abi": abi},
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                elapsed_seconds=time.perf_counter() - started,
            )
        try:
            python_version = _stage_python_runtime(request, staging, python_prefix, abi)
        except ValueError as error:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.python.runtime-invalid",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                elapsed_seconds=time.perf_counter() - started,
            )
        try:
            _stage_engine_python_package(request, staging, engine_package)
            _finalize_python_runtime_identity(staging)
            game_name, sdl_orientations, android_orientation = (
                _cook_player_content(request, staging, engine_package, abi)
            )
            resolution_scaling, target_dpi = _android_resolution_contract(request)
        except (OSError, RuntimeError, ValueError) as error:
            from infernux.engine.build.compute_aot import ComputeAotBuildError

            if isinstance(error, _UnsupportedJitImportError):
                return BuildResult(
                    request.target,
                    False,
                    diagnostics=(BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.python.jit-import-unsupported",
                        str(error),
                        source=self.exporter_id,
                    ),),
                    manifest={"abi": abi, "staging": str(staging)},
                    elapsed_seconds=time.perf_counter() - started,
                )
            if isinstance(error, ComputeAotBuildError):
                return BuildResult(
                    request.target,
                    False,
                    diagnostics=(BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.compute.aot-incomplete",
                        str(error),
                        source=self.exporter_id,
                        detail={"missing": error.missing},
                    ),),
                    manifest={"abi": abi, "staging": str(staging)},
                    elapsed_seconds=time.perf_counter() - started,
                )
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.player.cook-failed",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                elapsed_seconds=time.perf_counter() - started,
            )
        _configure_project(
            staging,
            sdk_root,
            abi,
            python_version=python_version,
            sdl_orientations=sdl_orientations,
            android_orientation=android_orientation,
            resolution_scaling=resolution_scaling,
            target_dpi=target_dpi,
            game_name=game_name,
        )
        request.report("prepare", 1, 1, "Android SDL host project ready")

        stage_native_payload(Path(str(details["player_payload"])), staging, abi=abi)
        request.report("native", 1, 1, f"Precompiled Android runtime staged ({abi})")

        gradle = Path(str(details["gradle"]))
        task_prefix = "bundle" if artifact_kind == "aab" else "assemble"
        task = f":app:{task_prefix}{configuration_name}"
        request.report("compile", 0, 1, f"Running Gradle {task}", abi=abi)
        return_code, gradle_logs = _run_command(
            request,
            [str(gradle), "-p", str(staging), "--console=plain", task],
            staging,
            _android_gradle_environment(
                request,
                {
                    **os.environ,
                    "ANDROID_SDK_ROOT": str(sdk_root),
                    "ANDROID_HOME": str(sdk_root),
                    "JAVA_HOME": str(details["java_home"]),
                    **signing_environment,
                },
            ),
            source="gradle",
        )
        logs = gradle_logs
        if return_code != 0:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.host.gradle",
                        f"Gradle failed with exit code {return_code}.",
                        source=self.exporter_id,
                    ),
                ),
                manifest={"abi": abi, "staging": str(staging)},
                logs=logs,
                elapsed_seconds=time.perf_counter() - started,
            )
        request.report("compile", 1, 1, "Android SDL host compiled", abi=abi)

        if not source_artifact.is_file():
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.host.artifact-missing",
                        f"Gradle completed without producing {source_artifact.name}.",
                        source=self.exporter_id,
                    ),
                ),
                logs=logs,
                elapsed_seconds=time.perf_counter() - started,
            )
        try:
            package_audit = _audit_android_archive(
                source_artifact,
                abi=abi,
                artifact_kind=artifact_kind,
            )
        except (OSError, ValueError) as error:
            return BuildResult(
                request.target,
                False,
                diagnostics=(
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "android.package.audit-failed",
                        str(error),
                        source=self.exporter_id,
                    ),
                ),
                logs=logs,
                elapsed_seconds=time.perf_counter() - started,
            )
        artifact_stem = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in game_name
        ).strip("_") or "InfernuxPlayer"
        configuration_slug = configuration_name.casefold()
        artifact_path = output_root / (
            f"{artifact_stem}-android-{abi}-{configuration_slug}.{artifact_kind}"
        )
        output_root.mkdir(parents=True, exist_ok=True)
        temporary = artifact_path.with_suffix(f".{artifact_kind}.tmp")
        shutil.copy2(source_artifact, temporary)
        os.replace(temporary, artifact_path)
        request.report(
            "package",
            1,
            1,
            f"Android SDL host {artifact_kind.upper()} published",
        )
        return BuildResult(
            request.target,
            True,
            artifacts=(
                BuildArtifact(
                    str(artifact_path),
                    artifact_kind,
                    size=artifact_path.stat().st_size,
                ),
            ),
            diagnostics=signing_diagnostics,
            manifest={
                "exporter": self.exporter_id,
                "abi": abi,
                "graphics_api": "vulkan",
                "scope": "cooked-player",
                "python": python_version,
                "game": game_name,
                "configuration": configuration_slug,
                "native_configuration": details["native_configuration"],
                "artifact_kind": artifact_kind,
                "signed": release_signed,
                "resolution_scaling": resolution_scaling,
                "target_dpi": target_dpi,
                "package_audit": package_audit,
            },
            logs=logs,
            elapsed_seconds=time.perf_counter() - started,
        )


def _audit_android_archive(
    artifact: Path,
    *,
    abi: str,
    artifact_kind: str,
) -> dict[str, object]:
    """Fail closed when an Android Player archive violates its runtime scope."""

    if artifact_kind not in {"apk", "aab"}:
        raise ValueError(f"Unsupported Android archive kind: {artifact_kind}")
    archive_prefix = "base/" if artifact_kind == "aab" else ""
    required_manifest = (
        "base/manifest/AndroidManifest.xml"
        if artifact_kind == "aab"
        else "AndroidManifest.xml"
    )
    try:
        with zipfile.ZipFile(artifact) as archive:
            entries = tuple(
                entry.filename.replace("\\", "/")
                for entry in archive.infolist()
                if not entry.is_dir()
            )
    except zipfile.BadZipFile as error:
        raise ValueError(f"Android {artifact_kind.upper()} is unreadable: {artifact}") from error

    if required_manifest not in entries:
        raise ValueError(
            f"Android {artifact_kind.upper()} is missing {required_manifest}: {artifact}"
        )

    native_prefix = f"{archive_prefix}lib/"
    native_entries = tuple(
        entry
        for entry in entries
        if entry.startswith(native_prefix) and entry.endswith(".so")
    )
    packaged_abis = {
        entry[len(native_prefix) :].split("/", 1)[0]
        for entry in native_entries
        if "/" in entry[len(native_prefix) :]
    }
    unexpected_abis = sorted(
        packaged_abi
        for packaged_abi in packaged_abis
        if packaged_abi in _ANDROID_ARCHIVE_ABIS and packaged_abi != abi
    )
    if unexpected_abis:
        raise ValueError(
            "Android archive contains native libraries for unexpected ABIs: "
            + ", ".join(unexpected_abis)
        )
    expected_native_entries = tuple(
        entry for entry in native_entries if entry.startswith(f"{native_prefix}{abi}/")
    )
    if not expected_native_entries:
        raise ValueError(f"Android archive contains no native libraries for {abi}")

    forbidden_entries: list[str] = []
    for entry in entries:
        parts = entry.casefold().split("/")
        try:
            package_index = parts.index("site-packages") + 1
        except ValueError:
            continue
        if package_index >= len(parts):
            continue
        top_level = parts[package_index]
        if any(
            top_level == distribution
            or top_level.startswith(f"{distribution}-")
            or top_level.startswith(f"{distribution}.")
            for distribution in _ANDROID_FORBIDDEN_DISTRIBUTIONS
        ):
            forbidden_entries.append(entry)
    if forbidden_entries:
        raise ValueError(
            "Android Player contains host-only Python distributions: "
            + ", ".join(sorted(forbidden_entries)[:8])
        )

    return {
        "native_library_count": len(expected_native_entries),
        "packaged_abis": [abi],
        "forbidden_distribution_count": 0,
    }


def _android_artifact_plan(
    request: BuildRequest,
    staging: Path,
    *,
    release_signed: bool = False,
) -> tuple[str, str, Path]:
    """Select the Gradle publication contract for one build request."""

    configured = str(
        request.profile.options.get("android_artifact", "") or ""
    ).strip().casefold()
    if not configured:
        configured = (
            "apk"
            if request.profile.configuration is BuildConfiguration.DEVELOPMENT
            else "aab"
        )
    if configured not in {"apk", "aab"}:
        raise ValueError("android_artifact must be apk or aab")

    configuration_name = (
        "Debug"
        if request.profile.configuration is BuildConfiguration.DEVELOPMENT
        else "Release"
    )
    configuration_slug = configuration_name.casefold()
    if configured == "aab":
        source = (
            staging
            / "app"
            / "build"
            / "outputs"
            / "bundle"
            / configuration_slug
            / f"app-{configuration_slug}.aab"
        )
    else:
        apk_name = (
            "app-debug.apk"
            if configuration_name == "Debug"
            else (
                "app-release.apk"
                if release_signed
                else "app-release-unsigned.apk"
            )
        )
        source = (
            staging
            / "app"
            / "build"
            / "outputs"
            / "apk"
            / configuration_slug
            / apk_name
        )
    return configured, configuration_name, source


def _android_signing_environment(request: BuildRequest) -> dict[str, str]:
    """Resolve release signing without serializing credentials into staging."""

    options = request.profile.options
    keystore = str(
        options.get("android_keystore", "")
        or os.environ.get("INFERNUX_ANDROID_KEYSTORE", "")
    ).strip()
    key_alias = str(
        options.get("android_key_alias", "")
        or os.environ.get("INFERNUX_ANDROID_KEY_ALIAS", "")
    ).strip()
    store_password_name = str(
        options.get("android_keystore_password_env", "")
        or "INFERNUX_ANDROID_KEYSTORE_PASSWORD"
    ).strip()
    key_password_name = str(
        options.get("android_key_password_env", "")
        or "INFERNUX_ANDROID_KEY_PASSWORD"
    ).strip()
    store_password = os.environ.get(store_password_name, "")
    configured_key_password = os.environ.get(key_password_name, "")
    key_password = configured_key_password or store_password

    supplied = bool(
        keystore or key_alias or store_password or configured_key_password
    )
    if not supplied:
        return {}
    missing: list[str] = []
    if not keystore:
        missing.append("android_keystore or INFERNUX_ANDROID_KEYSTORE")
    if not key_alias:
        missing.append("android_key_alias or INFERNUX_ANDROID_KEY_ALIAS")
    if not store_password:
        missing.append(store_password_name)
    if missing:
        raise ValueError(
            "Android release signing is partially configured; missing "
            + ", ".join(missing)
        )
    keystore_path = Path(keystore).expanduser().resolve()
    if not keystore_path.is_file():
        raise ValueError(f"Android keystore does not exist: {keystore_path}")
    return {
        "INFERNUX_ANDROID_KEYSTORE": str(keystore_path),
        "INFERNUX_ANDROID_KEY_ALIAS": key_alias,
        "INFERNUX_ANDROID_KEYSTORE_PASSWORD": store_password,
        "INFERNUX_ANDROID_KEY_PASSWORD": key_password,
    }


def _configure_project(
    project_root: Path,
    sdk_root: Path,
    abi: str,
    *,
    python_version: str = _ANDROID_PYTHON_SERIES,
    sdl_orientations: str = "LandscapeLeft LandscapeRight",
    android_orientation: str = "sensorLandscape",
    resolution_scaling: str = "fixed_dpi",
    target_dpi: int = 320,
    game_name: str = "Infernux Player",
) -> None:
    application_id = _android_application_id(game_name)
    replacements = {
        "@ANDROID_ABI@": abi,
        "@ANDROID_BUILD_TOOLS_VERSION@": ANDROID_BUILD_TOOLS,
        "@ANDROID_NDK_VERSION@": ANDROID_NDK,
        "@ANDROID_GRADLE_PLUGIN_VERSION@": ANDROID_GRADLE_PLUGIN,
        "@ANDROID_PYTHON_VERSION@": python_version,
        "@ANDROID_ORIENTATIONS@": sdl_orientations,
        "@ANDROID_SCREEN_ORIENTATION@": android_orientation,
        "@ANDROID_RESOLUTION_SCALING@": resolution_scaling,
        "@ANDROID_TARGET_DPI@": str(target_dpi),
        "@ANDROID_APP_NAME@": xml_escape(game_name, {'"': "&quot;", "'": "&apos;"}),
        "@ANDROID_APPLICATION_ID@": application_id,
    }
    for path in project_root.rglob("*.in"):
        payload = path.read_text(encoding="utf-8")
        for marker, value in replacements.items():
            payload = payload.replace(marker, value)
        destination = path.with_suffix("")
        destination.write_text(payload, encoding="utf-8", newline="\n")
        path.unlink()
    escaped_sdk = str(sdk_root).replace("\\", "\\\\").replace(":", "\\:")
    (project_root / "local.properties").write_text(
        f"sdk.dir={escaped_sdk}\n",
        encoding="utf-8",
        newline="\n",
    )


def _android_application_id(game_name: str) -> str:
    """Return the stable Android identity derived from the authored game name."""
    product = "".join(character.lower() for character in str(game_name) if character.isascii() and character.isalnum())
    if not product:
        raise ValueError("Android game_name must contain at least one ASCII letter or digit")
    if product[0].isdigit():
        product = "game" + product
    return f"com.infernux.{product}"


def _engine_package() -> Path:
    import infernux

    return Path(infernux.__file__).resolve().parent


def _stage_engine_python_package(
    request: BuildRequest,
    staging: Path,
    source_package: Path,
) -> None:
    """Stage shared Player Python modules from the installed engine package."""
    if not (source_package / "engine" / "platform_player_bootstrap.py").is_file():
        raise ValueError(f"Infernux Player Python sources are incomplete: {source_package}")
    site_packages = staging / "app" / "src" / "main" / "assets" / "python" / "site-packages"
    destination = site_packages / "infernux"
    request.report("analyze", 0, 2, "Staging Infernux Android Player modules")
    shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(
        source_package,
        destination,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "*.pyi",
            "*.pyd",
            "*.dll",
            "*.dylib",
            "*.so",
            "*.lib",
            "*.exp",
            "*.obj",
            "_runtime_modules",
            "_runtime_packs",
            "official_packages",
            "player_runtime",
            "project_templates",
            "test",
        ),
    )
    shutil.rmtree(
        destination / "_compiler" / "taichi" / "_vendor",
        ignore_errors=True,
    )

    packaging_spec = importlib.util.find_spec("packaging")
    packaging_source = (
        Path(str(packaging_spec.origin)).resolve().parent
        if packaging_spec is not None and packaging_spec.origin
        else None
    )
    if packaging_source is None or not (packaging_source / "__init__.py").is_file():
        raise ValueError("The Android Player staging environment has no packaging module")
    packaging_destination = site_packages / "packaging"
    shutil.rmtree(packaging_destination, ignore_errors=True)
    shutil.copytree(
        packaging_source,
        packaging_destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyi"),
    )
    _compile_android_runtime_bytecode(site_packages)
    request.report("analyze", 2, 2, "Android Player Python modules staged")


def _compile_android_runtime_bytecode(site_packages: Path) -> None:
    """Precompile the fixed Android CPython runtime import closure."""

    host_series = f"{sys.version_info.major}.{sys.version_info.minor}"
    if host_series != _ANDROID_PYTHON_SERIES:
        raise ValueError(
            "Android Player bytecode must be built by CPython "
            f"{_ANDROID_PYTHON_SERIES}, but the exporter is running {host_series}"
        )

    roots = (
        site_packages / "infernux",
        site_packages / "packaging",
    )
    sources = sorted(
        (
            source
            for root in roots
            for source in ([root] if root.is_file() else root.rglob("*.py"))
        ),
        key=lambda path: path.as_posix().casefold(),
    )
    for source in sources:
        relative = source.relative_to(site_packages).as_posix()
        try:
            py_compile.compile(
                str(source),
                cfile=importlib.util.cache_from_source(str(source)),
                dfile=relative,
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )
        except py_compile.PyCompileError as error:
            raise ValueError(
                f"Android Player runtime bytecode compilation failed: {relative}"
            ) from error


def _stage_host_template(source: Path, staging: Path) -> None:
    """Copy build templates without project AssetDatabase sidecars.

    Installed plugins live under ``Packages`` and therefore receive ``.meta``
    identity files.  Those sidecars are meaningful to Infernux, but Android's
    resource merger treats every file below ``res`` as a platform resource.
    Ignore new sidecars and prune stale copies left in the reusable host cache.
    """

    obsolete_cpp = staging / "app/src/main/cpp"
    if obsolete_cpp.exists():
        shutil.rmtree(obsolete_cpp)
    shutil.copytree(
        source,
        staging,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.meta"),
    )
    for sidecar in staging.rglob("*.meta"):
        sidecar.unlink()


def _cook_player_content(
    request: BuildRequest,
    staging: Path,
    engine_package: Path,
    abi: str,
) -> tuple[str, str, str]:
    """Run the shared GUID-based Player cook and stage its native package."""

    from infernux.engine.platform_content_cook import (
        build_settings_for_request,
        cook_platform_content,
        read_cooked_player_icon,
    )

    settings = build_settings_for_request(request)
    game_name = str(settings["game_name"]).strip() or Path(request.project_root).name
    application_id = _android_application_id(game_name)
    sdl_orientations, android_orientation = _android_orientation_contract(
        request,
        settings,
    )
    cook_root = staging / ".infernux-player-cook"
    shutil.rmtree(cook_root, ignore_errors=True)
    cook_root.mkdir(parents=True)
    try:
        cooked = cook_platform_content(
            request,
            cook_root,
            platform_host={
                "identity": "android-sdl-python-player-host",
                "entry_point": f"{application_id}/com.infernux.bootstrap.InfernuxActivity",
                "platform": "android",
                "architecture": abi,
            },
            gpu_compute_aot=True,
        )
        _reject_android_jit_imports(cooked.python_sources)
        cooked_data = cooked.data_directory
        player_assets = staging / "app" / "src" / "main" / "assets" / "player"
        shutil.rmtree(player_assets, ignore_errors=True)
        player_assets.mkdir(parents=True)
        staged_data = player_assets / cooked_data.name
        shutil.copytree(cooked_data, staged_data)
        (player_assets / "infernux-data-root.txt").write_text(
            cooked_data.name + "\n",
            encoding="utf-8",
            newline="\n",
        )
        icon = read_cooked_player_icon(
            staged_data,
            default_icon=(
                engine_package
                / "resources"
                / "icons"
                / "icon.png"
            ),
        )
        _stage_android_launcher_icons(staging, icon)

        digest = hashlib.sha256(b"INFERNUX_ANDROID_PLAYER_ASSETS\n")
        for path in sorted(
            (item for item in player_assets.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix().casefold(),
        ):
            relative = path.relative_to(player_assets).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        (player_assets / "infernux-content.id").write_text(
            digest.hexdigest() + "\n",
            encoding="ascii",
            newline="\n",
        )
    finally:
        shutil.rmtree(cook_root, ignore_errors=True)
    return cooked.game_name, sdl_orientations, android_orientation


def _stage_android_launcher_icons(staging: Path, source_icon: bytes) -> None:
    """Generate legacy and adaptive launcher icons from one cooked project icon."""

    try:
        from PIL import Image, ImageOps
    except ImportError as error:
        raise ValueError(
            "Pillow is required to generate Android launcher icons"
        ) from error

    try:
        from io import BytesIO

        with Image.open(BytesIO(source_icon)) as opened:
            source = opened.convert("RGBA")
    except (OSError, ValueError) as error:
        raise ValueError("Cooked Android launcher icon is unreadable") from error

    resources = staging / "app" / "src" / "main" / "res"
    densities = {
        "mdpi": 48,
        "hdpi": 72,
        "xhdpi": 96,
        "xxhdpi": 144,
        "xxxhdpi": 192,
    }
    for density, size in densities.items():
        destination = resources / f"mipmap-{density}" / "infernux_launcher.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        contained = ImageOps.contain(
            source,
            (size, size),
            method=Image.Resampling.LANCZOS,
        )
        canvas.alpha_composite(
            contained,
            ((size - contained.width) // 2, (size - contained.height) // 2),
        )
        canvas.save(destination, format="PNG", optimize=True)

    # Android masks adaptive icons aggressively.  Keep the authored image
    # inside the documented central safe zone while the platform owns shape.
    foreground_size = 432
    foreground = Image.new("RGBA", (foreground_size, foreground_size), (0, 0, 0, 0))
    contained = ImageOps.contain(
        source,
        (264, 264),
        method=Image.Resampling.LANCZOS,
    )
    foreground.alpha_composite(
        contained,
        (
            (foreground_size - contained.width) // 2,
            (foreground_size - contained.height) // 2,
        ),
    )
    foreground_path = (
        resources / "drawable-nodpi" / "infernux_launcher_foreground.png"
    )
    foreground_path.parent.mkdir(parents=True, exist_ok=True)
    foreground.save(foreground_path, format="PNG", optimize=True)


def _android_orientation_contract(
    request: BuildRequest,
    settings: dict[str, object],
) -> tuple[str, str]:
    """Resolve one SDL/Activity orientation policy for this Android build."""

    configured = str(
        request.profile.options.get("android_orientation", "landscape") or "landscape"
    ).strip().casefold()
    policies = {
        "landscape": ("LandscapeLeft LandscapeRight", "sensorLandscape"),
        "portrait": ("Portrait PortraitUpsideDown", "sensorPortrait"),
        "sensor": (
            "LandscapeLeft LandscapeRight Portrait PortraitUpsideDown",
            "fullUser",
        ),
    }
    try:
        return policies[configured]
    except KeyError as error:
        raise ValueError(
            "android_orientation must be landscape, portrait, or sensor"
        ) from error


def _android_resolution_contract(request: BuildRequest) -> tuple[str, int]:
    """Resolve Unity-style native or fixed-DPI mobile resolution scaling."""

    mode = str(
        request.profile.options.get("android_resolution_scaling", "fixed_dpi")
        or "fixed_dpi"
    ).strip().casefold()
    if mode not in {"disabled", "fixed_dpi"}:
        raise ValueError(
            "android_resolution_scaling must be disabled or fixed_dpi"
        )
    raw_target = request.profile.options.get("android_target_dpi", 320)
    try:
        target_dpi = int(raw_target)
    except (TypeError, ValueError) as error:
        raise ValueError("android_target_dpi must be an integer") from error
    if target_dpi < 120 or target_dpi > 1000:
        raise ValueError("android_target_dpi must be between 120 and 1000")
    return mode, target_dpi


def _python_prefix(request: BuildRequest, abi: str) -> Path | None:
    configured = str(request.profile.options.get("android_python_prefix", "") or "").strip()
    if not configured:
        suffix = "X86_64" if abi == "x86_64" else "ARM64"
        configured = os.environ.get(
            f"INFERNUX_ANDROID_PYTHON_PREFIX_{suffix}", ""
        ).strip()
    if not configured:
        return None
    prefix = Path(configured).expanduser().resolve()
    return prefix if prefix.is_dir() else None


def _android_build_cache_root(request: BuildRequest) -> Path:
    configured = str(request.profile.options.get("build_cache_root", "") or "").strip()
    if not configured:
        configured = os.environ.get("INFERNUX_BUILD_CACHE_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(request.project_root).resolve() / "Cache" / "Build"


def _android_staging_directory(request: BuildRequest) -> Path:
    return _android_build_cache_root(request) / "AndroidHost" / str(request.target)


def _stage_python_runtime(
    request: BuildRequest,
    staging: Path,
    prefix: Path,
    abi: str,
) -> str:
    manifest = validate_runtime_manifest(
        prefix,
        expected_abi=abi,
        expected_python_series=_ANDROID_PYTHON_SERIES,
        application_minimum_android_api=_ANDROID_MINIMUM_API,
    )
    include_roots = sorted((prefix / "include").glob("python*"))
    library_roots = sorted(
        path
        for path in (prefix / "lib").glob("python*")
        if path.is_dir() and (path / "encodings").is_dir()
    )
    if not include_roots or not library_roots:
        raise ValueError(f"Android Python prefix is incomplete: {prefix}")
    version = library_roots[0].name.removeprefix("python")
    if version != _ANDROID_PYTHON_SERIES:
        raise ValueError(
            "Android Player requires CPython "
            f"{_ANDROID_PYTHON_SERIES}, but the configured prefix provides {version}: "
            f"{prefix}"
        )
    runtime_version = str(manifest["cpython"]["version"])
    if not runtime_version.startswith(version + "."):
        raise ValueError(
            f"Android Python prefix layout provides {version}, but its manifest "
            f"declares {runtime_version}: {prefix}"
        )
    include_root = prefix / "include" / f"python{version}"
    library_root = prefix / "lib" / f"python{version}"
    if not (include_root / "Python.h").is_file() or not library_root.is_dir():
        raise ValueError(f"Android Python {version} prefix is inconsistent: {prefix}")
    python_library = prefix / "lib" / f"libpython{version}.so"
    if not python_library.is_file():
        raise ValueError(f"Android Python {version} shared library is missing: {prefix}")

    staged_python = staging / "app" / "src" / "main" / "assets" / "python"
    obsolete_headers = staging / "app/src/main/python"
    if obsolete_headers.exists():
        shutil.rmtree(obsolete_headers)
    staged_library = staged_python / "lib" / f"python{version}"
    native_library = staging / "app" / "src" / "main" / "jniLibs" / abi
    request.report("python-runtime", 1, 4, f"Staging Android Python {version} standard library")
    shutil.rmtree(staged_python, ignore_errors=True)
    shutil.copytree(
        library_root,
        staged_library,
        ignore=shutil.ignore_patterns(*_ANDROID_STDLIB_IGNORES),
    )
    request.report("python-runtime", 2, 4, f"Staging Android Python {version} native libraries")
    native_library.mkdir(parents=True, exist_ok=True)
    for stale_library in (*native_library.glob("libpython*.so"), *native_library.glob("lib*_python.so")):
        stale_library.unlink()
    required_sidecars = _required_python_sidecar_libraries(library_root)
    missing_sidecars = [
        name for name in required_sidecars if not (prefix / "lib" / name).is_file()
    ]
    if missing_sidecars:
        raise ValueError(
            f"Android Python {version} prefix is missing native dependencies "
            f"required by its extension modules: {', '.join(missing_sidecars)}"
        )
    runtime_libraries = (
        python_library,
        *sorted((prefix / "lib").glob("lib*_python.so")),
    )
    for library in runtime_libraries:
        shutil.copy2(library, native_library / library.name)

    request.report("python-runtime", 3, 4, "Staging Android NumPy runtime")
    numpy_wheel = _find_android_numpy_wheel(request, prefix, abi, version)
    site_packages = staged_python / "site-packages"
    _extract_wheel(
        numpy_wheel,
        site_packages,
        excluded_prefixes=_NUMPY_RUNTIME_EXCLUDED_PREFIXES,
    )

    runtime_identity = _python_runtime_identity(
        prefix,
        library_root,
        (*runtime_libraries, numpy_wheel),
        version,
        abi,
    )
    (staged_python / "infernux-runtime.id").write_text(
        runtime_identity + "\n",
        encoding="utf-8",
        newline="\n",
    )

    (staging / "app" / "src" / "main" / "python-runtime.properties").write_text(
        f"version={version}\nabi={abi}\n",
        encoding="utf-8",
        newline="\n",
    )
    request.report("python-runtime", 4, 4, f"Android Python {version} runtime staged")
    return version


def _required_python_sidecar_libraries(library_root: Path) -> tuple[str, ...]:
    """Find Android sidecar libraries named by CPython extension binaries."""
    dependency_pattern = re.compile(rb"lib[A-Za-z0-9_.+-]+_python\.so")
    dependencies: set[str] = set()
    for extension in library_root.rglob("*.so"):
        try:
            payload = extension.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"Unable to inspect Android Python extension dependencies: {extension}"
            ) from exc
        dependencies.update(
            match.decode("ascii") for match in dependency_pattern.findall(payload)
        )
    return tuple(sorted(dependencies))


def _find_android_numpy_wheel(
    request: BuildRequest,
    prefix: Path,
    abi: str,
    python_version: str,
) -> Path:
    configured = str(
        request.profile.options.get("android_numpy_wheel", "") or ""
    ).strip()
    if not configured:
        suffix = "X86_64" if abi == "x86_64" else "ARM64"
        configured = os.environ.get(
            f"INFERNUX_ANDROID_NUMPY_WHEEL_{suffix}", ""
        ).strip()

    candidates = (
        [Path(configured).expanduser().resolve()]
        if configured
        else sorted((prefix / "wheels").glob("numpy-*.whl"))
    )
    expected_interpreter = "cp" + python_version.replace(".", "")
    expected_architecture = "x86_64" if abi == "x86_64" else "arm64_v8a"
    compatible: list[tuple[object, Path]] = []
    for wheel in candidates:
        if not wheel.is_file():
            continue
        try:
            distribution, package_version, _build, tags = parse_wheel_filename(
                wheel.name
            )
        except InvalidWheelFilename:
            continue
        if distribution != "numpy":
            continue
        if any(
            tag.interpreter == expected_interpreter
            and tag.abi == expected_interpreter
            and tag.platform.startswith("android_")
            and tag.platform.endswith("_" + expected_architecture)
            for tag in tags
        ):
            compatible.append((package_version, wheel))
    if not compatible:
        raise ValueError(
            "Android Player requires a NumPy wheel matching "
            f"{expected_interpreter} and {abi}. Place it in {prefix / 'wheels'} "
            "or configure android_numpy_wheel."
        )
    return max(compatible, key=lambda item: item[0])[1]


def _extract_wheel(
    wheel: Path,
    destination: Path,
    *,
    excluded_prefixes: tuple[str, ...] = (),
) -> None:
    """Extract one validated wheel without permitting links or path traversal."""

    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    try:
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                normalized = entry.filename.replace("\\", "/")
                if entry.is_dir():
                    normalized = normalized.rstrip("/")
                parts = normalized.split("/")
                if (
                    not normalized
                    or normalized.startswith("/")
                    or any(part in {"", ".", ".."} for part in parts)
                    or ((entry.external_attr >> 16) & 0o170000) == 0o120000
                ):
                    raise ValueError(
                        f"Android Python wheel contains an unsafe entry: {entry.filename}"
                    )
                target = destination.joinpath(*parts).resolve()
                if not target.is_relative_to(destination_root):
                    raise ValueError(
                        f"Android Python wheel escapes site-packages: {entry.filename}"
                    )
                if any(
                    normalized == prefix.rstrip("/")
                    or normalized.startswith(prefix)
                    for prefix in excluded_prefixes
                ):
                    continue
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"Android Python wheel is unreadable: {wheel}") from error


def _python_runtime_identity(
    prefix: Path,
    library_root: Path,
    runtime_libraries: tuple[Path, ...],
    version: str,
    abi: str,
) -> str:
    """Fingerprint the packaged runtime without rereading hundreds of MB of payload."""

    digest = hashlib.sha256(
        (
            f"python={version}\n"
            f"abi={abi}\n"
            "stdlib-ignore=" + "\0".join(_ANDROID_STDLIB_IGNORES) + "\n"
            "numpy-exclude=" + "\0".join(_NUMPY_RUNTIME_EXCLUDED_PREFIXES) + "\n"
        ).encode("utf-8")
    )
    paths = [path for path in library_root.rglob("*") if path.is_file()]
    paths.extend(runtime_libraries)
    for path in sorted(paths, key=lambda item: item.as_posix().casefold()):
        stat = path.stat()
        try:
            relative = path.relative_to(prefix).as_posix()
        except ValueError:
            relative = path.name
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def _finalize_python_runtime_identity(staging: Path) -> str:
    """Include staged engine modules in the Android Python cache identity."""

    staged_python = staging / "app" / "src" / "main" / "assets" / "python"
    identity_path = staged_python / "infernux-runtime.id"
    if not identity_path.is_file():
        raise ValueError("Android Python runtime identity is missing before finalization")

    digest = hashlib.sha256(b"INFERNUX_ANDROID_PYTHON_ASSETS\n")
    digest.update(identity_path.read_bytes().strip() + b"\n")
    site_packages = staged_python / "site-packages"
    for package_name in ("infernux", "packaging"):
        package_root = site_packages / package_name
        if not package_root.is_dir():
            raise ValueError(
                f"Android Player Python package is missing before finalization: {package_root}"
            )
        for path in sorted(
            (item for item in package_root.rglob("*") if item.is_file()),
            key=lambda item: item.as_posix().casefold(),
        ):
            relative = path.relative_to(site_packages).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)

    identity = digest.hexdigest()
    identity_path.write_text(identity + "\n", encoding="ascii", newline="\n")
    return identity


def _android_gradle_environment(
    request: BuildRequest, environment: dict[str, str]
) -> dict[str, str]:
    """Keep reusable tool data with Hub, or with a standalone source project."""
    result = dict(environment)
    shared = result.get("INFERNUX_SHARED_DATA_ROOT", "").strip()
    owner = (
        Path(os.path.expandvars(os.path.expanduser(shared))).resolve()
        if shared else Path(request.project_root).resolve()
    )
    if not result.get("GRADLE_USER_HOME", "").strip():
        result["GRADLE_USER_HOME"] = str(owner / "Cache/Gradle")
    if not result.get("ANDROID_USER_HOME", "").strip():
        # Includes debug.keystore: deleting reusable caches must not change
        # the signing identity and break updates of an installed debug APK.
        result["ANDROID_USER_HOME"] = str(owner / "State/Android")
    return result


def _run_command(
    request: BuildRequest,
    command: list[str],
    working_directory: Path,
    environment: dict[str, str],
    *,
    source: str,
) -> tuple[int, tuple[str, ...]]:
    """Run one build tool with live, cancellable output for every frontend."""

    process = subprocess.Popen(
        command,
        cwd=str(working_directory),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    logs: list[str] = []
    try:
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip()
                if not line:
                    continue
                logs.append(line)
                request.report(
                    "compile",
                    0,
                    0,
                    line[:500],
                    source=source,
                )
        return process.wait(), tuple(logs)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()


__all__ = ["AndroidPlatformExporter"]
