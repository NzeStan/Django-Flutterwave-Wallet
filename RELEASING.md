# Releasing

Publishing a GitHub Release uploads the package to PyPI. The workflow in
`.github/workflows/publish.yml` does the work:

1. runs the full test suite (SQLite matrix and PostgreSQL with the race-condition tests);
2. checks that the release tag, `pyproject.toml` and `flutterwave_wallet/__init__.py` have the same version;
3. builds the wheel and sdist and checks them with `twine check --strict`;
4. uploads them to PyPI with Trusted Publishing, so no token is stored anywhere;
5. attaches the built files to the GitHub Release.

## One-time setup

**PyPI: add a trusted publisher**

On https://pypi.org/manage/account/publishing/, add a *pending publisher* (GitHub tab):

| Field             | Value                       |
|-------------------|-----------------------------|
| PyPI Project Name | `django-flutterwave-wallet` |
| Owner             | `NzeStan`                   |
| Repository name   | `Django-Flutterwave-Wallet` |
| Workflow name     | `publish.yml`               |
| Environment name  | `pypi`                      |

**GitHub: create the environment**

Go to Repository → Settings → Environments → New environment and name it `pypi`.
You can optionally add yourself as a required reviewer, so every upload waits for your approval.

## Making a release

1. Bump the version in **both** `pyproject.toml` and `flutterwave_wallet/__init__.py`.
2. Add a section for the new version to `CHANGELOG.md`.
3. Merge to `main`.
4. On GitHub, go to Releases → *Draft a new release*:
   - Tag: `vX.Y.Z` (for example `v1.0.0`). Create it on publish, targeting `main`.
   - Title: `vX.Y.Z`. Paste the changelog section as the notes.
   - Click **Publish release**.
5. Watch the *Publish to PyPI* run in the Actions tab.
6. Check the result at https://pypi.org/project/django-flutterwave-wallet/ and with
   `pip install django-flutterwave-wallet==X.Y.Z`.

PyPI never accepts the same version twice. If a release fails *after* uploading, bump the patch
version and release again. If it fails *before* uploading, fix the problem, delete the release and
its tag, and publish again.
