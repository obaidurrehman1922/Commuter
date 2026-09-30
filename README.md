# Commute tracker

Finds the best time to leave for the daily drive between Chah Miran and the office in Lahore.
A GitHub Actions job asks the Google Routes API for the drive time with live traffic every
15 minutes during the commute windows and stores each result in `commute.db` (SQLite), which
is committed back to this repo. A report turns the data into a heatmap of median drive
times by weekday and departure time.

| Direction | Window (Lahore time, Mon–Fri) |
|---|---|
| Home → Office (`to_office`) | 07:00–10:30 |
| Office → Home (`to_home`) | 16:30–21:00 |

> **Keep this repository private.** It contains your home and office addresses, and
> `commute.db` records when you usually travel between them.

## Setup

1. **Set the office address.** Replace the `OFFICE ADDRESS` placeholder in the `OFFICE`
   setting at the top of `commute_tracker.py`. The windows, slot length, workdays and
   time zone are set in the same place.
2. **Get a Google Maps API key.**
   In the [Google Cloud console](https://console.cloud.google.com/), create a project, attach
   billing, enable the **Routes API**, and create an API key under *APIs & Services →
   Credentials*. Restrict the key to the Routes API.
3. **Add the key to GitHub.** In the repo, open *Settings → Environments* and create an
   environment named `ENVIRONMENT NAME`. If you use a different name, change `environment:`
   in `.github/workflows/commute.yml` to match. Add an environment secret called
   `GOOGLE_MAPS_API_KEY` containing the key. Don't add required reviewers or a wait timer
   to the environment: every scheduled run would wait for them.
4. **Push to the default branch.** Scheduled workflows only run from the default branch.
5. **Check that it works.** During a commute window, open *Actions → Commute tracker →
   Run workflow*. The *Record commute time* step should log `Recorded 1 of 1 live trip(s)`,
   and a `Record commute times …` commit should appear. Outside the windows the run logs
   `outside the commute windows` and does nothing.

The workflow's cron schedule is in UTC (Lahore is UTC+5) and fires every 15 minutes from
07:00 to 10:45 and from 16:00 to 21:45 Lahore time. The script skips any run outside the
windows without calling the API. GitHub often starts scheduled runs a few minutes late, so
the script uses the 15-minute slot a run belongs to: a run starting at 10:37 still counts
as the 10:30 slot. Under heavy load GitHub can also skip runs entirely.

## Running locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GOOGLE_MAPS_API_KEY=your-key

git pull                                     # get the latest commute.db first
python commute_tracker.py predict            # predicted times for the next 7 days
python commute_tracker.py report --source predicted
```

| Command | What it does |
|---|---|
| `poll` | Records live traffic for the current window. Does nothing outside the windows or on weekends. |
| `poll --force` | Records both directions right now, whatever the time. These are stored as ordinary `live` rows and show up in the report. |
| `predict` | Asks for Google's traffic prediction at every 15-minute slot in the windows on each workday over the next 7 days (about 170 API calls). Rows are stored as `predicted`. |
| `report [--source live\|predicted\|all]` | Writes `commute_heatmap.png` and prints the best 3 departure slots, the worst slot, the average congestion index and the most common routes for each direction. Defaults to `live`. |

`predict` gives you a heatmap straight away. The live data from the scheduled job takes a
few weeks to become reliable.

**Avoid conflicts on `commute.db`.** The workflow commits `commute.db` every 15 minutes
during the windows, and git can't merge two versions of a binary file. If you keep local
changes (for example from `predict`), pull just before running, then commit and push right
away. Otherwise discard them with `git checkout -- commute.db`. Running `report` doesn't
change the database.

## Data

`commute.db` has one table, `trips`:

| Column | Meaning |
|---|---|
| `recorded_at` | When the row was written (Lahore time, ISO 8601) |
| `depart_at` | Departure time (Lahore time, ISO 8601). For live rows this is the time of the request |
| `direction` | `to_office` or `to_home` |
| `source` | `live` or `predicted` |
| `duration_s` | Drive time with traffic, in seconds |
| `static_s` | Drive time without traffic, in seconds |
| `distance_m` | Route length, in metres |
| `via` | Google's route description, e.g. the main road taken |

The congestion index in the report is `duration_s / static_s`: 1.5 means the drive takes
50% longer than it would on empty roads.

## Costs and limits

- **Routes API.** Scheduled polling makes about 34 calls per workday (around 750 a
  month), and each `predict` run makes about 170. Traffic-aware routing is billed at a
  higher rate than basic routing, so check the current
  [Google Maps Platform pricing](https://developers.google.com/maps/billing-and-pricing/pricing)
  and its free monthly allowance.
- **GitHub Actions minutes.** Private repos get a limited number of free minutes a month,
  and each run is billed as at least one minute. The schedule starts about 40 runs per
  workday, roughly 900 minutes a month, which fits the Free plan's 2,000 minutes.

## Troubleshooting

- **A run fails with `GOOGLE_MAPS_API_KEY is not set`.** Check the secret name and the
  environment name in the workflow.
- **API errors don't fail the run.** A failed request is retried up to 3 times on
  429/5xx/network errors. After that the error is logged and the run still succeeds,
  so one bad call never loses a run. As a result, a wrong or restricted key only shows up
  in the logs, so check a run's log after setup.
- **`git pull --rebase` fails in the workflow.** Someone pushed a conflicting
  `commute.db`. That run's single data point is lost, and the next run starts from the
  latest version.
