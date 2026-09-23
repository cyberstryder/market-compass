# Compass local research runner

Run the repetitive research on your Windows computer. No OpenAI subscription/API,
GPU, broker login, or connection to the production database is needed. The runner
makes no network requests. Setup downloads Python dependencies once.

This is an offline **futures signal-family screen**, not a live scanner, complete
Compass replica, optimizer, or exact Pine backtest. Continuous collection stays
on Railway. Automatic provider downloads are not wired up: use your existing
TradingView/data-provider CSV exports. No paid data request happens implicitly.

## 1. Set up EACH computer once

Install **Git for Windows** from https://git-scm.com/downloads/win and **64-bit
Python 3.11, 3.12 or 3.13** from https://www.python.org/downloads/windows/ (include the Python
launcher). Open PowerShell in the folder where you want your projects and run:

```powershell
git clone --branch research/local-windows-runner --single-branch https://github.com/cyberstryder/market-compass.git compass-research
cd compass-research\tools\local_research
.\setup.cmd
```

Git may ask you to sign in to your existing GitHub account. No credential should
be pasted into a script. Setup creates an isolated `.venv`, installs pinned
packages and runs the test suite. Setup first tries your installed `python`, then
the launcher for 3.13/3.12/3.11; no downgrade is required if you have 3.13.
If none is found, install a supported version and reopen PowerShell.
No PowerShell execution-policy change is needed.

Do not copy `.venv` to the second computer: run setup there too. The code was
verified in Linux with Python 3.11; Windows batch setup must still be exercised
on your machines. Dependency pins support Python 3.11 through 3.13.

## 2. Choose where data/results live

Default: `tools\local_research\workspace` on each computer. These files are
ignored by Git. **GitHub synchronizes code/configuration, not datasets/results.**

For switching PCs, use OneDrive (if already installed) or another file-sync
folder. On EACH PC set a user environment variable pointing to that PC's local
path, for example:

```powershell
[Environment]::SetEnvironmentVariable('COMPASS_RESEARCH_HOME', "$env:USERPROFILE\OneDrive\CompassResearch", 'User')
$env:COMPASS_RESEARCH_HOME = "$env:USERPROFILE\OneDrive\CompassResearch"
```

Use your actual OneDrive path if different. Mark the folder **Always keep on this
device**. Run on **only one computer at a time** and wait for sync to finish before
switching. The runner does not provide distributed locking. Do not import CSVs
while a run is active. You can also use an external drive or copy the workspace
folder manually. For a one-off location add `--workspace "D:\CompassResearch"`.

Keep the git checkout OUTSIDE the synced data folder. This avoids syncing Git's
internal files and virtual environments.

## 3. Import price history

Export standard **one-minute** chart OHLC, preferably with raw volume, from
TradingView. Timestamp means the bar's OPEN time. Keep contract/continuous symbol,
roll/back-adjustment setting, timezone and session settings consistent. Do not
use Heikin Ashi/Renko prices or a strategy trade-list export.

From `tools\local_research`, run:

```powershell
.\import.cmd MNQ
```

A file picker opens. Choose your MNQ CSV. Repeat for MES/MGC/SIL/etc. Or provide
a filename:

```powershell
.\import.cmd MES "C:\Users\YourName\Downloads\MES-export.csv"
```

The importer normalizes columns and saves `input\MNQ.csv`, merges matching
history, rejects conflicting overlap, and keeps a backup before changing an
existing file. `--replace` intentionally replaces the full history and preserves
the old file in `input-backups`. It cannot infer the true ticker from OHLC values;
select the correct symbol. It never fills missing minutes with invented prices.

Supported columns: `time,open,high,low,close` and optional `volume` (case-insensitive).
Time may be Unix **seconds**, or ISO text containing `Z` or a UTC offset. Naive
local timestamps, duplicates, malformed prices and coarser-only histories block
that instrument. Indicator columns are ignored; 'Total' is not assumed volume.

## 4. Run

