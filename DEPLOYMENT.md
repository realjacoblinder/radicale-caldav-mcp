# Local source and runtime deployment

Canonical source: `/home/yaakov/repos/radicale-caldav-mcp`

Runtime: `/home/yaakov/mcp/radicale-mcp-v4/.venv`

Hermes command (keep unchanged):

```text
/home/yaakov/mcp/radicale-mcp-v4/.venv/bin/radicale-mcp
```

The runtime directory needs only `.venv/`. Install a wheel normally, not editably.
Source edits do not take effect until redeployment and MCP reload. Do not move an
existing virtual environment: console scripts contain absolute interpreter paths.

Hermes owns configuration and credentials in `~/.hermes/config.yaml` and
`~/.hermes/.env`. Do not copy secrets into this repository. The MCP does not
implicitly load a project `.env`.

## Build and test an update

Use `uv` on PATH, or the currently available managed binary:

```bash
UV=/home/yaakov/.hermes/tools/uv-0.13.5-linux-x64/uv
cd /home/yaakov/repos/radicale-caldav-mcp
ARTIFACTS=$(mktemp -d)
"$UV" export --locked --no-dev --no-emit-project -o "$ARTIFACTS/runtime-requirements.txt"
"$UV" export --locked --no-emit-project -o "$ARTIFACTS/test-requirements.txt"
"$UV" build --wheel --out-dir "$ARTIFACTS/wheels"
"$UV" venv --python /usr/bin/python3.14 "$ARTIFACTS/test-venv"
"$UV" pip install --python "$ARTIFACTS/test-venv/bin/python" --require-hashes -r "$ARTIFACTS/test-requirements.txt"
"$UV" pip install --python "$ARTIFACTS/test-venv/bin/python" --no-deps "$ARTIFACTS"/wheels/*.whl
"$ARTIFACTS/test-venv/bin/python" -I -m pytest tests -q
```

`--locked` refuses an out-of-date lockfile instead of silently updating it. Test
integration fixtures create isolated local Radicale storage; they do not use your
real CalDAV account. The installed-entrypoint test uses the test interpreter's
console script, not a virtual environment inside the source checkout.

The managed uv binary path can change after a Hermes update; update `UV` if needed.

## Deploy

Back up the current runtime first. During a maintenance window, disconnect the
Radicale MCP child through its owner/client; do not kill the whole Hermes gateway
or unrelated MCP processes. Create any replacement environment at the final path.
Do not run these commands while the existing MCP child is in use.

For an existing healthy runtime, using the artifacts from above:

```bash
RUNTIME=/home/yaakov/mcp/radicale-mcp-v4/.venv
"$UV" pip sync --python "$RUNTIME/bin/python" --require-hashes "$ARTIFACTS/runtime-requirements.txt"
"$UV" pip install --python "$RUNTIME/bin/python" --no-deps "$ARTIFACTS"/wheels/*.whl
"$UV" pip check --python "$RUNTIME/bin/python"
"$RUNTIME/bin/python" -I -B -c 'import radicale_mcp; print(radicale_mcp.__file__)'
"$RUNTIME/bin/radicale-mcp" --version
```

The sync step may remove the previous project package because the requirements
exclude it; the next step installs the new wheel. If replacing a mixed or damaged
environment, back up/move the old one and first run:

```bash
"$UV" venv --python /usr/bin/python3.14 "$RUNTIME"
```

Imports must resolve under runtime `site-packages`, never under a source checkout.
Run `/reload-mcp` in Hermes to reconnect and use the new process. Validate tool
discovery and, if desired, read-only calendar discovery. Avoid calendar mutations
as production deployment checks. Remove temporary artifacts only after acceptance.

## Rollback

Disconnect the MCP, restore the saved runtime directory at its original path, and
reload it in Hermes. A pre-separation editable installation also requires its old
source tree at its original path. Preserve the migration backup until the new
installation has been accepted; it includes the old source, Git history, and
virtual environment. No Hermes configuration edit is required for rollback.
