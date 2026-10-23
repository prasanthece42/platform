"""GitHub-only central runner for team-owned Python applications."""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "aegis-team-validation"
SOURCE, REPORTS = WORK / "source", WORK / "reports"
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


def prepare(directory: str) -> None:
    WORK.mkdir()
    SOURCE.mkdir()
    REPORTS.mkdir()
    data = subprocess.check_output(["git", "archive", "HEAD"])
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        if any(not (m.isfile() or m.isdir()) for m in tar.getmembers()):
            raise ValueError("Only regular source files are supported")
        tar.extractall(SOURCE, filter="data")
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
    (WORK / "source-sha256").write_text(tree_hash())
    (REPORTS / "identity.json").write_text(
        json.dumps(
            {
                "source_commit": os.environ["GITHUB_SHA"],
                "repository": os.environ["GITHUB_REPOSITORY"],
                "central_commit": os.environ["CENTRAL_SHA"],
                "image": image,
                "pipeline_version": os.environ["PIPELINE_VERSION"],
            }
        )
    )


def stage(name: str, directory: str, limit: int) -> None:
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


def release() -> None:
    if tree_hash() != (WORK / "source-sha256").read_text():
        raise ValueError("Source integrity changed")
    for name in ("build", "tests", "secret_scan", "sast", "dependencies"):
        if not (REPORTS / name / "result.json").is_file():
            raise ValueError("Required evidence missing")
    files = {}
    for path in REPORTS.rglob("*"):
        if path.is_symlink():
            raise ValueError("Evidence symlinks are forbidden")
        if path.is_file():
            files[str(path.relative_to(REPORTS))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    (REPORTS / "manifest.json").write_text(json.dumps(files, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        choices=[
            "source",
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
        prepare(directory)
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