First inspect coverage:

```powershell
.\run.cmd --check
```

Then test available inputs:

```powershell
.\run.cmd
```

Or run selected instruments:

```powershell
.\run.cmd --symbols MNQ MGC
```

Missing instruments are marked pending. Invalid/insufficient data is blocked,
never scored as a win or loss. A blocked instrument does not stop the others.
The process returns exit code 2 for blocked inputs; missing CSVs remain pending.

**Before changing dates:** `config.json` currently reproduces our August 24–
September 22, 2026 research split: development through Sept 3, validation through
Sept 11, later through Sept 22. At least five complete sessions per slice are
required. New bars after `later_end` are not included automatically. This prevents
quietly moving the evaluation window each time data arrives. For another period,
copy `config.json` into the shared workspace as `study.json`, edit the three end
dates, then use:

```powershell
.\run.cmd --config "$env:COMPASS_RESEARCH_HOME\study.json"
```

Copy only once, so the second PC reuses that configuration. Keep experiments
bounded: routinely changing dates/rules based on final results is overfitting.

## 5. What to send back

Open `summary.md` in your workspace. Upload **`review.zip`** to ChatGPT when you
want interpretation. It contains compact results, coverage, code/config/data
fingerprints and errors—not raw prices, credentials or the full trade ledger.
The full trade CSVs are in `runs\SYMBOL\<fingerprint>\trades.csv` for targeted
investigation. Each current review describes the symbols requested in that run.

Unchanged input + configuration + code + dependency/Python versions reuse cached
results. Changing any of those creates a separate run folder. Failed runs retry.
Only completed runs are reused; deleting a run folder forces recomputation.
Hashing/reading the input still takes time; signal calculations are what gets
skipped. Older runs are retained for comparison. No claim of profitability is
automatically promoted into live rules.

## 6. Update code on either computer

From the checkout:

```powershell
git pull --ff-only
cd tools\local_research
.\setup.cmd
```

Run this from the repository root (if already in `tools\local_research`, only
run `git pull --ff-only` and `.\setup.cmd`). GitHub holds the research branch;
you do not need a main-branch merge or Railway deployment to use it. Personal
study configurations should live in your workspace so they don't conflict with
code updates. Nothing automatically commits or uploads data.

## Research scope and fixed assumptions

- 1/3/5-minute signals, executed against original one-minute paths.
- Nine families: existing trend breakout/pullback, band re-entry, failed breakout;
  uploaded Stochastic Pop, ORB, Location, Signal and Fractal delivery-review ports.
- Three bounded combinations: standalone, trend agreement, trend + efficiency.
- Common exit: 1.5 ATR14 stop, 2R target, max 60 minutes, 10-minute cooldown,
  flatten 15:45 Chicago. ORB has its narrower US cash window. Source-specific
  daily caps remain for ORB/Location/Signal; no paper account loss limit applies.
- One contract per simulation. Instrument tick/point multipliers are explicit in
  config; **fees are illustrative round-trip assumptions**, not broker quotes.
  Verify each against your actual contract/commission before interpreting dollars.
- Base/stressed costs deduct configured fees + one/four adverse ticks per side.
  They do not model variable spread, depth, partial fills or actual broker margin.
- Conservative stop-first treatment for an ambiguous minute; worse-open gap stop.
- Complete standard 17:00–16:00 CT sessions only. Holidays/shortened sessions are
  excluded rather than modeled; a holiday calendar is not implemented here.
- Earlier prices warm up features. Later dates are retrospective checks, not
  untouched holdouts if already inspected. No multiple-testing correction.
- Drift and Camarilla are pending implementations. Raw volume does not enable
  them automatically. Fractal manual POI/bias, FVG mode, and Pine-native exits
  are not reproduced. Event-by-event TradingView parity is still unverified.
- BTC/options/equities require different sessions/models and are not supported.

These limitations stay in the compact report so local speed doesn't turn an
incomplete test into a passed strategy.
