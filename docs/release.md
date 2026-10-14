# Making a release

A release is a version bump, a tag, and a Homebrew formula update. Users then run `brew upgrade lightkeeper`
or `pipx upgrade lightkeeper`.

1. Bump `__version__` in `lightkeeper/__init__.py` (pipx and pip only reinstall when the version changes).
   Run `make test`. Commit as `release X.Y.Z`.
2. Tag and push: `git tag vX.Y.Z && git push origin main vX.Y.Z`. Create the GitHub release from the tag
   (`gh release create vX.Y.Z --generate-notes`).
3. Update the Homebrew tap (repository `ridjex/homebrew-tap`, file `Formula/lightkeeper.rb`):

   ```sh
   url=https://github.com/ridjex/lightkeeper/archive/refs/tags/vX.Y.Z.tar.gz
   sha=$(curl -sL "$url" | shasum -a 256 | cut -d' ' -f1)
   # set `url` and `sha256` in the formula, then refresh the Python resources (rich, textual and their dependencies)
   brew update-python-resources --extra-packages rich,textual ridjex/tap/lightkeeper
   brew install --build-from-source ridjex/tap/lightkeeper && brew test ridjex/tap/lightkeeper
   git commit -am "lightkeeper X.Y.Z" && git push
   ```

   The formula uses `Language::Python::Virtualenv` with every dependency pinned as a resource, so installation never
   runs an unconstrained `pip install`; the tool's core has no dependencies and the resources exist only for the screen.

Install channels for users:

| Channel | Install | Update |
|---|---|---|
| Homebrew | `brew install ridjex/tap/lightkeeper` (optionally `brew services start lightkeeper`) | `brew services stop lightkeeper`, `brew upgrade lightkeeper`, start again |
| pipx | `pipx install "lightkeeper[ui] @ git+https://github.com/ridjex/lightkeeper"` | `pipx upgrade lightkeeper` (fetches main; releases bump the version) |
| Clone | `python3 -m pip install -e ".[ui]"` | `git pull` |

The configuration directory, journal and sessions are never touched by an install or update. The next start
of the upgraded program may repair a torn journal tail and normalise permissions; it never rewrites records.

Compatibility window: a newer lightkeeper reads older settings and journals. Going back to an older version is
not supported once the newer one has saved settings or written journal records (it may add keys and record
versions the older reader rejects). Stop the running instance before upgrading.

