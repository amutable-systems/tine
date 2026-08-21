"""Make one automatic change, test it, and open the pull request for it.

Only one run of each kind is ever in flight: while a pull request or a failure issue carrying the
run's label is open, this does nothing, and a failure opens that issue so the run stays paused until
somebody closes it. The pull request gets no CI of its own - the token a workflow pushes with
deliberately triggers no further workflow - which is what the test here stands in for.

Everything a workflow decides is an option, so that `--help` is the whole contract. The rest comes
from what Actions sets around the run: GITHUB_SERVER_URL and GITHUB_RUN_ID to link back to it, and
GITHUB_REF_NAME as the branch the pull request goes against.

Needs only git, gh and a host python: it runs before the project's build system has fetched
anything, and the gate below is meant to be reached without paying for that.
"""

import argparse
import os
import subprocess
from pathlib import Path

# Actions points this at the checkout. Everything below runs there rather than wherever the caller
# happened to leave the working directory.
ROOT = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()

# github-actions[bot], the identity the pushing token belongs to.
AUTHOR = ("github-actions[bot]", "noreply@amutable.com")


def _output(command: list[str]) -> str:
    """Stripped stdout of a command, with its stderr left going to the log."""
    return subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.PIPE, text=True).stdout.strip()


def git(*args: str) -> str:
    return _output(["git", *args])


def gh(*args: str) -> str:
    return _output(["gh", *args])


def shell(command: str) -> None:
    """Run a command line that came from configuration, as written."""
    subprocess.run(command, shell=True, cwd=ROOT, check=True)


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


def commit_change(command: str) -> str | None:
    """Run the command that commits the change; the commit it started from, or None if it made none."""
    git("config", "user.name", AUTHOR[0])
    git("config", "user.email", AUTHOR[1])
    before = git("rev-parse", "HEAD")
    shell(command)
    return before if git("rev-parse", "HEAD") != before else None


def open_pull_request(name: str, label: str, before: str, repository: str) -> str:
    """Push what the command committed to the branch this run owns, and open its pull request."""
    # The gate leaves at most one run of each kind in flight, so the branch is this run's own to
    # overwrite: the previous one's may still be there, merged and never deleted.
    git("push", "--force", "origin", f"HEAD:refs/heads/{name}")
    # --reverse: git log is newest-first, and these read in the order they were made.
    span = f"{before}..HEAD"
    log = git("log", "--reverse", "--format=%s%n%n%b", span)
    title = "; ".join(git("log", "--reverse", "--format=%s", span).splitlines())
    # The blank line matters: a commit body ending in a list would swallow the line after it.
    body = f"{log}\n\nTested by {run_url(repository)}.\n"
    base = os.environ["GITHUB_REF_NAME"]
    url = gh(
        "pr", "create", "--head", name, "--base", base, "--label", label, "--title", title, "--body", body
    )
    return url.rsplit("/", 1)[-1]


def report_failure(name: str, label: str, repository: str) -> None:
    """Open the issue that keeps runs of this kind paused until somebody closes it."""
    # Only ever one at a time, which the gate takes care of by looking for this same label.
    paused = f"Nothing was pushed; {name} stays paused until this issue is closed."
    body = f"{paused}\n\n{run_url(repository)}\n"
    gh("issue", "create", "--label", label, "--title", f"{name} is failing", "--body", body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="what this run is, e.g. bump")
    parser.add_argument("--label-description", required=True, help="what the label bot-<name> means")
    parser.add_argument("--command", required=True, metavar="COMMAND", help="how to make the change")
    parser.add_argument("--test", required=True, metavar="COMMAND", help="how to test the change")
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

    try:
        before = commit_change(args.command)
        if before is None:
            print(f"::notice::{args.name} found nothing to change")
            return
        shell(args.test)
        pull_request = open_pull_request(args.name, label, before, args.repo)
    except subprocess.CalledProcessError:
        # A command failing is what the issue is for. Anything else - a missing variable, a gh that
        # cannot talk to the API - is this driver or its workflow being wrong, and belongs in a
        # traceback rather than in an issue somebody has to close before the bot runs again.
        report_failure(args.name, label, args.repo)
        raise

    print(f"::notice::opened #{pull_request}")
    # What a caller with something left to do about the pull request reads.
    step_output = os.environ.get("GITHUB_OUTPUT")
    if step_output:
        with Path(step_output).open("a") as handle:
            handle.write(f"pull-request={pull_request}\n")


if __name__ == "__main__":
    main()
