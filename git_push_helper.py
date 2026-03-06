#!/usr/bin/env python3
"""
git_push_helper.py — Shared module for GitHub push operations using TokenManager.

Usage:
    from git_push_helper import get_token_for_repo, git_push

    # Get the best token for a repo
    token = get_token_for_repo("owner/repo")

    # Or push directly
    success = git_push("owner/repo", branch="main", commit_msg="update content")

Backward compatible: falls back to reading credentials/github_token.txt if
TokenManager config is not available.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

# Add parent dir so we can import github_token_manager
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from github_token_manager import (
    TokenConfig,
    mask_token,
    select_token,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
WORKSPACE = _SCRIPT_DIR  # tools/ lives in workspace root
CONFIG_PATH = WORKSPACE / "credentials" / "token_config.json"
SEO_REPOS_PATH = WORKSPACE / "credentials" / "github_seo_repos.json"
LEGACY_TOKEN_PATH = WORKSPACE / "credentials" / "github_token.txt"
FALLBACK_TOKEN_PATH = WORKSPACE / "credentials" / "github_token_fire_gains.txt"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> TokenConfig:
    """Load token config, merging SEO repo overrides if available."""
    if CONFIG_PATH.exists():
        config = TokenConfig.from_json(CONFIG_PATH)
    else:
        logger.warning("Token config not found at %s, falling back to env vars", CONFIG_PATH)
        config = TokenConfig.from_env()

    # Merge SEO repo overrides if the map file exists
    if SEO_REPOS_PATH.exists():
        try:
            seo_map = json.loads(SEO_REPOS_PATH.read_text(encoding="utf-8"))
            for repo, preferred_alias in seo_map.items():
                if repo.startswith("_"):  # skip comment keys
                    continue
                config.repo_overrides[repo] = preferred_alias
            logger.info("Merged %d SEO repo overrides from %s",
                        len([k for k in seo_map if not k.startswith("_")]),
                        SEO_REPOS_PATH.name)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load SEO repo overrides: %s", e)

    # Inject file-based token values if env vars are empty
    _inject_file_tokens(config)

    return config


def _inject_file_tokens(config: TokenConfig) -> None:
    """Read token values from files if they exist and entry has no resolved value.

    Reads file_path from config entry first; falls back to legacy hardcoded paths
    for backward compatibility.
    """
    legacy_file_map = {
        "classic": LEGACY_TOKEN_PATH,
        "fire_gains": FALLBACK_TOKEN_PATH,
    }
    for entry in config.tokens:
        if entry.resolve():
            continue  # already has a value from env

        # Try config-driven file_path first
        token_file = None
        if entry.file_path:
            candidate = WORKSPACE / entry.file_path
            if candidate.exists():
                token_file = candidate

        # Fall back to legacy hardcoded paths
        if token_file is None:
            legacy = legacy_file_map.get(entry.alias)
            if legacy and legacy.exists():
                token_file = legacy

        if token_file:
            raw = token_file.read_text(encoding="utf-8").strip()
            if raw:
                entry.value = raw
                logger.info("Loaded token '%s' from file %s (%s)",
                            entry.alias, token_file.name, mask_token(raw))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_token_for_repo(repo: str, verbose: bool = False) -> str | None:
    """Get the best GitHub token for a given repo.

    Args:
        repo: Repository in "owner/repo" format.
        verbose: Print detailed selection logs.

    Returns:
        Token string if found, None if all tokens fail.
    """
    config = _load_config()
    result = select_token(config, repo, verbose=verbose)

    if result.success:
        logger.info("✅ Token selected for %s: '%s' (%s)",
                     repo, result.alias, mask_token(result.token))
        return result.token
    else:
        logger.error("❌ All tokens failed for %s", repo)
        for attempt in result.attempts:
            logger.error("  %s: %s — %s",
                         attempt.get("alias", "?"),
                         attempt.get("status", "?"),
                         attempt.get("message", ""))
        return None


def git_push(
    repo: str,
    repo_dir: str | Path = ".",
    branch: str = "main",
    commit_msg: str = "auto update",
    verbose: bool = False,
) -> bool:
    """Stage, commit, and push changes using TokenManager.

    Args:
        repo: Repository in "owner/repo" format (e.g. "user/my-seo-site").
        repo_dir: Local directory of the git repo.
        branch: Branch to push to.
        commit_msg: Commit message.
        verbose: Print detailed logs.

    Returns:
        True if push succeeded, False otherwise.
    """
    token = get_token_for_repo(repo, verbose=verbose)
    if not token:
        logger.error("Cannot push — no valid token for %s", repo)
        return False

    repo_dir = Path(repo_dir).resolve()
    remote_url = f"https://x-access-token:{token}@github.com/{repo}.git"

    try:
        # Stage all changes
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_dir, check=True, capture_output=True, text=True,
        )

        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir, check=True, capture_output=True, text=True,
        )
        if not status.stdout.strip():
            logger.info("Nothing to commit for %s", repo)
            return True

        # Commit
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_dir, check=True, capture_output=True, text=True,
        )

        # Push using token-authenticated URL
        subprocess.run(
            ["git", "push", remote_url, f"HEAD:{branch}"],
            cwd=repo_dir, check=True, capture_output=True, text=True,
        )

        logger.info("✅ Pushed to %s/%s successfully (token: %s)",
                     repo, branch, mask_token(token))
        return True

    except subprocess.CalledProcessError as e:
        # Sanitize stderr to avoid leaking token
        stderr = (e.stderr or "").replace(token, mask_token(token))
        logger.error("❌ Git push failed for %s: %s", repo, stderr)
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for testing."""
    import argparse

    parser = argparse.ArgumentParser(description="GitHub push helper using TokenManager")
    sub = parser.add_subparsers(dest="command")

    # get-token
    gt = sub.add_parser("get-token", help="Get the best token for a repo")
    gt.add_argument("repo", help="owner/repo format")
    gt.add_argument("-v", "--verbose", action="store_true")

    # push
    ps = sub.add_parser("push", help="Git add, commit, push")
    ps.add_argument("repo", help="owner/repo format")
    ps.add_argument("--dir", default=".", help="Local repo directory")
    ps.add_argument("--branch", default="main")
    ps.add_argument("--msg", default="auto update", help="Commit message")
    ps.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.command == "get-token":
        token = get_token_for_repo(args.repo, verbose=args.verbose)
        if token:
            print(f"Token for {args.repo}: {mask_token(token)}")
        else:
            print(f"No valid token for {args.repo}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "push":
        ok = git_push(args.repo, repo_dir=args.dir, branch=args.branch,
                      commit_msg=args.msg, verbose=args.verbose)
        sys.exit(0 if ok else 1)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
