# SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
# SPDX-License-Identifier: MPL-2.0

"""Make one automatic change and open the pull request for it.

Only one run of each kind is ever in flight: while a pull request or a failure issue carrying the run's
label is open, this does nothing.

 - If the command fails without generating a commit, open an issue and pass on the failing exit code.
 - If the command fails after committing, open a draft PR with a note, and pass on the failing exit code.
 - Otherwise, open a regular PR and exit zero.

It pushes to origin with the deploy key of tools/setup_deploy_key.py (so that CI starts), and opens the
pull request with the given token (usually the default GITHUB_TOKEN).

Everything a workflow decides is an option, so that `--help` is the whole contract. The rest comes from
what Actions sets around the run: GITHUB_SERVER_URL and GITHUB_RUN_ID to link back to it, and
GITHUB_REF_NAME as the branch the pull request goes against.

Needs only git, gh and a host python: it runs before the project's build system has fetched anything.
"""

import argparse
import os
import subprocess
import time
from pathlib import Path

# Actions points this at the checkout. Everything below runs there rather than wherever the caller
# happened to leave the working directory.
ROOT = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()

# The identity the bot's commits show.
AUTHOR = ("github-actions[bot]", "noreply@amutable.com")


def _output(command: list[str]) -> str:
    """Stripped stdout of a command, with its stderr left going to the log.

    UTF-8 rather than the locale's encoding: a commit message this reads into a pull request is
    git's bytes, and what a runner names in LANG is no business of the title it ends up with.
    """
    run = subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.PIPE, encoding="utf-8")
    return run.stdout.strip()


def git(*args: str) -> str:
    return _output(["git", *args])


def gh(*args: str) -> str:
    return _output(["gh", *args])


def run_url(repository: str) -> str:
    """This workflow run, for a human following a link out of what we open."""
    server = os.environ["GITHUB_SERVER_URL"]
    return f"{server}/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


def ensure_label(label: str, description: str) -> None:
    """Create the label every run of this kind is found by; gh does not create one implicitly."""
    if not gh("label", "list", "--search", label):
        gh("label", "create", label, "--color", "ededed", "--description", description)


def blocked(label: str) -> bool:
    """Whether something carrying the label is still open, and this run therefore has nothing to do."""
    # The REST API covers pull requests as well, unlike `gh issue list`.
    query = f"repos/{{owner}}/{{repo}}/issues?state=open&labels={label}"
    open_items = gh("api", query, "--jq", r'.[] | "::notice::#\(.number) \(.title) is still open"')
    if open_items:
        print(open_items)
    return bool(open_items)


def commit_change(command: str) -> tuple[str | None, int]:
    """Run the command that commits the change.

    Returns the commit it started from, None if it made none, and the command's exit status.
    """
    git("config", "user.name", AUTHOR[0])
    git("config", "user.email", AUTHOR[1])
    before = git("rev-parse", "HEAD")
    status = subprocess.run(command, shell=True, cwd=ROOT).returncode
    return (before if git("rev-parse", "HEAD") != before else None), status


def start_ci(name: str) -> None:
    """Re-push the branch of a pull request with ssh, so that its CI runs.

    A pull request opened with the default GITHUB_TOKEN does not start CI workflows.
    """
    # If we push before GitHub creates the approval-waiting run of the "opened" event, the PR only shows
    # the latter. To avoid the race, wait for the `pull_request` event run to appear.
    pushed = git("rev-parse", "HEAD")
    runs = f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={pushed}&event=pull_request"
    deadline = time.monotonic() + 300
    while not int(gh("api", runs, "--jq", ".total_count")):
        if time.monotonic() > deadline:
            raise SystemExit(f"bot_pr: no pull_request run for {pushed} after 5 minutes")
        time.sleep(5)

    # needs to change something to get a different SHA, so bump commit time
    later = int(git("log", "-1", "--format=%ct")) + 1
    amend = ["git", "commit", "--amend", "--no-edit"]
    subprocess.run(amend, cwd=ROOT, check=True, env=os.environ | {"GIT_COMMITTER_DATE": f"{later} +0000"})
    git("push", "--force", "origin", f"HEAD:refs/heads/{name}")


