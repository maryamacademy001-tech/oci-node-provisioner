# Credential Rotation & History Cleanup Runbook

## Status

`.env` (containing an OCI RSA private key + 8 identifiers) is committed at
`b1aa686` and `HEAD`, and has been pushed to `origin`. The key must be treated
as COMPROMISED. This runbook was NOT executed — it requires the repository
owner's approval for the destructive history rewrite and force-push.

## Prerequisites

Install the history-rewriting tool (one-time):

```shell
pip install git-filter-repo
```

## Required sequence — DO NOT reorder

Step 1 and 2 are manual, owner-only actions. They cannot be automated here.

### 1. Rotate / revoke the compromised OCI key (OWNER, OCI Console)

Do this FIRST. Cleaning history is useless while the old key still works.

- OCI Console → Identity & Security → Users → select the user
  (`ocid1.user.oc1..***…7yra`)
- API keys → delete the exposed key (fingerprint `72:47:***…2:70`)
- Create a new API key pair; download the NEW private key
- Copy the NEW fingerprint

### 2. Update GitHub Actions Secrets (OWNER, repo Settings)

Settings → Secrets and variables → Actions → update:

| Secret | Action |
|---|---|
| `OCI_PRIVATE_KEY` | replace with NEW key |
| `OCI_FINGERPRINT` | replace with NEW fingerprint |

The other identifiers (`OCI_USER_ID`, `OCI_TENANCY_ID`, `OCI_REGION`,
`OCI_SUBNET_ID`, `OCI_PUBLIC_SSH_KEY`) were exposed too. They are not
secrets in the same sense as the private key, but re-validate that the
subnet and SSH key are still the ones you intend.

### 3. Rewrite history to remove `.env` (OWNER, after steps 1-2)

```shell
# Work on a fresh clone to protect your current checkout
cd /tmp
git clone https://github.com/maryamacademy001-tech/oci-node-provisioner.git oci-clean
cd oci-clean

# Back up first
git remote add backup-server /path/to/backup.git 2>/dev/null
git push backup-server --all 2>/dev/null || true

# Remove .env from every commit in history
git filter-repo --invert-paths --path .env --force

# Re-attach origin (filter-repo removes it deliberately)
git remote add origin https://github.com/maryamacademy001-tech/oci-node-provisioner.git
```

### 4. Verify history no longer contains `.env`

```shell
git rev-list --all -- .env | wc -l          # must print 0
git log --all -- .env | wc -l               # must print 0
```

### 5. Verify no secret remains in reachable objects

```shell
git grep -I -n -E "BEGIN PRIVATE KEY" $(git rev-list --all) -- 2>/dev/null | wc -l
# must print 0

git fsck --lost-found 2>/dev/null | Select-String "dangling blob"
# Inspect any dangling blob hashes; none should be a PEM key
```

The exposed key value is already public to anyone who cloned before cleanup,
so step 1 (rotation) is the real control. History cleanup reduces future
exposure and keeps the repo auditable.

### 6. Force-push the cleaned history (OWNER, explicit approval)

Only after steps 4 and 5 pass:

```shell
git push origin --force --all
git push origin --force --tags   # if any tags exist
```

This rewrites public commit hashes. Anyone with an old clone must re-clone.

### 7. Verify GitHub repository state

- Confirm the `.env` file no longer appears in any commit on GitHub
- Confirm GitHub Actions runs still succeed and pick up the NEW secrets
- Confirm the Actions log contains no private key material

## Notes

- `__pycache__/bot.cpython-312.pyc` was also tracked; it has been untracked
  from the index and gitignored. `.env` was untracked from the index in the
  same way. Both remain on disk locally — the working copy is untouched.
- No key rotation, deletion, or force-push was performed by the remediation.
