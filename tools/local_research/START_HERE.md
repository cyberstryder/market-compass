# Your local research kit

## Update your current computer

Open PowerShell and paste:

```powershell
cd C:\Compass\compass-research\tools\local_research
git pull --ff-only
.\setup.cmd
.\run.cmd
.\compass.cmd --days 3 --end 2026-09-22
```

Press a key when each script finishes so PowerShell can continue. Setup uses
your installed Python 3.13; you do not need to downgrade. Existing imported CSVs
and the OneDrive workspace are reused. No need to download the scripts one by one.

`compass.cmd` asks for your **Compass dashboard password**, not your GitHub
password. Typing is invisible. The password and login cookie stay in memory and
are not saved. It downloads existing reports over HTTPS. It does not place trades,
send Discord messages, or change live strategy rules.

The explicit date above collects September 20–22, including any empty weekend
cohorts. For routine use, `.\compass.cmd` downloads yesterday in Chicago time.
To refresh today's developing report, use `--end YYYY-MM-DD` with today's date.
`--days 7 --end YYYY-MM-DD` collects a maximum of seven session dates per run;
repeat with earlier dates to extend the local report archive. Re-download dates
later to capture outcomes that were still pending. Old snapshots remain saved.

## What runs where

| Research | This package does locally | Data you need |
|---|---|---|
| Futures | Calculates nine signal families, three combinations and 1/3/5-minute tests, with costs and data-gap checks | Existing one-minute CSV exports |
| 0DTE, options ideas, swing options, intraday stocks | Recalculates group win rates from measured saved outcomes and reports missing/pending coverage | Read-only Compass reports |
| Morning | Preserves stock and option measurements separately, including contract variants and horizons | Read-only Compass reports |
| Daily/weekly Smoothers | Preserves available snapshots without summing repeated cumulative results | Read-only Compass reports |
| TraderMatrix, swing flow, discovery | Summarizes available research/checkpoint coverage; coverage is not profitability | Read-only Compass reports |

The report downloader is **not a full raw-data export or a new options/stocks
backtester**. Some detail endpoints are capped at 200 rows; limits stay visible.
Historical option replay needs timestamped contract quotes and observations not
provided by these endpoints. Futures provider downloads, Drift, Camarilla and
exact Pine-native replay remain unimplemented. Railway continues live collection.

## Files to send back

In your OneDrive `CompassResearch` folder:

- **`review.zip`** — futures calculations, coverage and unresolved paths.
- **`compass\analysis\compass-review.zip`** — other research reports and evidence.

Upload both ZIP files to ChatGPT. You can read each nearby `summary.md` first.
No API key or paid AI call is needed for these calculations. Report packages
contain your research evidence; do not publish them in a public repository.

`reports.cmd` rebuilds the Compass report entirely offline from saved downloads.
Futures full trade ledgers stay under `runs`; original Compass JSON snapshots stay
under `compass\downloads`. GitHub carries code, OneDrive carries your data/results.

## What changed about futures gaps

The default segment policy allows interior holes in otherwise bounded standard
sessions. It never invents prices. After an unexpected gap, features restart and
wait for 200 decision bars and 50 completed 15-minute bars. Normal daily/weekend
maintenance is recognized. Each test interval still needs five usable sessions
per development/validation/later phase; heavily incomplete inputs may remain
blocked. An uncertain trade exit is retained as unresolved, disables subsequent
entries for that variant for the session, and prevents a preliminary pass.

This update changes calculation fingerprints, so your first run recalculates.
Later unchanged runs reuse the cache. Existing results remain in their old folders.
The futures study still ends September 22; adding later CSV bars does not silently
change its dates. See README.md to create a separate study configuration.

## Set up the second computer

Install Git and standard 64-bit Python 3.11–3.13. Sign into the same OneDrive and
mark `CompassResearch` **Always keep on this device**. Then in PowerShell:

```powershell
New-Item -ItemType Directory -Force C:\Compass | Out-Null
cd C:\Compass
git clone --branch research/local-windows-runner --single-branch https://github.com/cyberstryder/market-compass.git compass-research
cd compass-research\tools\local_research
[Environment]::SetEnvironmentVariable('COMPASS_RESEARCH_HOME', "$env:USERPROFILE\OneDrive\CompassResearch", 'User')
$env:COMPASS_RESEARCH_HOME = "$env:USERPROFILE\OneDrive\CompassResearch"
.\setup.cmd
```

Use your actual local OneDrive path if different. Clone only if the checkout
doesn't already exist; otherwise use `git pull --ff-only`. Do not copy `.venv`.
Run on **one computer at a time**, and wait for OneDrive sync before switching.

## If something stops

- A futures symbol is blocked: send `review.zip`; other symbols still run. A data
  block does not mean the strategy lost money. Do not lower gates just to pass.
- Compass says HTTP 401: check the dashboard password. HTTP 429: stop retrying
  and try later. HTTP 503/network failure: downloaded parts are retained; rerun
  later. Missing reports are not passed checks.
- Git refuses to update: keep the error and ask for help; do not force/reset away
  local changes. Put custom study configurations in OneDrive, outside the checkout.
- New OneDrive files are cloud-only: make them available offline before running.

The automated suite tests calculations and downloader behavior with fixtures.
The new live download and Windows wrappers need their first run on your PCs.
