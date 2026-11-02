"""Resolve a Cloudsmith API credential and workspace.

Order of precedence for the credential:
  1. --api-key flag
  2. CLOUDSMITH_API_KEY environment variable
  3. The Cloudsmith CLI (`cloudsmith credential-helper generic`), which covers
     API keys, SSO / keyring tokens and OIDC
  4. The Cloudsmith CLI's credentials.ini file read directly
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIRS = [Path.home() / ".cloudsmith", Path.home() / ".config" / "cloudsmith"]


class AuthError(RuntimeError):
    pass


@dataclass
class Credential:
    token: str
    source: str

    @property
    def headers(self) -> dict[str, str]:
        # SSO / OIDC tokens are JWTs and go in a bearer header; API keys use X-Api-Key.
        if self.token.count(".") == 2:
            return {"Authorization": f"Bearer {self.token}"}
        return {"X-Api-Key": self.token}


def _section(profile: str | None) -> str:
    return f"profile:{profile}" if profile else "default"


def _read_ini(filename: str, explicit: Path | None) -> list[configparser.ConfigParser]:
    paths = [explicit] if explicit else [d / filename for d in CONFIG_DIRS] + [Path.cwd() / filename]
    parsers = []
    for path in paths:
        if path and path.is_file():
            parser = configparser.ConfigParser(interpolation=None)
            parser.read(path)
            parsers.append(parser)
    return parsers


def _lookup(parsers: list[configparser.ConfigParser], profile: str | None, *keys: str) -> str | None:
    for parser in parsers:
        for section in dict.fromkeys([_section(profile), "default"]):
            if parser.has_section(section):
                for key in keys:
                    value = parser.get(section, key, fallback="").strip()
                    if value:
                        return value
    return None


def _from_cli(profile: str | None, credentials_file: Path | None) -> str | None:
    exe = shutil.which("cloudsmith")
    if not exe:
        return None
    cmd = [exe, "credential-helper", "generic"]
    if profile:
        cmd += ["--profile", profile]
    if credentials_file:
        cmd += ["--credentials-file", str(credentials_file)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout).get("password") or None
    except json.JSONDecodeError:
        return None


def resolve_credential(
    api_key: str | None = None,
    profile: str | None = None,
    credentials_file: Path | None = None,
) -> Credential:
    profile = profile or os.environ.get("CLOUDSMITH_PROFILE")
    if api_key:
        return Credential(api_key, "--api-key")
    if env := os.environ.get("CLOUDSMITH_API_KEY"):
        return Credential(env, "CLOUDSMITH_API_KEY")
    if token := _from_cli(profile, credentials_file):
        return Credential(token, "cloudsmith CLI")
    if token := _lookup(_read_ini("credentials.ini", credentials_file), profile, "api_key"):
        return Credential(token, "credentials.ini")
    raise AuthError(
        "No Cloudsmith credential found. Set CLOUDSMITH_API_KEY, pass --api-key, "
        "or authenticate the Cloudsmith CLI (`cloudsmith auth -o <workspace>`)."
    )


def resolve_workspace(workspace: str | None, profile: str | None = None) -> str | None:
    profile = profile or os.environ.get("CLOUDSMITH_PROFILE")
    return (
        workspace
        or os.environ.get("CLOUDSMITH_WORKSPACE")
        or os.environ.get("CLOUDSMITH_ORG")
        or _lookup(_read_ini("config.ini", None), profile, "workspace", "organization", "org")
    )
