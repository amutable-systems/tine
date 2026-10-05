#!/usr/bin/python3

# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Set up the deploy key that bot workflows push with.

Pushes with the default GITHUB_TOKEN start no workflows, so CI never runs on a branch a bot workflow
pushes that way; a push over SSH with a deploy key does start them. This script adds a write deploy key
to the repository, and puts its private half into the "self" environment as the DEPLOY_KEY secret. That
environment only runs from the default branch, so a workflow on any other branch cannot read the key. A
workflow uses it with `environment: self` and actions/checkout's `ssh-key: ${{ secrets.DEPLOY_KEY }}`.
Running this script again rotates the key.

Needs a `gh` login with admin rights on the repository. GitHub ties a deploy key to the token that added
it, and removes it when that token is revoked (see `gh repo deploy-key add --help`); run this script
again then.
"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

ENVIRONMENT = "self"
SECRET = "DEPLOY_KEY"
TITLE = "push to our own repo"


def gh(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["gh", *args], check=True, input=stdin, stdout=subprocess.PIPE, text=True).stdout


def main() -> None:
    parser = argparse.ArgumentParser(prog="setup-deploy-key", description=__doc__.splitlines()[0])
    parser.add_argument(
        "repo",
        nargs="?",
        metavar="OWNER/REPO",
        help="the repository (default: the one of the current checkout)",
    )
    args = parser.parse_args()
    # without a repository argument, gh asks the checkout it runs in
    info = json.loads(
        gh("repo", "view", *([args.repo] if args.repo else []), "--json", "nameWithOwner,defaultBranchRef")
    )
    repo = info["nameWithOwner"]
    default_branch = info["defaultBranchRef"]["name"]

    with tempfile.TemporaryDirectory() as directory:
        key = Path(directory) / "key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", repo, "-f", str(key)], check=True
        )

        # Clean up the previous key with the same name, for rotation. Through the API, as `gh repo
        # deploy-key list --json` prints nothing at all for no keys.
        pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/keys"))
        for deploy_key in (entry for page in pages for entry in page):
            if deploy_key["title"] == TITLE:
                gh("repo", "deploy-key", "delete", "--repo", repo, str(deploy_key["id"]))

        gh("repo", "deploy-key", "add", "--repo", repo, "--allow-write", "--title", TITLE, f"{key}.pub")

        environment = f"repos/{repo}/environments/{ENVIRONMENT}"
        policy = {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        gh("api", "--method", "PUT", environment, "--input", "-", stdin=json.dumps(policy))
        policies = json.loads(gh("api", f"{environment}/deployment-branch-policies"))["branch_policies"]
        if not any(entry["name"] == default_branch and entry["type"] == "branch" for entry in policies):
            policy_args = ["-f", f"name={default_branch}", "-f", "type=branch"]
            gh("api", "--method", "POST", f"{environment}/deployment-branch-policies", *policy_args)

        gh("secret", "set", SECRET, "--repo", repo, "--env", ENVIRONMENT, stdin=key.read_text())

    print(f"{repo}: deploy key set up; workflows read it from the {ENVIRONMENT} environment's {SECRET}")


if __name__ == "__main__":
    main()
