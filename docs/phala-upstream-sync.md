# Phala Upstream Sync

This fork keeps two long-lived branches:

- `main` mirrors official Hermes: `upstream/main`.
- `phala` is the deployable branch: `upstream/main` plus Phala/Clawdi patches.

Do not put Phala-specific commits on `main`. Keep the patch stack on `phala`
small and linear so upstream syncs stay easy to audit.

## Remote Setup

Expected remotes:

```bash
git remote -v
# origin   https://github.com/Clawdi-AI/hermes-agent.git
# upstream https://github.com/NousResearch/hermes-agent.git
```

If `upstream` is missing:

```bash
git remote add upstream https://github.com/NousResearch/hermes-agent.git
```

The GitHub default branch for this fork should be `phala`, while `main` remains
the official mirror.

## Normal Sync

Start with a clean worktree:

```bash
git status --short --branch
```

Fetch both remotes:

```bash
git fetch upstream --prune
git fetch origin --prune
```

Record the current remote tips before pushing later:

```bash
OLD_ORIGIN_MAIN="$(git rev-parse origin/main)"
OLD_ORIGIN_PHALA="$(git rev-parse origin/phala)"
```

Update local `main` to the official upstream tip:

```bash
git branch -f main upstream/main
```

Rebase `phala` on top of the new official tip:

```bash
git switch phala
git rebase upstream/main
```

If there are conflicts, resolve them in the Phala patch files, then continue:

```bash
git status --short
git add <resolved-files>
git rebase --continue
```

Abort only if the rebase is clearly wrong:

```bash
git rebase --abort
```

## Verification

Run the focused tests that cover the Phala dashboard patch:

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_web_server.py \
  tests/hermes_cli/test_env_loader.py \
  tests/gateway/test_feishu_bot_admission.py
```

For a stronger local runtime smoke test, start the dashboard with an isolated
`HERMES_HOME` containing a stable token and env values, then curl `/api/env`.

```bash
tmp="$(mktemp -d)"
cat > "$tmp/.env" <<'EOF'
HERMES_SESSION_TOKEN=phala-runtime-token
TELEGRAM_ALLOWED_USERS=123,456
FEISHU_ALLOWED_USERS=ou_a,ou_b
OPENROUTER_API_KEY=sk-test-secret
EOF

HERMES_HOME="$tmp" python - <<'PY'
from hermes_cli.web_server import start_server
start_server(host="127.0.0.1", port=19119, open_browser=False)
PY
```

In another shell:

```bash
curl -sS -o /tmp/env.json \
  -H 'X-Hermes-Session-Token: phala-runtime-token' \
  http://127.0.0.1:19119/api/env

python - <<'PY'
import json
p = json.load(open("/tmp/env.json"))
assert p["TELEGRAM_ALLOWED_USERS"]["value"] == "123,456"
assert p["FEISHU_ALLOWED_USERS"]["value"] == "ou_a,ou_b"
assert p["OPENROUTER_API_KEY"]["value"] is None
assert p["OPENROUTER_API_KEY"]["is_password"] is True
print("dashboard env smoke ok")
PY
```

For Docker verification:

```bash
docker build -t clawdi/hermes-agent:phala-smoke .

smoke_dir="$(mktemp -d)"
cat > "$smoke_dir/.env" <<'EOF'
HERMES_SESSION_TOKEN=phala-docker-token
TELEGRAM_ALLOWED_USERS=123,456
FEISHU_ALLOWED_USERS=ou_a,ou_b
OPENROUTER_API_KEY=sk-test-secret
EOF

docker run -d --rm --name hermes-phala-smoke \
  -p 19120:9119 \
  -e HERMES_DASHBOARD=1 \
  -e HERMES_DASHBOARD_HOST=0.0.0.0 \
  -e HERMES_DASHBOARD_PORT=9119 \
  -v "$smoke_dir:/opt/data" \
  clawdi/hermes-agent:phala-smoke \
  sleep infinity

curl -sS -o /tmp/docker-env.json \
  -H 'X-Hermes-Session-Token: phala-docker-token' \
  http://127.0.0.1:19120/api/env

docker stop hermes-phala-smoke
```

## Push

Because `main` is a mirror and `phala` is rebased, both updates may require
force pushes. Always use `--force-with-lease` with the recorded old remote tips.

```bash
git push --force-with-lease=main:"$OLD_ORIGIN_MAIN" origin main:main
git push --force-with-lease=phala:"$OLD_ORIGIN_PHALA" origin phala:phala
git remote set-head origin -a
```

If the lease fails, someone pushed new remote commits. Fetch again and inspect
before retrying:

```bash
git fetch origin --prune
git log --oneline --decorate --graph --max-count=30 --all
```

## Final Checks

`origin/main` should match official upstream exactly:

```bash
git rev-list --left-right --count upstream/main...origin/main
# expected: 0 0
```

`origin/phala` should be official upstream plus only the Phala patch stack:

```bash
git rev-list --left-right --count upstream/main...origin/phala
# expected: 0 <small number>

git log --oneline upstream/main..origin/phala
```

The fork default branch should remain `phala`:

```bash
gh repo view Clawdi-AI/hermes-agent \
  --json defaultBranchRef \
  -q '.defaultBranchRef.name'
```

If needed, set it explicitly:

```bash
gh repo edit Clawdi-AI/hermes-agent --default-branch phala
```
