# MSST Bible - Google Doc Mirror

A fast, lightweight static site mirror of the [MSST Bible Google Doc](https://docs.google.com/document/d/17fjNvJzj8ZGSer7c7OFe_CNfUKbAxEh_OBv94ZdRG5c/edit).

## Features

- **Fast loading** — one long, scrollable page per section instead of one 13MB document
- **Dark mode** — default theme with light/dark toggle
- **Collapsible TOC** — sidebar groups expand/collapse
- **Search** — client-side full-text search
- **Auto-updates** — a Cloudflare Worker cron regenerates on a schedule
- **Free hosting** — Cloudflare Pages with HTTPS

## Run your own mirror

Fork the repo and the whole pipeline works. Four things to set:

1. **Fork** the repo on GitHub (or use Code → Download ZIP).
2. **Make your Google Doc public** — File → Share → "Anyone with the link".
3. **Point it at your doc** — edit the `Generate site` step of
   `.github/workflows/deploy.yml`: `--doc YOUR_DOC_ID`, and pick your own
   project name (see below).
4. **Add your Cloudflare secrets and deploy** (next section).

The workflow **auto-creates the Cloudflare Pages project** on the first
deploy, so there's nothing to set up in the dashboard. On your fork, enable
Actions (the Actions tab prompts once), add the two secrets, and hit
**Run workflow**.

## Quick Start

### Local development
```bash
# Windows
local_build.bat

# Or manually
python gdoc_site.py --doc 17fjNvJzj8ZGSer7c7OFe_CNfUKbAxEh_OBv94ZdRG5c --out _site
python serve.py --dir _site
# Open http://localhost:8000
```

### Deploy to Cloudflare Pages

The site lives at `https://msst-bible.pages.dev` (a Cloudflare Pages project named `msst-bible`). A Cloudflare Worker cron fires the GitHub Actions workflow, which builds the site and pushes it to Cloudflare via Direct Upload.

1. **Pick a project name**
   - The default is `msst-bible` (set as `CLOUDFLARE_PAGES_PROJECT` in `.github/workflows/deploy.yml`).
   - The workflow creates the project automatically on the first deploy — no manual creation needed.
   - Your site will live at `https://<project-name>.pages.dev`

2. **Create an API token**
   - Cloudflare Dashboard → **My Profile** → **API Tokens** → **Create Token**
   - Create a custom token with **Account → Cloudflare Pages → Edit**, scoped to your account
   - Write down the token and your **Account ID** (Dashboard → right sidebar)

3. **Add secrets to the GitHub repo**
   - Repo → **Settings** → **Secrets and variables** → **Actions**
   - `CLOUDFLARE_API_TOKEN` = the token from step 2
   - `CLOUDFLARE_ACCOUNT_ID` = your account ID

4. **Done!** The site will:
   - Update every 15 minutes via the Cloudflare Worker cron (see the next section)
   - Redeploy on demand via the **Run workflow** button
   - Be available at `https://msst-bible.pages.dev`

## Cloudflare Worker timer (recommended)

GitHub Actions' `schedule` is best-effort: runs can be delayed by hours or
dropped under load. To refresh dependably every 15 minutes, a tiny scheduled
**Cloudflare Worker** calls the `workflow_dispatch` API on the same cadence.
The build + deploy still runs in GitHub Actions (only the *timer* moves to
Cloudflare's dependable scheduler).

The worker is `workers/msst-bible-tick/`. To set it up once:

1. **Create a GitHub token** — GitHub → Settings → Developer settings →
   Personal access tokens → Fine-grained tokens. Repository access:
   `bascurtiz/msst-bible`, **Permissions → Actions → Read and write**.
   Copy the token.
2. **Create the Worker** (Cloudflare Dashboard → Workers & Pages → Create →
   Worker, name `msst-bible-tick`), then either:
   - **Dashboard:** paste `cron-trigger.js` into the editor, add a Cron
     Trigger `*/15 * * * *`, a **secret** `GITHUB_TOKEN` = the token from step
     1, and a **variable** `DISPATCH_URL` = `https://api.github.com/repos/bascurtiz/msst-bible/actions/workflows/deploy.yml/dispatches`, then **Deploy**.
   - **CLI:** `cd workers/msst-bible-tick && wrangler login && wrangler deploy`
     then `wrangler secret put GITHUB_TOKEN`.
3. **Test it** — `curl https://msst-bible-tick.<your-subdomain>.workers.dev/trigger`
   should start a deploy run (you'll see it appear on the Actions tab). The
   `/trigger` route fires the workflow on demand; otherwise the cron handles it.

Once confirmed, you can drop the flaky `schedule:` block from
`.github/workflows/deploy.yml` so the Worker cron is the only timer (keeps the
`workflow_dispatch` trigger, which the Worker relies on). Optionally keep it as
a fallback — an occasional double-deploy is harmless (the `concurrency` group
runs them one at a time).

## How It Works

1. A Cloudflare Worker cron fires every 15 minutes
2. Calls GitHub's `workflow_dispatch` to start the deploy workflow
3. The workflow checks out this repo and runs `gdoc_site.py`
4. The script fetches the Google Doc and generates static HTML
5. The result deploys to Cloudflare Pages (`msst-bible.pages.dev`)

## Local Development

No third-party dependencies — just Python 3.10+ (`requirements.txt` exists
only to say so). Run the scripts directly, or install as an editable package
for a `gdoc-site` command:

```bash
# Option A: run the scripts directly
python gdoc_site.py --doc 17fjNvJzj8ZGSer7c7OFe_CNfUKbAxEh_OBv94ZdRG5c --out _site
python serve.py --dir _site

# Option B: install and use the command
pip install -e .
gdoc-site --doc 17fjNvJzj8ZGSer7c7OFe_CNfUKbAxEh_OBv94ZdRG5c --out _site
python serve.py --dir _site
```

## Configuration

### Change update frequency (Cloudflare Worker cron)

The refresh cadence lives in `workers/msst-bible-tick/wrangler.toml`:

```toml
[[triggers]]
crons = ["*/15 * * * *"]   # every 15 minutes (Cloudflare, dependable)
```

Change the cron (e.g. `["0 * * * *"]` for hourly, `["0 */6 * * *"]` for
six-hourly) and redeploy the worker (`wrangler deploy` or Dashboard → save).

### Use a different Google Doc

Edit the parameters in the `Generate site` step of `.github/workflows/deploy.yml`:

```yaml
- name: Generate site
  run: |
    python gdoc_site.py \
      --doc YOUR_DOC_ID \
      --base-url https://your-project.pages.dev/ \
      --out _site
```

## File Structure

```
msst-bible/
├── .github/workflows/
│   └── deploy.yml          # GitHub Actions (auto-deploy)
├── .gitignore              # Excludes credentials & build artifacts
├── gdoc_site.py            # Main generator script
├── serve.py                # Local preview server
├── local_build.bat         # Windows quick build script
├── requirements.txt        # No dependencies — stdlib only
├── pyproject.toml          # Package metadata + optional `gdoc-site` command
├── workers/
│   └── msst-bible-tick/    # Scheduled Cloudflare Worker timer (deploys on cron)
│       ├── cron-trigger.js # Worker: pokes workflow_dispatch
│       └── wrangler.toml   # Cron trigger + bindings
└── README.md               # This file
```

## Troubleshooting

**Site not updating?**
- Check GitHub Actions tab for errors
- Ensure Google Doc is public ("Anyone with the link")

**Auth expired?**
- Re-run: `python gdoc_site.py --auth --client-json client_secret_*.json`

**Local build fails?**
- Ensure Python 3.10+ is installed
- Check that `auth.json` exists (run OAuth setup first if needed)

## License

MIT
