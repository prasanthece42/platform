"""Fixed commands executed inside a credential-free team application container."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, timeout=600)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage",
        choices=[
            "build",
            "tests",
            "integration_tests",
            "evaluations",
            "secret_scan",
            "sast",
            "dependencies",
        ],
    )
    args = parser.parse_args()
    directory = os.environ["TEAM_DIRECTORY"]
    if directory not in {".", "backend"}:
        raise ValueError("Unsupported application directory")
    source, out = Path("/source") / directory, Path("/evidence")
    if (
        hashlib.sha256((source / "requirements-ci.txt").read_bytes()).digest()
        != hashlib.sha256(Path("/opt/runner/requirements-ci.txt").read_bytes()).digest()
    ):
        raise ValueError("Dependency lock changed")
    os.environ.update(
        HOME="/tmp",
        AWS_EC2_METADATA_DISABLED="true",
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONPATH=str(source),
    )
    result: dict[str, object] = {}
    if args.stage == "build":
        import shutil

        target = Path("/work/application")
        shutil.copytree(source, target)
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(out / "artifacts"),
                str(target),
            ],
            target,
        )
        artifacts = list((out / "artifacts").glob("*"))
        if not any(p.suffix == ".whl" for p in artifacts) or not any(
            p.name.endswith(".tar.gz") for p in artifacts
        ):
            raise ValueError("Build must produce a wheel and source distribution")
        result = {
            "artifacts": [
                {"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in artifacts
            ]
        }
    elif args.stage in {"tests", "integration_tests", "evaluations"}:
        paths = {
            "tests": [
                "tests",
                "--ignore=tests/integration",
                "--ignore=tests/contracts",
            ],
            "integration_tests": ["tests/integration"],
            "evaluations": ["tests/contracts"],
        }[args.stage]
        report = out / "tests.xml"
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                *paths,
                "-q",
                "-p",
                "no:cacheprovider",
                "--junitxml",
                str(report),
            ],
            source,
        )
        cases = list(ET.parse(report).getroot().iter("testcase"))
        if not cases or any(
            any(c.find(tag) is not None for tag in ("failure", "error", "skipped"))
            for c in cases
        ):
            raise ValueError("Tests must execute, pass and contain no skipped cases")
        if args.stage == "evaluations" and any(
            float(c.attrib.get("time", "0")) * 1000 > int(os.environ["CONTRACT_MAX_MS"])
            for c in cases
        ):
            raise ValueError("Contract latency exceeded shared policy")
        result = {"test_count": len(cases), "live_ai": False}
    elif args.stage == "secret_scan":
        run(
            [
                "gitleaks",
                "dir",
                "/source",
                "--config",
                "/policy/gitleaks.toml",
                "--gitleaks-ignore-path",
                "/policy/empty.gitleaksignore",
                "--ignore-gitleaks-allow",
                "--redact",
                "--report-format",
                "json",
                "--report-path",
                str(out / "secrets.json"),
                "--exit-code",
                "1",
            ],
            source,
        )
        if json.loads((out / "secrets.json").read_text()) != []:
            raise ValueError("Secret findings")
    elif args.stage == "sast":
        import shutil

        target = Path("/work/scan")
        shutil.copytree(source, target)
        for ignored in target.rglob(".semgrepignore"):
            ignored.unlink()
        shutil.copy2("/policy/semgrepignore", target / ".semgrepignore")
        run(
            [
                "/opt/scanners/bin/pysemgrep",
                "scan",
                "--config",
                "/policy/semgrep.yml",
                "--error",
                "--strict",
                "--disable-nosem",
                "--metrics",
                "off",
                "--disable-version-check",
                "--no-git-ignore",
                "--json-output",
                str(out / "sast.json"),
                str(target),
            ],
            source,
        )
        data = json.loads((out / "sast.json").read_text())
        if (
            data.get("results")
            or data.get("errors")
            or not data.get("paths", {}).get("scanned")
        ):
            raise ValueError("Static analysis failed or scanned no code")
    else:
        run(
            [
                "/opt/scanners/bin/python",
                "-m",
                "pip_audit",
                "--disable-pip",
                "--require-hashes",
                "-r",
                str(source / "requirements-ci.txt"),
                "--format",
                "json",
                "--output",
                str(out / "dependencies.json"),
            ],
            source,
        )
        dependencies = json.loads((out / "dependencies.json").read_text()).get(
            "dependencies"
        )
        if not dependencies or any(
            d.get("vulns") or "skip_reason" in d for d in dependencies
        ):
            raise ValueError("Dependency audit failed or was incomplete")
    (out / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
