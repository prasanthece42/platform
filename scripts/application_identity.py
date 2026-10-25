"""Stable server-derived identities for one application and its dedicated cluster."""

import hashlib
import re


def names(team: str, application: str) -> dict[str, str]:
    if not all(
        re.fullmatch(r"[a-z][a-z0-9-]{1,63}", value) for value in (team, application)
    ):
        raise ValueError("Invalid application identity")
    digest = hashlib.sha256(f"{team}/{application}".encode()).hexdigest()[:10]
    namespace = f"{team}-{application}-dev"
    if len(namespace) > 42:
        namespace = f"{team[:10]}-{application[:12]}-{digest}-dev"
    return {
        "key": digest,
        "cluster": f"aegis-{application[:18]}-{digest}-dev",
        "namespace": namespace,
        "ecr_repository": f"aegisai/{team}/{application}",
        "web_name": f"aegis-web-{digest}",
    }