def open_pull_request(name: str, label: str, before: str, repository: str, draft_note: str) -> str:
    """Push what the command committed to the branch this run owns, and open its pull request.

    A non-empty draft_note opens it as a draft, with the note as the body's footer.
    """
    # The gate leaves at most one run of each kind in flight, so the branch is this run's own to
    # overwrite: the previous one's may still be there, merged and never deleted.
    git("push", "--force", "origin", f"HEAD:refs/heads/{name}")
    # --reverse: git log is newest-first, and these read in the order they were made.
    span = f"{before}..HEAD"
    log = git("log", "--reverse", "--format=%s%n%n%b", span)
    title = "; ".join(git("log", "--reverse", "--format=%s", span).splitlines())
    note = f"{draft_note}\n\n" if draft_note else ""
    # The blank line matters: a commit body ending in a list would swallow the line after it.
    body = f"{log}\n\n{note}Opened by {run_url(repository)}.\n"
    base = os.environ["GITHUB_REF_NAME"]
    options = ["--head", name, "--base", base, "--label", label, "--title", title, "--body", body]
    url = gh("pr", "create", *options, *(["--draft"] if draft_note else []))
    # A branch already known to be broken is not worth a CI run
    if not draft_note:
        start_ci(name)
    return url.rsplit("/", 1)[-1]


def report_failure(name: str, label: str, repository: str) -> str:
    """Open the issue that keeps runs of this kind paused until somebody closes it; its URL."""
    # Only ever one at a time, which the gate takes care of by looking for this same label.
    paused = f"Nothing was committed; {name} stays paused until this issue is closed."
    body = f"{paused}\n\n{run_url(repository)}\n"
    return gh("issue", "create", "--label", label, "--title", f"{name} is failing", "--body", body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="what this run is, e.g. bump")
    parser.add_argument("--label-description", required=True, help="what the label bot-<name> means")
    parser.add_argument("--command", required=True, metavar="COMMAND", help="how to make the change")
    parser.add_argument(
        "--command-fail-note",
        default="",
        metavar="TEXT",
        help="draft PR description if --command fails after committing (default: name its exit status)",
    )
    parser.add_argument("--repo", required=True, metavar="OWNER/REPO", help="the repository to work on")
    parser.add_argument("--token", required=True, help="token to reach the repository with")
    args = parser.parse_args()

    # gh reads both of these from the environment, so put them there once for every child below.
    os.environ["GH_TOKEN"] = args.token
    os.environ["GH_REPO"] = args.repo

    label = f"bot-{args.name}"
    ensure_label(label, args.label_description)
    if blocked(label):
        return

    before, status = commit_change(args.command)
    if before is None:
        # Nothing committed is nothing to open, which is what the issue is for. Anything else
        # failing - a missing variable, a gh that cannot talk to the API - is this driver or its
        # workflow being wrong, and belongs in a traceback rather than in an issue.
        if status:
            print(f"::notice::opened {report_failure(args.name, label, args.repo)}")
            raise SystemExit(status)
        print(f"::notice::{args.name} found nothing to change")
        return

    note = (args.command_fail_note or f"The command failed with exit status {status}.") if status else ""
    pull_request = open_pull_request(args.name, label, before, args.repo, note)
    # A draft is a run that went wrong, so it is worth an annotation rather than a line in a log.
    kind = "::warning::opened draft" if note else "::notice::opened"
    print(f"{kind} #{pull_request}")
    # What a caller with something left to do about the pull request reads.
    step_output = os.environ.get("GITHUB_OUTPUT")
    if step_output:
        with Path(step_output).open("a") as handle:
            handle.write(f"pull-request={pull_request}\ndraft={str(bool(note)).lower()}\n")
    # Whatever failed fails the run as well, after the draft is open: a job that goes green is one
    # nobody looks at. Zero when nothing did.
    raise SystemExit(status)


if __name__ == "__main__":
    main()
