# MSST Bible - Google Doc Mirror

A fast, lightweight static site mirror of the [MSST Bible Google Doc](https://docs.google.com/document/d/17fjNvJzj8ZGSer7c7OFe_CNfUKbAxEh_OBv94ZdRG5c/edit).

## Features

- **Fast loading** — 66 pages instead of one 13MB document
- **Dark mode** — default theme with light/dark toggle
- **Collapsible TOC** — sidebar groups expand/collapse
- **Search** — client-side full-text search
- **Auto-updates** — GitHub Actions regenerates every hour
- **Free hosting** — GitHub Pages with HTTPS

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

### Deploy to GitHub Pages

1. **Create a GitHub repo**
   ```bash
   cd D:\github\msst-bible
   git init
   git add -A
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/msst-bible.git
   git push -u origin master
   ```

2. **Enable GitHub Pages**
   - Go to your repo on GitHub
   - Settings → Pages
   - Source: **GitHub Actions**
   - Save

3. **Done!** The site will:
   - Deploy automatically on first push
   - Update every hour via GitHub Actions
   - Be available at `https://YOUR_USERNAME.github.io/msst-bible/`

## How It Works

1. GitHub Actions runs every hour
2. Checks out this repo
3. Runs `gdoc_site.py` to fetch the Google Doc
4. Generates static HTML pages
5. Deploys to GitHub Pages

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

Edit the `--doc` parameter in `.github/workflows/deploy.yml`:

```yaml
- name: Generate site
  run: |
    python gdoc_site.py \
      --doc YOUR_DOC_ID \
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
