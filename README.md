# MSST Bible - Google Doc Mirror

A fast, lightweight static site mirror of the [MSST Bible Google Doc](https://docs.google.com/document/d/17fjNvJzj8ZGSer7c7OFe_CNfUKbAxEh_OBv94ZdRG5c/edit).

## Features

- **Fast loading** — 66 pages instead of one 13MB document
- **Dark mode** — default theme with light/dark toggle
- **Collapsible TOC** — sidebar groups expand/collapse
- **Search** — client-side full-text search
- **Auto-updates** — GitHub Actions regenerates on a schedule
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

The site lives at `https://msst-bible.pages.dev` (a Cloudflare Pages project named `msst-bible`). GitHub Actions builds it on a schedule and pushes the result to Cloudflare via Direct Upload.

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
   - Update every hour via GitHub Actions (the `schedule` in `.github/workflows/deploy.yml`)
   - Redeploy on demand via the **Run workflow** button
   - Be available at `https://msst-bible.pages.dev`

## How It Works

1. GitHub Actions runs on a schedule
2. Checks out this repo
3. Runs `gdoc_site.py` to fetch the Google Doc
4. Generates static HTML pages
5. Deploys to Cloudflare Pages (`msst-bible.pages.dev`)

## Local Development

```bash
# Build the site
python gdoc_site.py --doc 17fjNvJzj8ZGSer7c7OFe_CNfUKbAxEh_OBv94ZdRG5c --out _site

# Preview locally
python serve.py --dir _site
```

## Configuration

### Change update frequency

Edit `.github/workflows/deploy.yml`:

```yaml
on:
  schedule:
    # Every hour (default)
    - cron: '0 * * * *'
    
    # Every 6 hours
    # - cron: '0 */6 * * *'
    
    # Daily at 3 AM
    # - cron: '0 3 * * *'
```

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
