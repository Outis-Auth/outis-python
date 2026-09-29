# Releasing

Releases are cut by pushing a tag. The `Release` workflow tests the code, builds the sdist and wheel, publishes them to PyPI through trusted publishing, and creates a GitHub release with generated notes.

## Cut a release

1. On a branch, bump `version` in `pyproject.toml` and refresh the lockfile so it records the new version:

   ```sh
   uv lock
   ```

2. Commit both files, open a pull request, wait for CI, and merge it.

3. On an up-to-date `main`, tag the merge commit and push the tag:

   ```sh
   git checkout main && git pull
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The tag has to be `v` followed by the exact version in `pyproject.toml`. The workflow checks this first and stops with a clear error if they differ, before anything is built or published.

## What the workflow does

`.github/workflows/release.yml` runs on any tag matching `v*`, in three jobs:

1. **build** checks the tag against `pyproject.toml`, installs the dev group, runs the test suite, runs `uv build`, and runs `twine check` on the results.
2. **publish** runs in the `pypi` GitHub environment and uploads the artifacts to PyPI with `pypa/gh-action-pypi-publish`. There's no API token. PyPI trusts the OIDC identity of this workflow, in this repository, in this environment.
3. **github-release** creates the GitHub release for the tag, with generated notes, and attaches the sdist and wheel.

If a job fails, fix the cause on a branch, merge it, delete the tag locally and on GitHub (`git push --delete origin vX.Y.Z`), and tag again. PyPI never accepts the same version twice, so a version that already published needs a new version number.

## One-time setup

Both of these have to exist before the first tag is pushed.

1. **Create the GitHub environment.** In the repository, go to Settings, then Environments, and create one named `pypi`. Optionally add a required reviewer so a publish waits for a click.

2. **Register the trusted publisher on PyPI.** Sign in to [pypi.org](https://pypi.org), open Your account, then Publishing, and add a pending publisher under "Add a new pending publisher" with:

   - PyPI project name: `outis`
   - Owner: `Outis-Auth`
   - Repository name: `outis-python`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

   The project doesn't exist on PyPI yet, which is what "pending" means. The first successful publish creates it and turns the pending publisher into a regular one.

## Installing

```sh
pip install outis
pip install "outis[durable]"   # sealed calls for workers (defer_to)
uv add outis
```

The command line lives in the package, so after installing it run `python -m outis keygen` or `python -m outis init worker`. With uv: `uv run python -m outis keygen`.
