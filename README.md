# Portfolio job board

A free, self-updating job board for the portfolio, like Getro without the subscription.

- `portfolio.yaml`: the companies and where each one posts jobs
- `scripts/fetch_jobs.py`: pulls every company's open roles into `docs/jobs.json`
- `.github/workflows/update-jobs.yml`: runs the fetcher every night and commits the result
- `docs/index.html`: the board (search, team / location / company filters, talent network link, portfolio roster)

Cost: $0. GitHub Actions and GitHub Pages (or Cloudflare Pages) are free at this size.

## Set it up (about 20 minutes)

1. **Create a GitHub repo** and push this folder to it.
   GitHub Pages on a free account needs a public repo. If it must be private, use Cloudflare Pages instead (step 3b).
2. **Edit the settings** at the top of the script in `docs/index.html`:
   `fundName`, `homeUrl` (your main site), `talentFormUrl`.
   For the talent network, make a free [Tally](https://tally.so) form (name, email, LinkedIn, roles wanted, location) and paste its link.
3. **Publish the site.** Pick one:
   - a. GitHub Pages: repo Settings → Pages → Deploy from branch → `main` / `/docs`. For a custom domain, add `jobs.yourfund.com` there and a CNAME record at your DNS provider pointing to `<your-user>.github.io`.
   - b. Cloudflare Pages: connect the repo, no build command, output directory `docs`. Add the custom domain in Pages settings.
4. **Turn on the nightly update.** Actions tab → enable workflows → "Update jobs" → Run workflow. It runs again every night at 03:17 UTC. Each run's summary lists any company that failed.
5. **Link it from your site**: a "Portfolio jobs" link in the nav, or embed it:
   `<iframe src="https://jobs.yourfund.com" style="width:100%;height:1400px;border:0"></iframe>`

## Day to day

**New investment:** add it to `portfolio.yaml`, then run
```
pip install -r scripts/requirements.txt
python scripts/detect_careers.py --name "NewCo"      # or --url https://newco.ai
```
Paste the suggested `careers:` line, check it with `python scripts/fetch_jobs.py --only "NewCo"`, and commit. The push triggers a fresh run.

**A company shows "Failed" in the run summary:** its careers page moved or changed layout. Run `detect_careers.py --name` on it and update its entry. Its previous jobs stay on the board for up to 7 days while you fix it.

**A company has no public jobs page** (`type: none`): it still appears in the roster as "No open roles listed". If you want a role on the board anyway, ask the founder to put it on a careers page or an ATS.

## Supported sources

| type | where jobs live | needs |
|---|---|---|
| `ashby` | jobs.ashbyhq.com/slug | `slug` |
| `greenhouse` | job-boards.greenhouse.io/slug | `slug` |
| `lever` | jobs.lever.co/slug | `slug` |
| `workable` | apply.workable.com/slug | `slug` |
| `comeet` | comeet.com/jobs/slug/UID | `slug`, `uid` |
| `hibob` | slug.careers.hibob.com | `slug` |
| `html` | the company's own careers page | `url` + `link` (regex for job links), or `url` + `item` (CSS selector per job card); optional `title`, `location`, `department` selectors |

Teams are mapped to about ten standard names (Engineering, Product, Data & AI, Sales & BD…) by keyword in `FUNCTIONS` inside `fetch_jobs.py`; adjust there if something lands in the wrong place.

## Notes from setup (Oct 28, 2026)

- 15 of 32 companies list jobs publicly; 93 roles on day one.
- Atom, HeroCon and HAAT Delivery couldn't be reached during setup. Run `python scripts/detect_careers.py` once from your machine or let the Action run, then fill those in.
- Playvault's site now redirects to playmagnet.io. Confirm the name and URL.
- Robusta's site announces it joined Freshworks. Decide whether it stays on the board.
- Opmed and Robusta don't show a location on their careers pages, so their roles say "See posting".
