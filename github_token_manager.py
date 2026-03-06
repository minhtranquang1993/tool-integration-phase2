#!/usr/bin/env python3
"""
github_token_manager.py — GitHub token auto-selection with fallback policy.

Features:
- Token registry from JSON config or environment variables
- Repo-specific override (owner/repo → preferred token alias)
- Auto-selection: try classic → fallback fine-grained
- Masked logging (never expose raw tokens)
- CLI interface for testing and validation

Spec: 2026-03-05-editor-improvements-for-ni (item 1)
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Token masking
# ---------------------------------------------------------------------------

def mask_token(token: str, visible_chars: int = 4) -> str:
    """Mask a token, showing only the last N characters.

    Examples:
        >>> mask_token("ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1234")
        'ghp_****1234'
        >>> mask_token("github_pat_abc123def456")
        'github_pat_****f456'
    """
    if not token or len(token) <= visible_chars:
        return "****"
    prefix = ""
    # Preserve known prefixes for readability
    for p in ("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_"):
        if token.startswith(p):
            prefix = p
            break
    suffix = token[-visible_chars:]
    return f"{prefix}****{suffix}"


# ---------------------------------------------------------------------------
# Token config
# ---------------------------------------------------------------------------

@dataclass
class TokenEntry:
    """A single token source."""
    alias: str
    env_var: str
    value: str | None = None
    file_path: str | None = None  # optional path to token file

    def resolve(self) -> str | None:
        """Resolve token value from env var or direct value."""
        if self.value:
            return self.value
        return os.environ.get(self.env_var)


@dataclass
class TokenConfig:
    """Token configuration with ordered list and repo overrides."""
    tokens: list[TokenEntry] = field(default_factory=list)
    repo_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, config_path: Path) -> "TokenConfig":
        """Load config from JSON file."""
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            print(f"⚠️  Config file not found: {config_path}", file=sys.stderr)
            return cls.from_env()
        except json.JSONDecodeError as e:
            print(f"❌ Invalid JSON in {config_path}: {e}", file=sys.stderr)
            return cls.from_env()

        tokens = []
        for alias, info in data.get("tokens", {}).items():
            env_var = info.get("env_var", "")
            direct_value = info.get("value")  # optional, for testing
            file_path = info.get("file_path")  # optional, path to token file
            tokens.append(TokenEntry(alias=alias, env_var=env_var, value=direct_value, file_path=file_path))

        repo_overrides = data.get("repo_overrides", {})

        return cls(tokens=tokens, repo_overrides=repo_overrides)

    @classmethod
    def from_env(cls) -> "TokenConfig":
        """Create config from default environment variables."""
        tokens = [
            TokenEntry(alias="classic", env_var="GITHUB_TOKEN_CLASSIC"),
            TokenEntry(alias="finegrained", env_var="GITHUB_TOKEN_FINEGRAINED"),
        ]
        return cls(tokens=tokens)


# ---------------------------------------------------------------------------
# GitHub API validation
# ---------------------------------------------------------------------------

def validate_token_for_repo(token: str, repo: str, timeout: int = 10) -> tuple[bool, int, str]:
    """Validate a token can access a specific repo.

    Args:
        token: GitHub personal access token
        repo: Repository in "owner/repo" format
        timeout: Request timeout in seconds

    Returns:
        (success, http_status, message)
    """
    url = f"https://api.github.com/repos/{repo}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ni-editor-token-manager/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            if status == 200:
                return True, status, "Access granted"
            return False, status, f"Unexpected status: {status}"
    except urllib.error.HTTPError as e:
        status = e.code
        messages = {
            401: "Invalid or expired token",
            403: "Token lacks required permissions",
            404: "Repo not found or no access",
        }
        msg = messages.get(status, f"HTTP error: {status}")
        return False, status, msg
    except urllib.error.URLError as e:
        return False, 0, f"Network error: {e.reason}"
    except Exception as e:
        return False, 0, f"Unexpected error: {e}"


# ---------------------------------------------------------------------------
# Token selection engine
# ---------------------------------------------------------------------------

@dataclass
class SelectionResult:
    """Result of token auto-selection."""
    success: bool
    token: str | None
    alias: str | None
    repo: str
    http_status: int
    message: str
    attempts: list[dict] = field(default_factory=list)


def select_token(config: TokenConfig, repo: str, verbose: bool = False) -> SelectionResult:
    """Auto-select the best token for a given repo.

    Policy:
    1. If repo has an override → try that token first
    2. Try tokens in order (classic → fine-grained)
    3. Return first successful token

    Args:
        config: Token configuration
        repo: Repository in "owner/repo" format
        verbose: Print detailed logs

    Returns:
        SelectionResult with the selected token info
    """
    attempts = []

    # Build ordered list of tokens to try
    ordered_aliases = []

    # Check repo-specific override
    override_alias = config.repo_overrides.get(repo)
    if override_alias:
        ordered_aliases.append(override_alias)
        if verbose:
            print(f"🔑 Repo override: {repo} → prefer '{override_alias}'")

    # Add remaining tokens in config order
    for t in config.tokens:
        if t.alias not in ordered_aliases:
            ordered_aliases.append(t.alias)

    # Build alias → TokenEntry map
    token_map = {t.alias: t for t in config.tokens}

    # Try each token in order
    for alias in ordered_aliases:
        entry = token_map.get(alias)
        if not entry:
            attempts.append({
                "alias": alias,
                "status": "SKIP",
                "message": f"Token alias '{alias}' not found in config",
            })
            if verbose:
                print(f"  ⏭️  [{alias}] Not found in config — skipped")
            continue

        token_value = entry.resolve()
        if not token_value:
            attempts.append({
                "alias": alias,
                "status": "SKIP",
                "message": f"No value for '{alias}' (env: {entry.env_var})",
            })
            if verbose:
                masked = entry.env_var
                print(f"  ⏭️  [{alias}] No value (env: {masked}) — skipped")
            continue

        masked = mask_token(token_value)
        if verbose:
            print(f"  🔐 [{alias}] Trying {masked} ...")

        success, status, message = validate_token_for_repo(token_value, repo)

        attempt_record = {
            "alias": alias,
            "token_masked": masked,
            "status": "PASS" if success else "FAIL",
            "http_status": status,
            "message": message,
        }
        attempts.append(attempt_record)

        if success:
            if verbose:
                print(f"  ✅ [{alias}] {masked} → {message} (HTTP {status})")
            return SelectionResult(
                success=True,
                token=token_value,
                alias=alias,
                repo=repo,
                http_status=status,
                message=f"Selected token '{alias}' ({masked})",
                attempts=attempts,
            )
        else:
            if verbose:
                print(f"  ❌ [{alias}] {masked} → {message} (HTTP {status})")

    # All tokens failed
    return SelectionResult(
        success=False,
        token=None,
        alias=None,
        repo=repo,
        http_status=0,
        message=f"All tokens failed for repo '{repo}'",
        attempts=attempts,
    )


# ---------------------------------------------------------------------------
# Report formatters
# ---------------------------------------------------------------------------

def format_report_text(result: SelectionResult) -> str:
    """Format selection result as human-readable text."""
    lines = []
    lines.append(f"{'═' * 50}")
    lines.append(f"🔑 GitHub Token Selection Report")
    lines.append(f"{'═' * 50}")
    lines.append(f"Repo: {result.repo}")
    lines.append(f"Result: {'✅ SUCCESS' if result.success else '❌ FAILED'}")
    lines.append(f"Message: {result.message}")
    lines.append("")
    lines.append("Attempts:")
    for i, a in enumerate(result.attempts, 1):
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}.get(a["status"], "?")
        masked = a.get("token_masked", "N/A")
        http_s = a.get("http_status", "")
        http_part = f" (HTTP {http_s})" if http_s else ""
        lines.append(f"  {i}. {icon} [{a['alias']}] {masked}{http_part} — {a['message']}")
    lines.append(f"{'═' * 50}")
    return "\n".join(lines)


def format_report_json(result: SelectionResult) -> str:
    """Format selection result as JSON."""
    return json.dumps({
        "success": result.success,
        "repo": result.repo,
        "selected_alias": result.alias,
        "selected_token_masked": mask_token(result.token) if result.token else None,
        "http_status": result.http_status,
        "message": result.message,
        "attempts": result.attempts,
    }, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="GitHub token auto-selection — tries tokens by policy until one works",
    )
    parser.add_argument(
        "--repo", type=str, required=True,
        help="Repository in owner/repo format (e.g., minhtranquang1993/my-repo)",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to token_config.json (default: auto-detect from env vars)",
    )
    parser.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print detailed selection logs",
    )

    args = parser.parse_args()

    # Load config
    if args.config:
        config = TokenConfig.from_json(Path(args.config))
    else:
        config = TokenConfig.from_env()

    # Run selection
    result = select_token(config, args.repo, verbose=args.verbose)

    # Output report
    if args.format == "json":
        print(format_report_json(result))
    else:
        print(format_report_text(result))

    # Exit code
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
