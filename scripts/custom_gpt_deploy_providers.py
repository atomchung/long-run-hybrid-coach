#!/usr/bin/env python3
"""Provider read-back primitives for a Custom GPT production release.

The module deliberately keeps provider authentication outside its boundary.  It
invokes ``gh api`` without inspecting its credential store, or accepts injected
Vercel readers (for example, MCP/SDK adapters).  Returned evidence contains only
allow-listed identifiers and SHA-256 digests of raw provider responses.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERCEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
PROXY_REVISION_RE = re.compile(r"^gclp-[0-9a-f]{64}$")
PROJECT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
GITHUB_BRANCH = "main"
GITHUB_WORKFLOW_PATH = ".github/workflows/ci.yml"


class ProviderReadbackError(ValueError):
    """A privacy-safe, classifiable provider evidence failure."""

    def __init__(self, code: str, provider: str, operation: str, message: str):
        self.code = code
        self.provider = provider
        self.operation = operation
        super().__init__(f"{provider} {operation}: {message}")


@dataclass(frozen=True)
class ProviderResponse:
    """Minimal adapter response; bodies must be decoded JSON objects."""

    status_code: int
    body: Mapping[str, Any] | None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_exact_keys(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ProviderReadbackError(
            "invalid_evidence", "local", label, "schema is not exact"
        )
    return value


def _target_body(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": value["schema_version"],
        "environment": value["environment"],
        "github": value["github"],
        "vercel": value["vercel"],
        "custom_gpt": value["custom_gpt"],
    }


def production_target_binding(value: Mapping[str, Any]) -> str:
    """Return the canonical binding for a target, excluding its binding field."""
    return canonical_sha256(_target_body(value))


def validate_production_target(
    value: Any, *, expected_repository: str | None = None
) -> dict[str, Any]:
    target = _require_exact_keys(
        value,
        {"schema_version", "environment", "github", "vercel", "custom_gpt", "binding_sha256"},
        "production target",
    )
    github = _require_exact_keys(
        target["github"], {"repository", "branch", "workflow_path"}, "GitHub target"
    )
    vercel = _require_exact_keys(
        target["vercel"],
        {"team_id", "project_id", "project_name", "stable_domain"},
        "Vercel target",
    )
    custom_gpt = _require_exact_keys(
        target["custom_gpt"], {"gpt_id"}, "Custom GPT target"
    )
    repository = github.get("repository")
    stable_domain = vercel.get("stable_domain")
    valid = (
        target.get("schema_version") == "1"
        and target.get("environment") == "production"
        and isinstance(repository, str)
        and bool(REPOSITORY_RE.fullmatch(repository))
        and (expected_repository is None or repository == expected_repository)
        and github.get("branch") == GITHUB_BRANCH
        and github.get("workflow_path") == GITHUB_WORKFLOW_PATH
        and isinstance(vercel.get("team_id"), str)
        and bool(VERCEL_ID_RE.fullmatch(vercel["team_id"]))
        and isinstance(vercel.get("project_id"), str)
        and bool(VERCEL_ID_RE.fullmatch(vercel["project_id"]))
        and isinstance(vercel.get("project_name"), str)
        and bool(PROJECT_NAME_RE.fullmatch(vercel["project_name"]))
        and isinstance(stable_domain, str)
        and stable_domain == stable_domain.lower()
        and bool(DOMAIN_RE.fullmatch(stable_domain))
        and isinstance(custom_gpt.get("gpt_id"), str)
        and bool(VERCEL_ID_RE.fullmatch(custom_gpt["gpt_id"]))
        and isinstance(target.get("binding_sha256"), str)
        and target["binding_sha256"] == production_target_binding(target)
    )
    if not valid:
        raise ProviderReadbackError(
            "target_mismatch", "local", "production target", "identity or binding mismatch"
        )
    return json.loads(_canonical_bytes(target))


def load_production_target(
    path: Path, *, expected_repository: str | None = None, repo_root: Path = ROOT
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    root = repo_root.resolve()
    if resolved == root or root in resolved.parents:
        raise ProviderReadbackError(
            "unsafe_location", "local", "production target", "file must be outside repository"
        )
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderReadbackError(
            "invalid_evidence", "local", "production target", "cannot read valid JSON"
        ) from exc
    return validate_production_target(value, expected_repository=expected_repository)


def _default_gh_runner(arguments: list[str]) -> Any:
    result = subprocess.run(
        arguments, check=False, capture_output=True, text=True, timeout=30
    )
    if result.returncode:
        # Do not relay stderr: provider/CLI diagnostics can contain account data.
        raise ProviderReadbackError(
            "provider_error", "github", "read-back", "gh api request failed"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProviderReadbackError(
            "invalid_readback", "github", "read-back", "provider returned invalid JSON"
        ) from exc


def _receipt_hash(value: Mapping[str, Any], field: str) -> str:
    body = {key: item for key, item in value.items() if key != field}
    return canonical_sha256(body)


def validate_github_provider_receipt(
    value: Any, *, target: Mapping[str, Any], expected_sha: str
) -> dict[str, Any]:
    target = validate_production_target(target)
    receipt = _require_exact_keys(
        value,
        {
            "schema_version", "provider", "target_binding_sha256", "repository",
            "branch", "workflow_path", "commit_sha", "main_ref_sha",
            "workflow_run_id", "workflow_run_url", "workflow_run_event",
            "workflow_run_status", "workflow_run_conclusion", "checked_at",
            "raw_response_sha256", "receipt_sha256",
        },
        "GitHub receipt",
    )
    raw = _require_exact_keys(
        receipt["raw_response_sha256"], {"main_ref", "workflow_runs"}, "GitHub raw hashes"
    )
    run_id = receipt.get("workflow_run_id")
    valid = (
        COMMIT_RE.fullmatch(expected_sha or "")
        and receipt.get("schema_version") == "1"
        and receipt.get("provider") == "github-actions"
        and receipt.get("target_binding_sha256") == target["binding_sha256"]
        and receipt.get("repository") == target["github"]["repository"]
        and receipt.get("branch") == GITHUB_BRANCH
        and receipt.get("workflow_path") == GITHUB_WORKFLOW_PATH
        and receipt.get("commit_sha") == expected_sha
        and receipt.get("main_ref_sha") == expected_sha
        and isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0
        and isinstance(receipt.get("workflow_run_url"), str)
        and receipt["workflow_run_url"].startswith(
            f"https://github.com/{target['github']['repository']}/actions/runs/{run_id}"
        )
        and receipt.get("workflow_run_event") == "push"
        and receipt.get("workflow_run_status") == "completed"
        and receipt.get("workflow_run_conclusion") == "success"
        and all(isinstance(item, str) and SHA256_RE.fullmatch(item) for item in raw.values())
        and receipt.get("receipt_sha256") == _receipt_hash(receipt, "receipt_sha256")
    )
    if not valid:
        raise ProviderReadbackError(
            "invalid_evidence", "github", "receipt validation", "receipt binding mismatch"
        )
    return json.loads(_canonical_bytes(receipt))


class GitHubProviderReader:
    """Read current main and exact successful ``ci.yml`` push evidence."""

    def __init__(
        self,
        target: Mapping[str, Any],
        *,
        runner: Callable[[list[str]], Any] = _default_gh_runner,
        clock: Callable[[], str] = _now,
    ):
        self.target = validate_production_target(target)
        self.runner = runner
        self.clock = clock

    def read(self, expected_sha: str) -> dict[str, Any]:
        if not COMMIT_RE.fullmatch(expected_sha or ""):
            raise ProviderReadbackError(
                "invalid_request", "github", "read-back", "expected commit is malformed"
            )
        repository = self.target["github"]["repository"]
        ref_endpoint = f"repos/{repository}/git/ref/heads/{GITHUB_BRANCH}"
        workflow_endpoint = (
            f"repos/{repository}/actions/workflows/ci.yml/runs"
            f"?branch={GITHUB_BRANCH}&event=push&status=completed&per_page=100"
        )
        ref_raw = self.runner(["gh", "api", ref_endpoint])
        runs_raw = self.runner(["gh", "api", workflow_endpoint])
        try:
            main_sha = ref_raw["object"]["sha"]
            runs = runs_raw["workflow_runs"]
        except (KeyError, TypeError) as exc:
            raise ProviderReadbackError(
                "invalid_readback", "github", "read-back", "provider schema is incomplete"
            ) from exc
        if main_sha != expected_sha:
            raise ProviderReadbackError(
                "stale_main", "github", "read-back", "remote main is not the expected commit"
            )
        if not isinstance(runs, list):
            raise ProviderReadbackError(
                "invalid_readback", "github", "read-back", "workflow runs are not a list"
            )
        matches: list[dict[str, Any]] = []
        for run in runs:
            if not isinstance(run, dict):
                continue
            path = run.get("path")
            path_matches = path == GITHUB_WORKFLOW_PATH or (
                isinstance(path, str) and path.startswith(GITHUB_WORKFLOW_PATH + "@")
            )
            if (
                run.get("head_sha") == expected_sha
                and run.get("head_branch") == GITHUB_BRANCH
                and run.get("event") == "push"
                and run.get("status") == "completed"
                and run.get("conclusion") == "success"
                and path_matches
                and isinstance(run.get("repository"), dict)
                and run["repository"].get("full_name") == repository
            ):
                matches.append(run)
        if not matches:
            raise ProviderReadbackError(
                "ci_not_successful", "github", "read-back",
                "no exact successful main push run exists for ci.yml",
            )
        matches.sort(key=lambda item: int(item.get("id", 0)), reverse=True)
        run = matches[0]
        run_id = run.get("id")
        run_url = run.get("html_url")
        receipt: dict[str, Any] = {
            "schema_version": "1",
            "provider": "github-actions",
            "target_binding_sha256": self.target["binding_sha256"],
            "repository": repository,
            "branch": GITHUB_BRANCH,
            "workflow_path": GITHUB_WORKFLOW_PATH,
            "commit_sha": expected_sha,
            "main_ref_sha": main_sha,
            "workflow_run_id": run_id,
            "workflow_run_url": run_url,
            "workflow_run_event": run.get("event"),
            "workflow_run_status": run.get("status"),
            "workflow_run_conclusion": run.get("conclusion"),
            "checked_at": self.clock(),
            "raw_response_sha256": {
                "main_ref": canonical_sha256(ref_raw),
                "workflow_runs": canonical_sha256(runs_raw),
            },
        }
        receipt["receipt_sha256"] = _receipt_hash(receipt, "receipt_sha256")
        return validate_github_provider_receipt(
            receipt, target=self.target, expected_sha=expected_sha
        )


def _https_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value if "://" in value else "https://" + value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
    ):
        return None
    return "https://" + parsed.hostname.lower()


def normalize_vercel_create_attestation(
    raw: Mapping[str, Any], *, target: Mapping[str, Any]
) -> dict[str, Any]:
    """Allow-list a deployment create result and bind it to production target."""
    target = validate_production_target(target)
    if not isinstance(raw, Mapping):
        raise ProviderReadbackError(
            "invalid_evidence", "vercel", "create attestation", "response is not an object"
        )
    deployment_id = raw.get("deployment_id", raw.get("deploymentId", raw.get("id")))
    deployment_url = raw.get("deployment_url", raw.get("deploymentUrl", raw.get("url")))
    project_id = raw.get("project_id", raw.get("projectId"))
    team_id = raw.get("team_id", raw.get("teamId"))
    project_name = raw.get("project_name", raw.get("projectName", raw.get("name")))
    metadata = raw.get("metadata", raw.get("meta"))
    normalized_url = _https_url(deployment_url)
    expected = target["vercel"]
    if (
        raw.get("provider") != "vercel"
        or raw.get("target") != "production"
        or deployment_id is None
        or not isinstance(deployment_id, str)
        or not VERCEL_ID_RE.fullmatch(deployment_id)
        or project_id != expected["project_id"]
        or team_id != expected["team_id"]
        or project_name != expected["project_name"]
        or not isinstance(metadata, Mapping)
        or set(metadata) != {
            "gclProxyRevision", "gclRequestSha256", "gclConfigSha256",
        }
        or not all(isinstance(value, str) and value for value in metadata.values())
        or not PROXY_REVISION_RE.fullmatch(
            str(metadata.get("gclProxyRevision", ""))
        )
        or not SHA256_RE.fullmatch(str(metadata.get("gclRequestSha256", "")))
        or not SHA256_RE.fullmatch(str(metadata.get("gclConfigSha256", "")))
        or normalized_url is None
    ):
        raise ProviderReadbackError(
            "target_mismatch", "vercel", "create attestation",
            "deployment does not identify the fixed production target",
        )
    attestation: dict[str, Any] = {
        "schema_version": "1",
        "provider": "vercel",
        "target": "production",
        "target_binding_sha256": target["binding_sha256"],
        "team_id": team_id,
        "project_id": project_id,
        "project_name": project_name,
        "deployment_id": deployment_id,
        "deployment_url": normalized_url,
        "metadata": dict(metadata),
        "create_raw_sha256": canonical_sha256(raw),
    }
    attestation["attestation_sha256"] = _receipt_hash(
        attestation, "attestation_sha256"
    )
    return validate_vercel_create_attestation(attestation, target=target)


def validate_vercel_create_attestation(
    value: Any, *, target: Mapping[str, Any]
) -> dict[str, Any]:
    target = validate_production_target(target)
    item = _require_exact_keys(
        value,
        {
            "schema_version", "provider", "target", "target_binding_sha256",
            "team_id", "project_id", "project_name", "deployment_id",
            "deployment_url", "metadata", "create_raw_sha256",
            "attestation_sha256",
        },
        "Vercel create attestation",
    )
    expected = target["vercel"]
    valid = (
        item.get("schema_version") == "1"
        and item.get("provider") == "vercel"
        and item.get("target") == "production"
        and item.get("target_binding_sha256") == target["binding_sha256"]
        and item.get("team_id") == expected["team_id"]
        and item.get("project_id") == expected["project_id"]
        and item.get("project_name") == expected["project_name"]
        and isinstance(item.get("deployment_id"), str)
        and bool(VERCEL_ID_RE.fullmatch(item["deployment_id"]))
        and _https_url(item.get("deployment_url")) == item.get("deployment_url")
        and isinstance(item.get("metadata"), dict)
        and set(item["metadata"]) == {
            "gclProxyRevision", "gclRequestSha256", "gclConfigSha256",
        }
        and all(isinstance(value, str) and value for value in item["metadata"].values())
        and bool(PROXY_REVISION_RE.fullmatch(item["metadata"]["gclProxyRevision"]))
        and bool(SHA256_RE.fullmatch(item["metadata"]["gclRequestSha256"]))
        and bool(SHA256_RE.fullmatch(item["metadata"]["gclConfigSha256"]))
        and isinstance(item.get("create_raw_sha256"), str)
        and bool(SHA256_RE.fullmatch(item["create_raw_sha256"]))
        and item.get("attestation_sha256") == _receipt_hash(item, "attestation_sha256")
    )
    if not valid:
        raise ProviderReadbackError(
            "invalid_evidence", "vercel", "create attestation", "binding mismatch"
        )
    return json.loads(_canonical_bytes(item))


def _provider_object(
    value: Any, *, provider: str, operation: str
) -> dict[str, Any]:
    status = 200
    body = value
    if isinstance(value, ProviderResponse):
        status, body = value.status_code, value.body
    if status == 404:
        raise ProviderReadbackError(
            "provider_not_found", provider, operation, "resource not found"
        )
    if status != 200:
        raise ProviderReadbackError("provider_error", provider, operation, "request failed")
    if not isinstance(body, dict):
        raise ProviderReadbackError(
            "invalid_readback", provider, operation, "provider returned a non-object"
        )
    return body


def _team_identity(value: Mapping[str, Any], *names: str) -> str | None:
    seen = {value[name] for name in names if isinstance(value.get(name), str)}
    return next(iter(seen)) if len(seen) == 1 else None


def _aliases(value: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    aliases = value.get("alias", value.get("aliases", []))
    if not isinstance(aliases, list):
        return result
    for item in aliases:
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            candidate = item.get("domain", item.get("name"))
        else:
            continue
        url = _https_url(candidate)
        if url:
            result.add(url.removeprefix("https://"))
    return result


def validate_vercel_provider_receipt(
    value: Any,
    *,
    target: Mapping[str, Any],
    create_attestation: Mapping[str, Any],
) -> dict[str, Any]:
    target = validate_production_target(target)
    create = validate_vercel_create_attestation(create_attestation, target=target)
    receipt = _require_exact_keys(
        value,
        {
            "schema_version", "provider", "target", "target_binding_sha256",
            "team_id", "project_id", "project_name", "deployment_id",
            "deployment_url", "deployment_ready_state", "current_production_target",
            "stable_domain", "checked_at", "raw_response_sha256", "receipt_sha256",
        },
        "Vercel provider receipt",
    )
    raw = _require_exact_keys(
        receipt["raw_response_sha256"],
        {"create_attestation", "deployment_readback", "project_readback"},
        "Vercel raw hashes",
    )
    expected = target["vercel"]
    valid = (
        receipt.get("schema_version") == "1"
        and receipt.get("provider") == "vercel"
        and receipt.get("target") == "production"
        and receipt.get("target_binding_sha256") == target["binding_sha256"]
        and receipt.get("team_id") == expected["team_id"]
        and receipt.get("project_id") == expected["project_id"]
        and receipt.get("project_name") == expected["project_name"]
        and receipt.get("deployment_id") == create["deployment_id"]
        and receipt.get("deployment_url") == create["deployment_url"]
        and receipt.get("deployment_ready_state") == "READY"
        and receipt.get("current_production_target") == create["deployment_id"]
        and receipt.get("stable_domain") == expected["stable_domain"]
        and raw.get("create_attestation") == create["attestation_sha256"]
        and all(isinstance(item, str) and SHA256_RE.fullmatch(item) for item in raw.values())
        and receipt.get("receipt_sha256") == _receipt_hash(receipt, "receipt_sha256")
    )
    if not valid:
        raise ProviderReadbackError(
            "invalid_evidence", "vercel", "receipt validation", "receipt binding mismatch"
        )
    return json.loads(_canonical_bytes(receipt))


class VercelProviderReader:
    """Verify the created deployment against two independent Vercel read-backs."""

    def __init__(
        self,
        target: Mapping[str, Any],
        *,
        get_deployment: Callable[[str, str], Any],
        get_project: Callable[[str, str], Any],
        clock: Callable[[], str] = _now,
    ):
        self.target = validate_production_target(target)
        self.get_deployment = get_deployment
        self.get_project = get_project
        self.clock = clock

    def read(self, create_attestation: Mapping[str, Any]) -> dict[str, Any]:
        create = validate_vercel_create_attestation(
            create_attestation, target=self.target
        )
        deployment = _provider_object(
            self.get_deployment(create["deployment_id"], create["team_id"]),
            provider="vercel",
            operation="get deployment",
        )
        project = _provider_object(
            self.get_project(create["project_id"], create["team_id"]),
            provider="vercel",
            operation="get project",
        )
        expected = self.target["vercel"]
        deployment_team = _team_identity(deployment, "teamId", "ownerId")
        project_team = _team_identity(project, "teamId", "accountId")
        production = project.get("targets", {}).get("production") if isinstance(project.get("targets"), dict) else None
        production_id = production.get("id") if isinstance(production, dict) else None
        deployment_url = _https_url(deployment.get("url"))
        stable_domain = expected["stable_domain"]
        valid = (
            deployment.get("id") == create["deployment_id"]
            and deployment.get("projectId") == expected["project_id"]
            and deployment.get("name") == expected["project_name"]
            and deployment_team == expected["team_id"]
            and deployment.get("target") == "production"
            and deployment.get("readyState") == "READY"
            and deployment.get("meta") == create["metadata"]
            and deployment_url == create["deployment_url"]
            and project.get("id") == expected["project_id"]
            and project.get("name") == expected["project_name"]
            and project_team == expected["team_id"]
            and production_id == create["deployment_id"]
            and stable_domain in _aliases(deployment)
            and stable_domain in _aliases(project)
        )
        if not valid:
            raise ProviderReadbackError(
                "target_mismatch", "vercel", "read-back",
                "deployment is not READY at the fixed current production target and stable domain",
            )
        receipt: dict[str, Any] = {
            "schema_version": "1",
            "provider": "vercel",
            "target": "production",
            "target_binding_sha256": self.target["binding_sha256"],
            "team_id": expected["team_id"],
            "project_id": expected["project_id"],
            "project_name": expected["project_name"],
            "deployment_id": create["deployment_id"],
            "deployment_url": create["deployment_url"],
            "deployment_ready_state": "READY",
            "current_production_target": production_id,
            "stable_domain": stable_domain,
            "checked_at": self.clock(),
            "raw_response_sha256": {
                "create_attestation": create["attestation_sha256"],
                "deployment_readback": canonical_sha256(deployment),
                "project_readback": canonical_sha256(project),
            },
        }
        receipt["receipt_sha256"] = _receipt_hash(receipt, "receipt_sha256")
        return validate_vercel_provider_receipt(
            receipt, target=self.target, create_attestation=create
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, request, file_pointer, code, message, headers, new_url,
    ):
        return None


class VercelRestProviderReader:
    """Read Vercel production state directly from authenticated REST APIs."""

    def __init__(
        self,
        target: Mapping[str, Any],
        *,
        token: str,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], str] = _now,
        attempts: int = 1,
        retry_delay_seconds: float = 0,
        sleeper: Callable[[float], None] = lambda _seconds: None,
    ):
        self.target = validate_production_target(target)
        if not isinstance(token, str) or not (16 <= len(token) <= 512) or any(
            character.isspace() for character in token
        ):
            raise ProviderReadbackError(
                "invalid_request", "vercel", "REST authentication", "token is malformed"
            )
        self.token = token
        self.opener = opener or urllib.request.build_opener(_NoRedirect()).open
        self.clock = clock
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 60:
            raise ProviderReadbackError(
                "invalid_request", "vercel", "REST read-back", "attempt count is invalid"
            )
        if retry_delay_seconds < 0 or retry_delay_seconds > 30:
            raise ProviderReadbackError(
                "invalid_request", "vercel", "REST read-back", "retry delay is invalid"
            )
        self.attempts = attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.sleeper = sleeper

    def _get(self, path: str, query: Mapping[str, str]) -> ProviderResponse:
        url = "https://api.vercel.com" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/json",
                "User-Agent": "garmin-coach-loop-release/1",
            },
            method="GET",
        )
        try:
            with self.opener(request, timeout=20) as response:
                if response.geturl() != url:
                    return ProviderResponse(502, None)
                try:
                    body = json.loads(response.read())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return ProviderResponse(response.status, None)
                return ProviderResponse(response.status, body)
        except urllib.error.HTTPError as exc:
            return ProviderResponse(exc.code, None)
        except (OSError, TimeoutError) as exc:
            raise ProviderReadbackError(
                "provider_error", "vercel", "REST read-back", "request failed"
            ) from exc

    def _read_once(self, create_attestation: Mapping[str, Any]) -> dict[str, Any]:
        create = validate_vercel_create_attestation(
            create_attestation, target=self.target
        )
        expected = self.target["vercel"]
        query = {"teamId": expected["team_id"]}
        deployment = _provider_object(
            self._get(f"/v13/deployments/{create['deployment_id']}", query),
            provider="vercel", operation="get deployment",
        )
        project = _provider_object(
            self._get(f"/v9/projects/{expected['project_id']}", query),
            provider="vercel", operation="get project",
        )
        stable = expected["stable_domain"]
        stable_alias = _provider_object(
            self._get(
                "/v4/aliases/" + urllib.parse.quote(stable, safe=""), query,
            ),
            provider="vercel", operation="get stable production alias",
        )
        aliases = _provider_object(
            self._get(f"/v2/deployments/{create['deployment_id']}/aliases", query),
            provider="vercel", operation="get deployment aliases",
        )
        domains = _provider_object(
            self._get(
                f"/v9/projects/{expected['project_id']}/domains",
                {**query, "production": "true", "limit": "100"},
            ),
            provider="vercel", operation="get project domains",
        )
        alias_values = aliases.get("aliases")
        domain_values = domains.get("domains")
        if not isinstance(alias_values, list) or not isinstance(domain_values, list):
            raise ProviderReadbackError(
                "invalid_readback", "vercel", "REST read-back", "alias schema is incomplete"
            )
        deployment_aliases = [
            item.get("alias") for item in alias_values
            if isinstance(item, dict) and isinstance(item.get("alias"), str)
        ]
        project_domains = [
            item.get("name") for item in domain_values
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        if (
            stable_alias.get("alias") != stable
            or stable_alias.get("deploymentId") != create["deployment_id"]
            or stable_alias.get("projectId") != expected["project_id"]
            or stable not in deployment_aliases
            or stable not in project_domains
        ):
            raise ProviderReadbackError(
                "target_mismatch", "vercel", "REST read-back",
                "stable production domain is not assigned to this deployment and project",
            )
        normalized_deployment = {
            "id": deployment.get("id"),
            "projectId": deployment.get("projectId"),
            "name": deployment.get("name"),
            "teamId": _team_identity(deployment, "teamId", "ownerId"),
            "target": deployment.get("target"),
            "readyState": deployment.get("readyState", deployment.get("status")),
            "url": deployment.get("url"),
            "meta": deployment.get("meta"),
            "alias": deployment_aliases,
            "rest_response_sha256": canonical_sha256(deployment),
            "alias_response_sha256": canonical_sha256(aliases),
        }
        normalized_project = {
            "id": project.get("id"),
            "name": project.get("name"),
            "accountId": _team_identity(project, "accountId", "teamId"),
            "targets": {
                "production": {"id": stable_alias.get("deploymentId")},
            },
            "alias": project_domains,
            "rest_response_sha256": canonical_sha256(project),
            "stable_alias_response_sha256": canonical_sha256(stable_alias),
            "domain_response_sha256": canonical_sha256(domains),
        }
        return VercelProviderReader(
            self.target,
            get_deployment=lambda _deployment_id, _team_id: normalized_deployment,
            get_project=lambda _project_id, _team_id: normalized_project,
            clock=self.clock,
        ).read(create)

    def read(self, create_attestation: Mapping[str, Any]) -> dict[str, Any]:
        for attempt in range(1, self.attempts + 1):
            try:
                return self._read_once(create_attestation)
            except ProviderReadbackError as exc:
                if (
                    attempt == self.attempts
                    or exc.code not in {"provider_not_found", "target_mismatch"}
                ):
                    raise
                self.sleeper(self.retry_delay_seconds)
        raise AssertionError("unreachable")


__all__ = [
    "GITHUB_BRANCH", "GITHUB_WORKFLOW_PATH", "GitHubProviderReader",
    "ProviderReadbackError", "ProviderResponse", "VercelProviderReader",
    "VercelRestProviderReader",
    "canonical_sha256", "load_production_target",
    "normalize_vercel_create_attestation", "production_target_binding",
    "validate_github_provider_receipt", "validate_production_target",
    "validate_vercel_create_attestation", "validate_vercel_provider_receipt",
]
