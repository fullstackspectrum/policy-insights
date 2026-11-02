"""Synthetic decision logs for trying the views without API access."""

from __future__ import annotations

import random
import string
import uuid
from datetime import datetime, timedelta, timezone

# (name, chance of matching, share of evaluations, terminal)
POLICIES = [
    ("Block critical CVEs", 0.06, 1.0, True),
    ("Quarantine copyleft licences", 0.03, 1.0, True),
    ("Tag unreviewed upstream packages", 0.42, 0.9, False),
    ("Deny packages older than 5 years", 0.01, 1.0, True),
    ("Flag EPSS > 0.5", 0.09, 0.8, False),
    ("Require SBOM for containers", 0.0, 0.35, False),
    ("Malware feed match", 0.002, 1.0, True),
    ("Restrict prerelease versions", 0.18, 0.7, False),
]
REPOS = [("production", 0.45), ("upstream-cache", 0.35), ("dev-sandbox", 0.2)]
FORMATS = {
    "NPM": ["lodash", "react", "express", "axios", "left-pad", "chalk", "minimist"],
    "PYTHON": ["requests", "urllib3", "django", "numpy", "pyyaml", "jinja2"],
    "MAVEN": ["log4j-core", "jackson-databind", "spring-core", "commons-text"],
    "DOCKER": ["nginx", "python", "node", "alpine"],
    "GO": ["golang.org/x/net", "github.com/gin-gonic/gin"],
}


def _slug(rng: random.Random, n: int = 12) -> str:
    return "".join(rng.choices(string.ascii_letters + string.digits, k=n))


def _ulid(ts: datetime, rng: random.Random) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    ms = int(ts.timestamp() * 1000)
    head = "".join(alphabet[(ms >> (5 * i)) & 31] for i in reversed(range(10)))
    return head + "".join(rng.choices(alphabet, k=16))


def generate(days: int = 30, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    policies = [(_slug(rng), *p) for p in POLICIES]
    repos = [(_slug(rng), name, w) for name, w in REPOS]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    logs = []
    for d in range(days, -1, -1):
        day = (now - timedelta(days=d)).replace(hour=0, minute=0, second=0)
        weekday = day.weekday() < 5
        growth = 1 + (days - d) / days * 0.6
        evaluations = int(rng.gauss(55 if weekday else 15, 8) * growth)
        if rng.random() < 0.06:  # occasional bulk re-evaluation
            evaluations *= 4
        for _ in range(max(evaluations, 0)):
            ts = day + timedelta(seconds=rng.randrange(86400))
            if ts > now:
                continue
            fmt = rng.choice(list(FORMATS))
            name = rng.choice(FORMATS[fmt])
            repo_slug, repo_name, _ = rng.choices(repos, weights=[r[2] for r in repos])[0]
            corr = str(uuid.UUID(int=rng.getrandbits(128)))
            pkg_slug = _slug(rng)
            version = f"{rng.randint(0, 5)}.{rng.randint(0, 20)}.{rng.randint(0, 9)}"
            for precedence, (slug, pname, p_match, share, terminal) in enumerate(policies, 1):
                if rng.random() > share:
                    continue
                if pname == "Require SBOM for containers" and fmt != "DOCKER":
                    continue
                matched = rng.random() < p_match * (1.8 if repo_name == "upstream-cache" else 1)
                started = ts + timedelta(milliseconds=precedence * 7)
                logs.append(
                    {
                        "id": _ulid(started, rng),
                        "actions": {},
                        "correlation_id": corr,
                        "started_at": started.isoformat().replace("+00:00", "Z"),
                        "ended_at": (started + timedelta(milliseconds=5)).isoformat().replace("+00:00", "Z"),
                        "match": matched,
                        "package_format": fmt,
                        "package_name": name,
                        "package_slug": f"{name}-{version}".replace("/", "-"),
                        "package_slug_perm": pkg_slug,
                        "package_version": version,
                        "policy_is_terminal": terminal,
                        "policy_name": pname,
                        "policy_precedence": precedence,
                        "policy_slug_perm": slug,
                        "policy_version": 1 + precedence % 3,
                        "repository_name": repo_name,
                        "repository_slug": repo_name,
                        "repository_slug_perm": repo_slug,
                    }
                )
                if matched and terminal:
                    break
    logs.sort(key=lambda log: log["id"])
    return logs
