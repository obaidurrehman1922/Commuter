# Commute tracker

Finds the best time to leave for the daily drive between home (Shayyan Furniture, Chah Miran)
and the office (04 Old FCC Road) in Lahore.
A GitHub Actions job asks the Google Routes API for the drive time with live traffic in both
directions every 15 minutes from 07:30 to midnight, every day, and stores each result in `commute.db`
(SQLite), which is committed back to this repo. After each new trip the workflow rebuilds an interactive
dashboard and publishes it with GitHub Pages at
<https://obaidurrehman1922.github.io/Commuter/>.

| Direction | Window (Lahore time, every day) |
|---|---|
| Home → Office (`to_office`) | 09:00–11:00 |
| Office → Home (`to_home`) | 19:00–21:00 |

The windows only decide where departure times are suggested. The dashboard shows both
legs side by side. Each has a 24-hour traffic chart and an hour-by-hour week heatmap built
from every reading, with the window marked, plus a best time to leave, window chart and daily
trend that count only departures inside that leg's window. The `report` command's
suggestions and heatmap also cover only the windows.

> **Privacy.** The code contains your home and office addresses, and `commute.db` records
> when you travel between them. Keep the repository private if you can; on a free GitHub
> plan, GitHub Pages needs it public.

## Setup

1. **Check the addresses.** `HOME` and `OFFICE` are at the top of `commute_tracker.py`,
   along with the windows, slot length, the days to track (`DAYS`), the hours to skip
   (`QUIET_HOURS`, 00:00–07:30) and time zone. After the first run, check
   that the distance in the log matches the route in Google Maps. If Google placed an
   address in the wrong spot, make the address more specific.
2. **Get a Google Maps API key.**
   In the [Google Cloud console](https://console.cloud.google.com/), create a project, attach
   billing, enable the **Routes API**, and create an API key under *APIs & Services →
   Credentials*. Restrict the key to the Routes API.
3. **Add the key to GitHub.** In the repo, open *Settings → Secrets and variables →
   Actions* and click *New repository secret*. Name it `GOOGLE_MAPS_API_KEY` and paste
   the key as the value.
4. **Push to the default branch.** Scheduled workflows only run from the default branch.
5. **Turn on GitHub Pages.** Open *Settings → Pages* and under *Build and deployment* set
   *Source* to **GitHub Actions**. On a free GitHub plan, Pages only works while the repo
   is public.
6. **Check that it works.** Open *Actions → Commute tracker → Run workflow*. The *Record
   commute time* step logs `Recorded 2 of 2 live trip(s)` and a `Record commute times …`
   commit appears. A manual run also publishes the dashboard, and the *pages* job shows its
   link.

### Dashboard website

The `pages` job runs after every run that recorded a trip, and after every manual run. It
builds `commute_dashboard.html` from `commute.db` and publishes it as the site's front page,
so the dashboard is never more than one trip behind. Nothing is committed for this; the page
is uploaded straight to GitHub Pages.

The site is public: anyone with the link can see your drive times, departure times and the
roads Google chose. It doesn't include your addresses, and it asks search engines not to
index it.

The workflow fires every 15 minutes from 07:30 to 23:45 Lahore time, every day (its cron
schedule is in UTC, Lahore is UTC+5, so that is 02:30 to 18:45 UTC). The script also skips
`QUIET_HOURS` and any day left out of `DAYS`, so if you change either, change the cron lines
to match. GitHub often starts scheduled runs a few
minutes late; each trip is grouped into the 15-minute slot it actually left in, so a run that
starts at 10:37 counts as the 10:30 slot. Under heavy load GitHub can also skip runs entirely.

### Google's forecast

Every Sunday at 20:20 Lahore time the workflow runs `predict` instead of `poll`. It stores
Google's traffic forecast for every slot in both windows on each day of the coming week, which
fills the dashboard's *Google forecast* view. To refresh it at another time, open *Actions →
Commute tracker → Run workflow* and choose `predict`.

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
| `poll` | Records live traffic in both directions. Does nothing in `QUIET_HOURS` (00:00–07:30) or on days left out of `DAYS` (every day is tracked by default). |
| `poll --force` | Records both directions regardless of the day or the quiet hours. |
| `predict` | Asks for Google's traffic prediction at every 15-minute slot in the windows on each tracked day over the next 7 days (about 126 API calls). Rows are stored as `predicted`. |
| `report [--source live\|predicted\|all]` | Writes `commute_heatmap.png` and prints the best 3 departure slots, the worst slot, the average congestion index and the most common routes for each direction. Defaults to `live`. |
| `dashboard` | Writes `commute_dashboard.html`, an interactive page you open in a browser. It shows both directions side by side: the best time to leave home and the office, drive time by departure slot, a weekday heatmap, a day-by-day trend and the routes taken, with filters for period, weekdays or weekends, and live or forecast data. The page is built from `dashboard_template.html` with your trips embedded, so it works offline. |

`predict` gives you a heatmap straight away (the workflow also runs it every Sunday). The
live data from the scheduled job takes a few weeks to become reliable.

**Avoid conflicts on `commute.db`.** The workflow commits `commute.db` every 15 minutes
every day, and git can't merge two versions of a binary file. If you keep local
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

- **Routes API.** Scheduled polling makes 132 calls a day (66 runs × 2 directions), about
  4,000 a month, and the Sunday forecast adds about 126 a week, for roughly 4,500 a month.
  That fits inside the 5,000 free requests a month on the Pro tier; past that, Google charges
  $10 per 1,000, so extra `predict` runs or a shorter `QUIET_HOURS` can push it over.
  Requests use `TRAFFIC_AWARE_OPTIMAL`,
  Google's most accurate traffic mode (set by `ROUTING_PREFERENCE` in the script). It is
  billed at the Routes API's Pro rate, the same as `TRAFFIC_AWARE`, which is higher than
  basic routing. Check the current
  [Google Maps Platform pricing](https://developers.google.com/maps/billing-and-pricing/pricing)
  and its free monthly allowance.
- **GitHub Actions minutes.** Public repos don't use up minutes. A private repo on the
  Free plan gets 2,000 a month, with each job billed as at least one minute, and this
  schedule needs about 4,000 (a recording job and a dashboard job per run), so a private
  repo would need a paid plan or a slower schedule.
- **Repository size.** Every recorded run commits `commute.db`, about 66 commits a day.
  Expect the repository to grow by a few hundred megabytes a year.

## Troubleshooting

- **A run fails with `GOOGLE_MAPS_API_KEY is not set`.** Check that the repository secret
  is named exactly `GOOGLE_MAPS_API_KEY`.
- **API errors don't fail the run.** A failed request is retried up to 3 times on
  429/5xx/network errors. After that the error is logged and the run still succeeds,
  so one bad call never loses a run. As a result, a wrong or restricted key only shows up
  in the logs, so check a run's log after setup.
- **`git pull --rebase` fails in the workflow.** Someone pushed a conflicting
  `commute.db`. That run's single data point is lost, and the next run starts from the
  latest version.
