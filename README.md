# kereby flat monitor

Polls [kereby.dk/bolig/](https://kereby.dk/bolig/) on a schedule and emails you
when a flat becomes available, either as a brand new listing or because a
reservation fell through and the flat is back on the market.

## How it works

`monitor.py` fetches the listing page, parses every `jorato-case-card` article,
and compares the result against `listings_state.json` from the previous run.

| Transition | Action |
| --- | --- |
| Card id not seen before, state `available` | Email as NEW |
| Was not available, now `available` | Email as REPOSTED |
| Was `available`, now not available | Log to stdout only |
| No change | Nothing |

A brand new flat that first appears as `reserved` is recorded silently and then
emailed as a REPOSTED listing if it later turns available, so it is not lost.

### The three states

The page uses `available`, `reserved`, and `completed`, not just the first two.
Anything that is not `available` is treated as unavailable, so a flat coming
back from either `reserved` or `completed` triggers an alert.

After the comparison the state file is overwritten with the current snapshot and
committed back to the repo by the workflow.

### Known blind spots

**Reserve then cancel between runs.** If a flat is reserved and then released
again entirely between two runs, both snapshots look identical and nothing is
reported. A shorter polling interval narrows the window but cannot close it.

**The 24 listing render cap.** The jorato catalog widget renders at most
`data-jorato-limit` cards into the HTML, currently 24. There is no pagination
and the limit is fixed server side, so a query string cannot raise it. With 17
flats listed today there is plenty of headroom, but if kereby ever exceeds the
cap the overflow would be invisible.

The monitor guards against this rather than failing silently. It reads the
"N resultater" header, which always reports the true total, and compares it
against the number of cards it actually parsed. If the total is higher it prints
a warning and emails you that the monitor may be missing listings, even on a run
with no other alerts. If you see that warning, the parser needs a different data
source, most likely the jorato API behind the widget.

## Setup

### 1. Create the repository

Push these files to a GitHub repository:

```
monitor.py
requirements.txt
.github/workflows/monitor.yml
README.md
```

### 2. Generate the baseline locally

Run once on your machine so the first run does not email you every flat on the
page:

```bash
pip install -r requirements.txt
python monitor.py
```

The first run writes `listings_state.json` and sends no email. Verify the parser
looks right first if you want:

```bash
DEBUG=1 python monitor.py
```

`DEBUG=1` prints every listing with its id, state, address, and rent, then exits
without diffing, emailing, or writing state.

Commit the generated baseline:

```bash
git add listings_state.json
git commit -m "Add baseline listing state"
git push
```

### 3. Add the Actions secrets

In the repository, go to Settings > Secrets and variables > Actions and add:

| Secret | Example | Notes |
| --- | --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | STARTTLS port |
| `SMTP_USER` | `you@gmail.com` | Also used as the From address |
| `SMTP_PASS` | 16 character app password | See the Gmail note below |
| `EMAIL_TO` | `you@gmail.com` | Optional, defaults to `SMTP_USER` |

**Gmail note:** you cannot use your normal account password. Turn on 2 step
verification, then create an App Password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
It is 16 characters. Paste it into `SMTP_PASS` without spaces.

If the SMTP variables are missing or incomplete, the script does not crash. It
prints the email it would have sent to stdout instead, which makes local testing
painless.

### 4. Trigger a manual run to test

Go to the Actions tab, select "kereby flat monitor", and click "Run workflow".
Check the run log. On a quiet run you should see something like
`No new or reposted flats. Tracking 17 listing(s).`

To force an alert, hand edit `listings_state.json` and change one flat's
`"state"` from `"available"` to `"reserved"`, commit that, then trigger a run.
You should get exactly one repost email, and no duplicate on the run after.

## Schedule

GitHub's native cron scheduler can delay or drop runs, so the reliable schedule
uses [cron-job.org](https://cron-job.org/) to call the workflow dispatch API
every 5 minutes. The workflow intentionally exposes only its
`workflow_dispatch` trigger; scheduling is managed by cron-job.org.

### Configure cron-job.org

1. In GitHub, create a fine-grained personal access token under **Settings >
   Developer settings > Personal access tokens > Fine-grained tokens**.
2. Set the resource owner to `annabzinkowska`, grant access only to
   `kereby-monitor`, and give it **Actions: Read and write** permission.
3. Give the token an expiration date and add a reminder to replace it before it
   expires. Do not commit the token or add it to this repository.
4. Create a cron-job.org job that runs every 5 minutes with these settings:

| Setting | Value |
| --- | --- |
| Method | `POST` |
| URL | `https://api.github.com/repos/annabzinkowska/kereby-monitor/actions/workflows/monitor.yml/dispatches` |
| Body | `{"ref":"main"}` |
| Successful response | HTTP `204` |

Add these request headers, replacing `<token>` only in cron-job.org:

```text
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
```

Enable cron-job.org failure notifications. Use its test function and confirm a
`204` response, then check that GitHub shows a successful run with event
`workflow_dispatch`.

The workflow's `concurrency` group serializes dispatches, so a delayed or
duplicate request cannot run alongside another monitor job and corrupt the
state file.

### Scheduler recovery

If external runs stop, check the cron-job.org execution history first. A `401`
or `403` response normally means the GitHub token expired, was revoked, or no
longer has Actions write permission. Create a replacement fine-grained token,
update it only in cron-job.org, and use the test function again. The workflow
can always be started manually from GitHub's Actions tab while the scheduler is
being repaired.

## Configuration

| Environment variable | Purpose |
| --- | --- |
| `DEBUG=1` | Parse and print listings, then exit. No diff, email, or state write. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO` | Email delivery |

The target URL and state file name are module level constants at the top of
`monitor.py`.
