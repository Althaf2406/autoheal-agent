# Setup: GitHub App, Demo Repo & Auto-PR (Anggota B)

This guide covers the manual steps for Sub-Task 2 and how to test Sub-Task 4 and 5.

## 1. Push the demo repo
The `demo-etl-repo/` folder is a separate repository (it is in `.gitignore` here).

1. Create a new empty repo on GitHub, for example `autoheal-demo-etl`.
2. Copy the `demo-etl-repo/` folder somewhere outside this project, then:
   ```bash
   cd autoheal-demo-etl
   git init -b main
   git add .
   git commit -m "Initial demo ETL with intentional bug"
   git remote add origin https://github.com/<you>/autoheal-demo-etl.git
   git push -u origin main
   ```
   The first run will fail with `KeyError: 'total_price'`. That is expected.

## 2. Create the GitHub App
1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Fill in:
   - **Name:** something unique, for example `autoheal-agent-<yourname>`
   - **Homepage URL:** the URL of this repo
   - **Webhook:** uncheck **Active** (errors come from GitHub Actions, not App events)
3. **Repository permissions:**
   - Contents: **Read and write** (create branch, commit the fix)
   - Pull requests: **Read and write** (open the Draft PR)
   - Issues: **Read and write** (label `autoheal-bot`, "Terjadi lagi" comments on the PR, escalation issues)
   - Metadata: **Read-only** (set automatically)
4. **Where can this app be installed:** Only on this account.
5. Click **Create GitHub App** and note the **App ID**.
6. Under **Private keys**, click **Generate a private key**. A `.pem` file downloads.
7. Put it in this project at `secrets/autoheal-app.private-key.pem` (the folder is ignored by git).
8. In the left menu, click **Install App** and install it on **only** the `autoheal-demo-etl` repo.

## 3. Configure `.env`
Copy `.env.example` to `.env` and fill in:
```
NEBIUS_API_KEY=...
TAVILY_API_KEY=...
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=secrets/autoheal-app.private-key.pem
DEMO_REPO=<you>/autoheal-demo-etl
```
If the App is not ready yet, leave the App values empty and set `GITHUB_TOKEN` to a fine-grained
Personal Access Token with Contents, Pull requests and Issues read/write on the demo repo.

Test the connection:
```bash
python -m backend.github_integration <you>/autoheal-demo-etl
```
You should see `Auth mode: app` and `Connected to ...`.

## 4. Expose the backend to GitHub Actions
GitHub's runners cannot reach `127.0.0.1`, so use a tunnel while developing:
```bash
uvicorn backend.main:app --reload
ngrok http 8000
```
In the demo repo, add the secret **`AUTOHEAL_WEBHOOK_URL`** = `https://<your-ngrok-id>.ngrok-free.app/webhook`
(Settings → Secrets and variables → Actions → New repository secret).

Optional but recommended, because the ngrok URL is public: set `AUTOHEAL_WEBHOOK_SECRET` in `.env` to a random
string and add the same value as the **`AUTOHEAL_WEBHOOK_SECRET`** secret in the demo repo. The backend then
rejects any webhook without the matching `X-AutoHeal-Secret` header.

## 5. Run the full flow
1. Start the dashboard: `streamlit run frontend/app.py`
2. In the demo repo, open **Actions → ETL Pipeline → Run workflow** (or push a commit).
3. The run fails and sends the error to `/webhook`.
4. In the dashboard, press **Refresh**. You will see the status move through
   Researcher → Engineer → Pull Request, then the fixed code and the PR link.
5. The workflow also runs on the PR itself, so a green check shows the fix works.
   If the pipeline fails again while the PR is still open, the agent reuses that PR instead of opening a new one.
6. Merge the PR in the demo repo. The next workflow run should pass.

To reset the demo, revert the merge commit so the bug comes back.

## Test without GitHub Actions
You can send the webhook yourself:
```bash
curl -X POST http://127.0.0.1:8000/webhook -H "Content-Type: application/json" -H "X-AutoHeal-Secret: <secret, if set>" -d '{
  "error_log": "KeyError: '\''total_price'\''",
  "repo": "<you>/autoheal-demo-etl",
  "file_path": "etl/pipeline.py",
  "branch": "main"
}'
```

## Webhook payload
| Field | Required | Description |
|---|---|---|
| `error_log` | yes | Error output from the failed step |
| `repo` | for PR | `owner/name` of the repo to fix |
| `file_path` | for PR | Path of the file to fix, e.g. `etl/pipeline.py` |
| `branch` | no | Branch that failed. The PR targets it. Defaults to the repo's default branch |
| `run_url` | no | Link to the failed Actions run, added to the PR description |
