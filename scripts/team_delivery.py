"""Finite, centrally reviewed development delivery; no caller-supplied shell commands."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
SHA = re.compile(r"[a-f0-9]{40}")


class CommandFailed(RuntimeError):
    def __init__(self, message: str, aws_code: str | None = None):
        super().__init__(message)
        self.aws_code = aws_code


def run(arguments: list[str], *, stdin: str | None = None, timeout: int = 180) -> str:
    result = subprocess.run(
        arguments, input=stdin, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        # Do not echo command arguments, credentials, or arbitrary subprocess output.
        match = (
            re.search(
                r"An error occurred \(([A-Za-z0-9]+)\) when calling the ([A-Za-z0-9]+) operation",
                result.stderr,
            )
            if arguments[0] == "aws"
            else None
        )
        code = match.group(1) if match else None
        detail = f"{match.group(2)}: {code}" if match else f"exit {result.returncode}"
        raise CommandFailed(f"{arguments[0]} {arguments[1]} failed ({detail})", code)
    return result.stdout.strip()


def aws(*arguments: str) -> Any:
    return json.loads(
        run(
            [
                "aws",
                *arguments,
                "--region",
                target()["region"],
                "--output",
                "json",
                "--no-cli-pager",
            ]
        )
    )


def target() -> dict[str, str]:
    root = Path(__file__).resolve().parent.parent
    registry = json.loads((root / "configs/delivery-targets.json").read_text())
    key = os.environ["TARGET_REF"]
    if registry.get("version") != 1 or key not in registry["targets"]:
        raise ValueError("Unknown centrally approved deployment target")
    profile = registry["targets"][key]
    if (
        not re.fullmatch(r"[a-z][a-z0-9-]{1,60}-dev", key)
        or profile["namespace"] != key
        or profile["branch"] != "main"
        or profile["account_id"] != "230001290423"
        or profile["region"] != "ca-central-1"
        or profile["cluster"] != "aegisai-development"
        or profile.get("workload", "payments") not in {"payments", "stateless-chatbot"}
    ):
        raise ValueError("Only registered development targets are enabled")
    for phase in ("publisher", "deployer"):
        expected_role = (
            f"arn:aws:iam::{profile['account_id']}:role/aegisai-{key}-{phase}"
        )
        if profile[f"{phase}_role"] != expected_role:
            raise ValueError(
                "Target role differs from the registered development boundary"
            )
    for claim, expected in (
        ("GITHUB_REPOSITORY", "repository"),
        ("GITHUB_REPOSITORY_ID", "repository_id"),
        ("GITHUB_REPOSITORY_OWNER_ID", "repository_owner_id"),
    ):
        if os.environ[claim] != profile[expected]:
            raise ValueError("Caller differs from the approved repository identity")
    if os.environ["GITHUB_REF"] != "refs/heads/" + profile["branch"] or os.environ[
        "GITHUB_EVENT_NAME"
    ] not in {"push", "workflow_dispatch"}:
        raise ValueError("Deployment is restricted to the approved main branch")
    if not SHA.fullmatch(os.environ["GITHUB_SHA"]) or not SHA.fullmatch(
        os.environ["CENTRAL_SHA"]
    ):
        raise ValueError("Source and central workflow must use immutable commits")
    if not all(
        re.fullmatch(r"[1-9][0-9]*", os.environ[key])
        for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
    ):
        raise ValueError("Invalid GitHub run identity")
    return profile


def image_prefix(profile: dict[str, str]) -> str:
    return f"{profile['account_id']}.dkr.ecr.{profile['region']}.amazonaws.com/{profile['ecr_repository']}"


def validate_image(profile: dict[str, str], image: str) -> str:
    prefix = image_prefix(profile) + "@"
    if not image.startswith(prefix) or not DIGEST.fullmatch(image[len(prefix) :]):
        raise ValueError(
            "Image must use this application's ECR repository and an immutable digest"
        )
    return image[len(prefix) :]


def manifest(profile: dict[str, str], image: str, source_sha: str) -> dict[str, Any]:
    validate_image(profile, image)
    if not SHA.fullmatch(source_sha):
        raise ValueError("Invalid source commit")
    name, namespace = profile["application"], profile["namespace"]
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/managed-by": "aegisai"}
    result = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "type": "ClusterIP",
                    "selector": {"app.kubernetes.io/name": name},
                    "ports": [{"name": "http", "port": 8000, "targetPort": "http"}],
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": name, "namespace": namespace, "labels": labels},
                "spec": {
                    "serviceName": name,
                    "replicas": 1,
                    "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                    "template": {
                        "metadata": {
                            "labels": labels,
                            "annotations": {"aegisai/source-commit": source_sha},
                        },
                        "spec": {
                            "serviceAccountName": profile["service_account"],
                            "automountServiceAccountToken": False,
                            "terminationGracePeriodSeconds": 30,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 10001,
                                "runAsGroup": 10001,
                                "fsGroup": 10001,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": name,
                                    "image": image,
                                    "imagePullPolicy": "IfNotPresent",
                                    "ports": [{"name": "http", "containerPort": 8000}],
                                    "env": [
                                        {
                                            "name": "PAYMENTS_DATABASE_PATH",
                                            "value": "/data/payments.db",
                                        },
                                        {
                                            "name": "PYTHONDONTWRITEBYTECODE",
                                            "value": "1",
                                        },
                                    ],
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "128Mi"},
                                        "limits": {"cpu": "500m", "memory": "512Mi"},
                                    },
                                    "securityContext": {
                                        "readOnlyRootFilesystem": True,
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "startupProbe": {
                                        "httpGet": {"path": "/health", "port": "http"},
                                        "periodSeconds": 5,
                                        "failureThreshold": 30,
                                    },
                                    "readinessProbe": {
                                        "httpGet": {"path": "/health", "port": "http"},
                                        "periodSeconds": 5,
                                    },
                                    "livenessProbe": {
                                        "httpGet": {"path": "/health", "port": "http"},
                                        "periodSeconds": 10,
                                    },
                                    "volumeMounts": [
                                        {"name": "data", "mountPath": "/data"},
                                        {"name": "temporary", "mountPath": "/tmp"},
                                    ],
                                }
                            ],
                            "volumes": [
                                {
                                    "name": "data",
                                    "persistentVolumeClaim": {
                                        "claimName": "payment-api-data"
                                    },
                                },
                                {
                                    "name": "temporary",
                                    "emptyDir": {"sizeLimit": "64Mi"},
                                },
                            ],
                        },
                    },
                },
            },
        ],
    }
    if profile.get("workload", "payments") == "stateless-chatbot":
        workload = result["items"][1]
        workload["kind"] = "Deployment"
        workload["spec"].pop("serviceName")
        pod = workload["spec"]["template"]["spec"]
        container = pod["containers"][0]
        container["env"] = [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}]
        container["volumeMounts"] = [{"name": "temporary", "mountPath": "/tmp"}]
        pod["volumes"] = [{"name": "temporary", "emptyDir": {"sizeLimit": "64Mi"}}]
    return result


def evidence_path(name: str) -> Path:
    directory = Path(os.environ["RUNNER_TEMP"]) / "aegis-delivery"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def write_report(name: str, report: Any) -> None:
    evidence_path(name).write_text(json.dumps(report, indent=2) + "\n")


def provenance(profile: dict[str, str]) -> dict[str, str]:
    return {
        "source_commit": os.environ["GITHUB_SHA"],
        "central_commit": os.environ["CENTRAL_SHA"],
        "repository": profile["repository"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "target": os.environ["TARGET_REF"],
    }


def check_role(profile: dict[str, str], phase: str) -> None:
    identity = aws("sts", "get-caller-identity")
    role = profile[f"{phase}_role"].split("/")[-1]
    prefix = f"arn:aws:sts::{profile['account_id']}:assumed-role/{role}/"
    if identity["Account"] != profile["account_id"] or not identity["Arn"].startswith(
        prefix
    ):
        raise ValueError("Unexpected AWS account or assumed role")


def build(profile: dict[str, str]) -> None:
    source = Path(os.environ["GITHUB_WORKSPACE"]) / "source"
    if run(["git", "-C", str(source), "rev-parse", "HEAD"]) != os.environ["GITHUB_SHA"]:
        raise ValueError("Build source differs from the validated source commit")
    run(
        [
            "docker",
            "build",
            "--provenance=false",
            "--sbom=false",
            "--platform",
            "linux/amd64",
            "--label",
            "org.opencontainers.image.revision=" + os.environ["GITHUB_SHA"],
            "-t",
            "aegis-delivery:validated",
            str(source),
        ],
        timeout=600,
    )


def scan(profile: dict[str, str], digest: str) -> dict[str, Any]:
    for _ in range(40):
        try:
            result = aws(
                "ecr",
                "describe-image-scan-findings",
                "--repository-name",
                profile["ecr_repository"],
                "--image-id",
                "imageDigest=" + digest,
            )
        except CommandFailed as error:
            if error.aws_code not in {
                "ScanNotFoundException",
                "ImageNotFoundException",
            }:
                raise
            time.sleep(5)
            continue
        status = result.get("imageScanStatus", {}).get("status")
        if status == "COMPLETE":
            counts = result.get("imageScanFindings", {}).get(
                "findingSeverityCounts", {}
            )
            if counts.get("CRITICAL", 0) or counts.get("HIGH", 0):
                raise ValueError(
                    "ECR scan found critical or high vulnerabilities; deployment is blocked"
                )
            return {"status": status, "severity_counts": counts}
        if status not in {"IN_PROGRESS", "PENDING"}:
            raise ValueError("Required ECR scan did not complete successfully")
        time.sleep(5)
    raise TimeoutError("Required ECR image scan timed out")


def publish(profile: dict[str, str]) -> None:
    check_role(profile, "publisher")
    tag = f"sha-{os.environ['GITHUB_SHA']}-run-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    image = image_prefix(profile) + ":" + tag
    registry = image_prefix(profile).split("/")[0]
    password = run(["aws", "ecr", "get-login-password", "--region", profile["region"]])
    with tempfile.TemporaryDirectory(prefix="aegis-docker-auth-") as credentials:
        try:
            run(
                [
                    "docker",
                    "--config",
                    credentials,
                    "login",
                    "--username",
                    "AWS",
                    "--password-stdin",
                    registry,
                ],
                stdin=password,
            )
        finally:
            password = ""
        run(["docker", "tag", "aegis-delivery:validated", image])
        run(["docker", "--config", credentials, "push", image], timeout=600)
    result = aws(
        "ecr",
        "describe-images",
        "--repository-name",
        profile["ecr_repository"],
        "--image-ids",
        "imageTag=" + tag,
    )
    digest = result["imageDetails"][0]["imageDigest"]
    image_uri = image_prefix(profile) + "@" + digest
    validate_image(profile, image_uri)
    findings = scan(profile, digest)
    write_report(
        "image.json", {**provenance(profile), "image": image_uri, "scan": findings}
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write("image_uri=" + image_uri + "\n")


def deploy(profile: dict[str, str]) -> None:
    check_role(profile, "deployer")
    image = os.environ["IMAGE_URI"]
    digest = validate_image(profile, image)
    previous = json.loads(evidence_path("image.json").read_text())
    if (
        any(previous.get(key) != value for key, value in provenance(profile).items())
        or previous.get("image") != image
    ):
        raise ValueError(
            "Image evidence differs from this run, source or central workflow"
        )
    scan(profile, digest)
    rendered = manifest(profile, image, os.environ["GITHUB_SHA"])
    write_report("manifests.json", rendered)
    kubeconfig = Path(os.environ["RUNNER_TEMP"]) / "aegis-kubeconfig"
    os.environ["KUBECONFIG"] = str(kubeconfig)
    run(
        [
            "aws",
            "eks",
            "update-kubeconfig",
            "--name",
            profile["cluster"],
            "--region",
            profile["region"],
            "--kubeconfig",
            str(kubeconfig),
        ]
    )
    kubeconfig.chmod(0o600)
    kubectl = ["kubectl", "--namespace", profile["namespace"]]
    for resource in (
        "secrets",
        "roles",
        "rolebindings",
        "serviceaccounts",
        "pods/exec",
    ):
        result = subprocess.run(
            [*kubectl, "auth", "can-i", "create", resource],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip() != "no":
            raise ValueError(
                "Deployment credentials have unexpected privilege: " + resource
            )
    run(
        [
            *kubectl,
            "apply",
            "--server-side",
            "--field-manager=aegisai-delivery",
            "-f",
            str(evidence_path("manifests.json")),
        ]
    )
    run(
        [
            *kubectl,
            "rollout",
            "status",
            (
                "deployment/"
                if profile.get("workload") == "stateless-chatbot"
                else "statefulset/"
            )
            + profile["application"],
            "--timeout=300s",
        ],
        timeout=320,
    )
    pods = json.loads(
        run(
            [
                *kubectl,
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/name=" + profile["application"],
                "-o",
                "json",
            ]
        )
    )
    if (
        len(pods["items"]) != 1
        or pods["items"][0]["spec"]["containers"][0]["image"] != image
    ):
        raise ValueError("Running workload differs from the published digest")
    storage = "not-required"
    if profile.get("workload", "payments") == "payments":
        volume = json.loads(
            run([*kubectl, "get", "pvc", "payment-api-data", "-o", "json"])
        )
        if volume.get("status", {}).get("phase") != "Bound":
            raise ValueError("Durable application storage is not bound")
        storage = "Bound"
    with tempfile.TemporaryFile() as logs:
        forward = subprocess.Popen(
            [
                *kubectl,
                "port-forward",
                "service/" + profile["application"],
                "18080:8000",
                "--address=127.0.0.1",
            ],
            stdout=logs,
            stderr=logs,
        )
        try:
            for _ in range(40):
                if forward.poll() is not None:
                    raise RuntimeError("Authenticated service port-forward failed")
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:18080/health", timeout=3
                    ) as response:
                        if response.status == 200 and json.load(response) == {
                            "status": "ok"
                        }:
                            break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(1)
            else:
                raise TimeoutError("Deployed application health check did not pass")
            if profile.get("workload") == "stateless-chatbot":
                request = urllib.request.Request(
                    "http://127.0.0.1:18080/api/chat",
                    data=json.dumps(
                        {"message": "Explain the pipeline", "history": []}
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    reply = json.load(response)
                    if (
                        response.status != 200
                        or reply.get("provider") != "demo-faq"
                        or reply.get("live_ai") is not False
                        or not reply.get("reply")
                        or reply.get("topic") != "pipeline"
                    ):
                        raise ValueError("Deployed chatbot API verification failed")
            else:
                with urllib.request.urlopen(
                    "http://127.0.0.1:18080/payments", timeout=5
                ) as response:
                    if response.status != 200 or not isinstance(
                        json.load(response), list
                    ):
                        raise ValueError("Deployed application API check failed")
        finally:
            forward.terminate()
            try:
                forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                forward.kill()
                forward.wait(timeout=5)
    write_report(
        "deployment.json",
        {
            **provenance(profile),
            "image": image,
            "cluster": profile["cluster"],
            "namespace": profile["namespace"],
            "storage": storage,
            "health": "passed",
            "api": "passed",
            "status": "succeeded",
        },
    )


def configure(profile: dict[str, str]) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        for key in ("publisher_role", "deployer_role", "account_id", "region"):
            if "\n" in profile[key] or "\r" in profile[key]:
                raise ValueError("Invalid target output")
            output.write(key + "=" + profile[key] + "\n")


def website(profile: dict[str, str]) -> None:
    """Require the registered HTTPS entry point and real application behavior."""
    url = profile.get("website_url")
    if url is None:
        return
    if profile.get("workload") != "stateless-chatbot" or not re.fullmatch(
        r"https://[a-z0-9]{8,64}\.cloudfront\.net", url
    ):
        raise ValueError("Unregistered public website URL")

    def check(path: str, *, data: bytes | None = None) -> bytes:
        request = urllib.request.Request(
            url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200 or response.url != url + path:
                raise ValueError("Website returned an unexpected status or redirect")
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise ValueError("Website response exceeds verification limit")
            return body

    for attempt in range(24):
        try:
            if json.loads(check("/health")) != {"status": "ok"}:
                raise ValueError("Website health check failed")
            page = check("/").decode()
            if "<title>Harbor" not in page or 'id="chat-form"' not in page:
                raise ValueError("Website does not serve the expected chatbot page")
            for path in ("/static/chat.css", "/static/chat.js"):
                if not check(path):
                    raise ValueError("Website asset is empty")
            reply = json.loads(
                check(
                    "/api/chat",
                    data=json.dumps(
                        {"message": "Explain the pipeline", "history": []}
                    ).encode(),
                )
            )
            if (
                reply.get("provider") != "demo-faq"
                or reply.get("live_ai") is not False
                or not reply.get("reply")
                or reply.get("topic") != "pipeline"
            ):
                raise ValueError("Public chatbot API verification failed")
            break
        except (OSError, ValueError):
            if attempt == 23:
                raise
            time.sleep(5)
    write_report(
        "website.json",
        {
            **provenance(profile),
            "website_url": url,
            "https": "passed",
            "page": "passed",
            "assets": "passed",
            "api": "passed",
            "status": "succeeded",
        },
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as output:
            output.write(
                f"\n### Development website\n\n[Open app]({url})\n\nHTTPS, page, assets and chat API verified.\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase", choices=["configure", "build", "publish", "deploy", "website"]
    )
    args = parser.parse_args()
    try:
        profile = target()
        {
            "configure": configure,
            "build": build,
            "publish": publish,
            "deploy": deploy,
            "website": website,
        }[args.phase](profile)
    except Exception as error:
        write_report(
            "failure.json",
            {"status": "failed", "phase": args.phase, "reason": str(error)[:300]},
        )
        raise SystemExit(f"Delivery {args.phase} failed: {error}") from None


if __name__ == "__main__":
    main()
