# Automatic Magnolia-area cash bids (no CHS emails, no TinyFish)

This is a starter collector that runs in GitHub's cloud browser at **approximately 5:15 AM Central** and publishes a table plus screenshots of public grain-price webpages. Your computer does NOT need to remain on. It is **not yet tested against the current live CHS widget**; its first run captures screenshots and all visible page text, which allows us to refine extraction if a publisher changes its table.

## One-time setup (~15 minutes)

1. Create or sign in to your **free GitHub account** at https://github.com.
2. Install **GitHub Desktop** at https://desktop.github.com/ on your Windows computer.
3. In GitHub Desktop, choose **File → New repository**, name it `magnolia-grain-bids`, and create it locally. **Select Public** when publishing (these are already-public elevator bids; do not put passwords or farm account information here).
4. Unzip this package. Copy its **contents** (including the hidden `.github` folder and `docs` folder) into the local repository folder. Do not put the entire unzipped parent folder into the repository.
5. In GitHub Desktop, enter a summary such as `Add public grain price collector`, choose **Commit to main**, then **Publish repository** and ensure public visibility.
6. In your repository's website, open **Actions → Collect public grain bids → Run workflow**. It takes a few minutes; look for a green check mark.
7. Open **Settings → Pages**, under **Build and deployment → Source** choose **GitHub Actions**, then Save. The included workflow will publish the result directly. Your public bid report will appear at `https://YOUR_GITHUB_USERNAME.github.io/magnolia-grain-bids/` after the job succeeds.
8. **Send ChatGPT that report URL.** We can inspect whether CHS and New Vision were successfully captured and refine the parser using the automatic diagnostics—without you supplying screenshots. Once it works, the existing 6:00 AM Morning Ag News scheduled task can be updated to read it.

## Important limitations

- GitHub's scheduled jobs sometimes start late. The collector typically runs at 5:15 AM Central; a delayed run is possible. The data records the actual capture time.
- GitHub-hosted browser traffic may be blocked by individual grain companies. If that happens, the report will show the failure—not a fabricated quote. A locally running Windows collector or buyer-authorized data feed may be needed.
- CHS/Bushel/DTN embedded widgets may use HTML tables or custom controls. This collector saves the **rendered PNG and visible text** for each source automatically; first-run parsing needs verification and may require adjustment.
- The app only reads **public webpages** without logging in or circumventing website access controls. Keep usage low and follow each site's applicable terms.
- No personal farm/account information should be saved in this **public** repository. Only public bid pages are requested.
- Source data aren't guaranteed live or executable. Always confirm bids/delivery windows and trucking with the buyer before contracting.
- The collector is a starting implementation; because this session cannot reach public sites from the Python container, end-to-end CHS extraction cannot be certified until its first scheduled/manual GitHub browser run.

## Changing the sites

Edit `SOURCES` near the top of `collect_bids.py`. Keep only the relevant public cash-bid pages. The first run captures `docs/captures/<source>.txt` and `<source>.png` for inspection. Parsed bids live in `docs/latest.csv` and `docs/latest.json` and are displayed at `docs/index.html`.

## Manual run on Windows (optional)

If you don't want GitHub cloud automation, install Python 3.12 on Windows, extract the archive, and run from PowerShell:

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py collect_bids.py --force
```

Open `docs/index.html` in your browser. This local option doesn't automatically make data accessible to your ChatGPT 6 AM report—use the GitHub Pages option for that.
