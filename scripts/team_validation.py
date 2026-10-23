"""GitHub-only central runner for team-owned Python applications."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "aegis-team-validation"
SOURCE, REPORTS = WORK / "source", WORK / "reports"
TRANSFER = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "aegis-team-transfer"
PLATFORM = Path(os.environ.get("AEGIS_PLATFORM_DIRECTORY", ".")).resolve()


def run(args: list[str]) -> None:
    subprocess.run(args, check=True, timeout=1200)


def tree_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(SOURCE.rglob("*")):
        if path.is_symlink():
            raise ValueError("Source symlinks require an adapter review")
        if path.is_file():
            digest.update(
                str(path.relative_to(SOURCE)).encode() + b"\0" + path.read_bytes()
            )
    return digest.hexdigest()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity() -> dict[str, str]:
    return {
        "source_commit": os.environ["GITHUB_SHA"],
        "repository": os.environ["GITHUB_REPOSITORY"],
        "central_commit": os.environ["CENTRAL_SHA"],
        "pipeline_version": os.environ["PIPELINE_VERSION"],
        "workflow_run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
    }


def output(name: str, value: str) -> None:
    if filename := os.environ.get("GITHUB_OUTPUT"):
        with Path(filename).open("a") as stream:
            stream.write(f"{name}={value}\n")


def extract_source(archive: Path) -> None:
    SOURCE.mkdir()
    with tarfile.open(archive) as tar:
        if any(
            not (m.isfile() or m.isdir())
            or Path(m.name).is_absolute()
            or ".." in Path(m.name).parts
            for m in tar.getmembers()
        ):
            raise ValueError("Only regular, contained source files are supported")
        tar.extractall(SOURCE, filter="data")


def capture_source() -> None:
    WORK.mkdir()
    REPORTS.mkdir()
    transfer = TRANSFER / "source"
    transfer.mkdir(parents=True)
    archive = transfer / "source.tar"
    with archive.open("wb") as stream:
        subprocess.run(
            ["git", "archive", "HEAD"], stdout=stream, check=True, timeout=120
        )
    extract_source(archive)
    source_hash = tree_hash()
    metadata = {
        **identity(),
        "source_sha256": source_hash,
        "archive_sha256": digest(archive),
    }
    (transfer / "metadata.json").write_text(json.dumps(metadata))
    (WORK / "source-sha256").write_text(source_hash)
    (REPORTS / "source").mkdir()
    (REPORTS / "source/identity.json").write_text(json.dumps(metadata))
    output("source_digest", source_hash)


def restore_source() -> None:
    transfer = TRANSFER / "source"
    metadata = json.loads((transfer / "metadata.json").read_text())
    if any(metadata.get(key) != value for key, value in identity().items()):
        raise ValueError("Source artifact belongs to another run identity")
    expected = os.environ["EXPECTED_SOURCE_DIGEST"]
    if (
        not re.fullmatch(r"[a-f0-9]{64}", expected)
        or metadata["source_sha256"] != expected
    ):
        raise ValueError("Source artifact digest differs from the Source job output")
    archive = transfer / "source.tar"
    if digest(archive) != metadata["archive_sha256"]:
        raise ValueError("Source archive integrity changed")
    WORK.mkdir()
    REPORTS.mkdir()
    extract_source(archive)
    if tree_hash() != expected:
        raise ValueError("Restored source integrity changed")
    (WORK / "source-sha256").write_text(expected)


def prepare_image(directory: str) -> None:
    context = WORK / "image"
    context.mkdir()
    for source, name in (
        (PLATFORM / "runner/team.Dockerfile", "Dockerfile"),
        (PLATFORM / "runner/team_stage.py", "stage.py"),
        (SOURCE / directory / "requirements-ci.txt", "requirements-ci.txt"),
        (PLATFORM / "policies/scanners.lock", "scanners.lock"),
    ):
        shutil.copy2(source, context / name)
    with urllib.request.urlopen(
        "https://github.com/gitleaks/gitleaks/releases/download/v8.28.0/gitleaks_8.28.0_linux_x64.tar.gz",
        timeout=60,
    ) as response:
        data = response.read(30_000_000)
    if (
        hashlib.sha256(data).hexdigest()
        != "a65b5253807a68ac0cafa4414031fd740aeb55f54fb7e55f386acb52e6a840eb"
    ):
        raise ValueError("Scanner checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        member = tar.getmember("gitleaks")
        if not member.isfile():
            raise ValueError("Invalid scanner release")
        stream = tar.extractfile(member)
        assert stream is not None
        (context / "gitleaks").write_bytes(stream.read())
    run(
        [
            "docker",
            "build",
            "--build-arg",
            "BASE_IMAGE=python:3.12.12-slim",
            "-t",
            "aegis-team-validation:run",
            str(context),
        ]
    )
    image = subprocess.check_output(
        [
            "docker",
            "image",
            "inspect",
            "aegis-team-validation:run",
            "--format",
            "{{.Id}}",
        ],
        text=True,
    ).strip()
    (WORK / "image").joinpath("id").write_text(image)
    if tree_hash() != (WORK / "source-sha256").read_text():
        raise ValueError("Source integrity changed during runner preparation")


def export_image() -> None:
    image = (WORK / "image/id").read_text()
    transfer = TRANSFER / "image"
    transfer.mkdir(parents=True)
    archive = transfer / "image.tar"
    run(["docker", "save", "--output", str(archive), image])
    run(["gzip", "-1", str(archive)])
    archive = transfer / "image.tar.gz"
    archive_hash = digest(archive)
    metadata = {
        **identity(),
        "image": image,
        "archive_sha256": archive_hash,
        "source_sha256": (WORK / "source-sha256").read_text(),
    }
    (transfer / "metadata.json").write_text(json.dumps(metadata))
    output("image_id", image)
    output("image_digest", archive_hash)


def restore_image() -> None:
    transfer = TRANSFER / "image"
    metadata = json.loads((transfer / "metadata.json").read_text())
    image, expected = (
        os.environ["EXPECTED_IMAGE_ID"],
        os.environ["EXPECTED_IMAGE_DIGEST"],
    )
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image) or not re.fullmatch(
        r"[a-f0-9]{64}", expected
    ):
        raise ValueError("Missing immutable image identity from Build job")
    if any(metadata.get(key) != value for key, value in identity().items()):
        raise ValueError("Runner artifact belongs to another run identity")
    if metadata.get("source_sha256") != (WORK / "source-sha256").read_text():
        raise ValueError("Runner artifact belongs to another source snapshot")
    archive = transfer / "image.tar.gz"
    if (
        metadata.get("image") != image
        or metadata.get("archive_sha256") != expected
        or digest(archive) != expected
    ):
        raise ValueError("Runner artifact integrity changed")
    run(["docker", "load", "--input", str(archive)])
    actual = subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    if actual != image:
        raise ValueError("Restored runner image does not match Build job")
    (WORK / "image").mkdir()
    (WORK / "image/id").write_text(image)


def stage(name: str, directory: str, limit: int) -> None:
    if tree_hash() != (WORK / "source-sha256").read_text():
        raise ValueError("Source integrity changed before stage execution")
    out = REPORTS / name
    out.mkdir(mode=0o777)
    out.chmod(0o777)
    source = SOURCE
    if name == "dependencies":
        source = WORK / "lock-only"
        (source / directory).mkdir(parents=True)
        shutil.copy2(
            SOURCE / directory / "requirements-ci.txt",
            source / directory / "requirements-ci.txt",
        )
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "bridge" if name == "dependencies" else "none",
            "--user",
            "10001:10001",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "3g",
            "--cpus",
            "2",
            "--tmpfs",
            "/tmp:rw,nosuid,size=512m",
            "--tmpfs",
            "/work:rw,exec,nosuid,size=1g",
            "--mount",
            f"type=bind,src={source},dst=/source,readonly",
            "--mount",
            f"type=bind,src={PLATFORM / 'policies'},dst=/policy,readonly",
            "--mount",
            f"type=bind,src={out},dst=/evidence",
            "--env",
            f"TEAM_DIRECTORY={directory}",
            "--env",
            f"CONTRACT_MAX_MS={limit}",
            (WORK / "image/id").read_text(),
            name,
        ]
    )
    if not (out / "result.json").is_file() or (out / "result.json").is_symlink():
        raise ValueError("Stage evidence is missing")
    if tree_hash() != (WORK / "source-sha256").read_text():
        raise ValueError("Source integrity changed during stage execution")
    (out / "identity.json").write_text(
        json.dumps(
            {
                **identity(),
                "source_sha256": (WORK / "source-sha256").read_text(),
                "image": (WORK / "image/id").read_text(),
            }
        )
    )


def release() -> None:
    selected = {
        "integration_tests": os.environ.get("RUN_INTEGRATION_TESTS") == "true",
        "contract_evaluations": os.environ.get("RUN_CONTRACT_EVALUATIONS") == "true",
    }
    needs = json.loads(os.environ["JOB_RESULTS"])
    required_jobs = {"source", "build", "unit_tests", "security"} | {
        name for name, enabled in selected.items() if enabled
    }
    if any(needs.get(name, {}).get("result") != "success" for name in required_jobs):
        raise ValueError("A required pipeline job did not pass")
    if any(
        needs.get(name, {}).get("result") != "skipped"
        for name, enabled in selected.items()
        if not enabled
    ):
        raise ValueError("Optional job selection differs from the request")
    expected = (WORK / "source-sha256").read_text()
    if tree_hash() != expected:
        raise ValueError("Source integrity changed")
    source_identity = json.loads((REPORTS / "source/identity.json").read_text())
    if (
        any(source_identity.get(key) != value for key, value in identity().items())
        or source_identity.get("source_sha256") != expected
    ):
        raise ValueError("Source evidence identity differs from this run")
    stages = {"build", "tests", "secret_scan", "sast", "dependencies"}
    if selected["integration_tests"]:
        stages.add("integration_tests")
    if selected["contract_evaluations"]:
        stages.add("evaluations")
    image = os.environ["EXPECTED_IMAGE_ID"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Build job image identity is missing")
    for name in stages:
        result = REPORTS / name / "result.json"
        if not result.is_file() or result.is_symlink():
            raise ValueError("Required stage evidence missing: " + name)
        stage_identity = json.loads((REPORTS / name / "identity.json").read_text())
        if (
            any(stage_identity.get(key) != value for key, value in identity().items())
            or stage_identity.get("source_sha256") != expected
            or stage_identity.get("image") != image
        ):
            raise ValueError("Stage evidence identity mismatch: " + name)
    (REPORTS / "identity.json").write_text(
        json.dumps({**identity(), "image": image, "source_sha256": expected})
    )
    (REPORTS / "job-results.json").write_text(json.dumps(needs, indent=2))
    files = {}
    for path in REPORTS.rglob("*"):
        if path.is_symlink():
            raise ValueError("Evidence symlinks are forbidden")
        if path.is_file():
            files[str(path.relative_to(REPORTS))] = digest(path)
    (REPORTS / "manifest.json").write_text(json.dumps(files, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        choices=[
            "source",
            "restore-source",
            "prepare-image",
            "export-image",
            "restore-image",
            "build",
            "tests",
            "integration_tests",
            "security",
            "evaluations",
            "release",
        ],
    )
    args = parser.parse_args()
    directory, limit = os.environ["TEAM_DIRECTORY"], int(os.environ["CONTRACT_LIMIT"])
    if directory not in {".", "backend"} or not 1 <= limit <= 5000:
        raise ValueError("Unsupported application directory or latency")
    if args.stage == "source":
        capture_source()
    elif args.stage == "restore-source":
        restore_source()
    elif args.stage == "prepare-image":
        prepare_image(directory)
    elif args.stage == "export-image":
        export_image()
    elif args.stage == "restore-image":
        restore_image()
    elif args.stage == "release":
        release()
    else:
        for name in (
            ["secret_scan", "sast", "dependencies"]
            if args.stage == "security"
            else [args.stage]
        ):
            stage(name, directory, limit)


if __name__ == "__main__":
    main()
