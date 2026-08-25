# hetzner-stock-watch

Watches the Hetzner Cloud API and pushes a phone notification the moment a
**32 GB cost-optimized server** (x86 `cx`/`cpx` or Arm64 `cax`) is actually
buyable in a region you care about.

- One dependency-free Python script (`check_hetzner.py`, standard library only).
- Runs on GitHub Actions on a schedule; alerts through [ntfy.sh](https://ntfy.sh) or any webhook.
- Alerts **once** per (server type, location) — a plan that stays in stock will not
  re-notify you every five minutes.

---

## Quick start

The monitor workflow ships **disabled**, so it can't fail on a missing token before
you've had a chance to add one. Three steps to turn it on:

```bash
# 1. Create a READ-ONLY Hetzner API token (see below) and add it.
gh secret set HCLOUD_TOKEN          # paste the token when prompted
gh secret set NTFY_TOPIC            # paste your topic name (skip if already set)

# 2. Subscribe to that topic on your phone (ntfy app) or at https://ntfy.sh/<TOPIC>

# 3. Enable the schedule, then prove the whole chain works end to end.
gh workflow enable monitor.yml
gh workflow run monitor.yml -f notify_test=true   # works without HCLOUD_TOKEN too
```

If a "Hetzner stock watch is live" notification lands on your phone, you're done —
the schedule takes over from there. Check `gh run list --workflow=monitor.yml` if not.

Turn it off again once you've got your server: `gh workflow disable monitor.yml`.

---

## 1. Get a read-only Hetzner API token

The script only ever issues `GET` requests, so give it the weakest token that works.

1. Open the [Hetzner Cloud Console](https://console.hetzner.cloud/projects).
2. Select the project you'd deploy into (tokens are **per project**, and availability
   is identical across projects, so any project will do).
3. **Security → API tokens → Generate API token**.
4. Description: `stock-watch`. Permissions: **Read** — *not* Read & Write.
5. Copy the token now; the console shows it exactly once.

## 2. Set the repository secrets

```bash
gh secret set HCLOUD_TOKEN --body 'your-hetzner-read-token'
gh secret set NTFY_TOPIC   --body 'your-topic-name'
```

Optional secrets, only if you need them:

| Secret | Purpose |
|---|---|
| `NTFY_SERVER` | Self-hosted ntfy instance (default `https://ntfy.sh`) |
| `NTFY_TOKEN` | Bearer token, if your topic is access-protected |
| `WEBHOOK_URL` | Generic fallback: receives a JSON `POST` with the full offer list |

`WEBHOOK_URL` can be used instead of *or alongside* ntfy — Slack, Discord, and
n8n-style endpoints all work. Its payload is `{title, text, offers[], console_url}`.

> **Treat `NTFY_TOPIC` as a password.** On the public ntfy.sh, anyone who knows a topic
> name can read and post to it. Use something unguessable, e.g.
> `python3 -c "import secrets; print('hz-' + secrets.token_hex(8))"`.

## 3. Subscribe to notifications

- **Android / iOS** — install [ntfy](https://ntfy.sh/docs/subscribe/phone/), tap **+**,
  enter your topic name. Enable "override Do Not Disturb" so the max-priority
  availability alert actually wakes you.
- **Desktop / browser** — open `https://ntfy.sh/<YOUR_TOPIC>` and allow notifications,
  or `ntfy subscribe <YOUR_TOPIC>` with the [CLI](https://ntfy.sh/docs/subscribe/cli/).

Availability alerts are sent at priority `urgent` (5) with a tap-through link straight
to the Hetzner console.

---

## Cost

**On a public repo, standard runners are unmetered — this costs nothing at any cadence.**

On a *private* repo, minutes are billed and GitHub
[rounds every job up to a whole minute](https://docs.github.com/en/billing/reference/actions-runner-pricing),
so a 20-second run bills as 1. The monthly allowance (2,000 min on Free, 3,000 on
Pro/Team) is shared across **all** your private repos, so a frequent poller can quietly
crowd out the CI you actually depend on.

| Cron | Runs/month | Billed min/month | Fits a 3,000-min allowance? |
|---|---|---|---|
| `*/5 * * * *` | ~8,770 | ~8,770 | no — ~$35/mo over |
| `*/10 * * * *` | ~4,380 | ~4,380 | no — ~$8/mo over |
| `*/30 * * * *` (default) | ~1,460 | ~1,460 | only if ~half your allowance is free |

Check what's left before committing to a private schedule:

```bash
gh api "/users/$(gh api /user -q .login)/settings/billing/usage?year=$(date +%Y)&month=$(date +%-m)" \
  -q '[.usageItems[] | select(.product=="actions" and .unitType=="Minutes")] | map(.quantity) | add'
```

That total covers public *and* private repos; only the private ones draw down the
allowance. If the headroom is tight, make this repo public, or run it on a machine you
already own — the script is self-contained:

```
*/30 * * * * cd /path/to/repo && HCLOUD_TOKEN=... NTFY_TOPIC=... python3 check_hetzner.py
```

### Is a scheduled monitor allowed on a public repo?

[GitHub's Actions terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features)
set two limits that matter here, and this workflow sits inside both:

> Any activity that places a burden on our servers, where that burden is
> disproportionate to the benefits provided to users (for example, don't use Actions as
> a content delivery network or as part of a serverless application, **but a low benefit
> Action could be ok if it's also low burden**)

> If using GitHub-hosted runners, any other activity **unrelated to the production,
> testing, deployment, or publication of the software project associated with the
> repository** where GitHub Actions are used

A ~20-second job 48 times a day is low burden by any reading of the first. And the
second turns on the word *unrelated*: the software project associated with this
repository **is** the stock checker, so running it is the project, not an unrelated
workload parked on a convenient runner. That distinction is the whole point of the
clause — it exists to stop Actions being used as free general-purpose compute.

Keep it that way: don't crank the cadence back up on a public repo just because the
minutes are free, and don't graft unrelated jobs onto this repo.

## Two scheduling caveats

- **`schedule` is best-effort.** GitHub enforces a 5-minute floor and openly delays or
  drops scheduled runs under load — 10–20 minutes late is common on the hour. At `*/30`
  that means "within the hour", which is fine for restocks that stay open for hours. If
  you ever need to-the-minute reaction, run the script from a machine you control.
- **Scheduled workflows are disabled after 60 days without repo activity.** If alerts
  go quiet for a long stretch, check the Actions tab; any commit, or
  `gh workflow enable monitor.yml`, brings it back.

---

## Tuning what it watches

Defaults: **≥ 30 GB RAM**, **shared vCPU** (the cost-optimized `cx`/`cpx`/`cax` lines),
**any location**. That is deliberately broad — it catches every 32 GB cost-optimized
plan on both architectures without hardcoding model names that Hetzner renames between
generations.

Narrow it with **repository variables** (Settings → Secrets and variables → Variables,
or `gh variable set`) — no code change needed:

| Variable | Default | Example |
|---|---|---|
| `MIN_MEMORY_GB` | `30` | `60` |
| `MAX_MEMORY_GB` | *(none)* | `32` — pins it to 32 GB exactly |
| `MAX_PRICE_MONTHLY` | *(none)* | `80` — ignore anything dearer (gross, incl. VAT) |
| `CPU_TYPES` | `shared` | `shared,dedicated` or `*` for any |
| `ARCHITECTURES` | *(any)* | `arm` or `x86` |
| `LOCATIONS` | *(any)* | `fsn1,nbg1,hel1` for EU only |
| `SERVER_TYPE_FAMILIES` | *(any)* | `cx,cax` to drop the pricier `cpx` line |
| `SERVER_TYPES` | *(none)* | `cx52,cax41` — exact allowlist, overrides every filter above |
| `INCLUDE_DEPRECATED` | `false` | `true` to also accept sunsetting plans |
| `NOTIFY_MODE` | `new` | `always` (alert every run) / `never` (log only) |
| `CURRENCY` | `EUR` | label only, for the price shown in alerts |

```bash
gh variable set LOCATIONS --body 'fsn1,nbg1,hel1'
gh variable set MAX_PRICE_MONTHLY --body '80'
```

Every variable has a matching CLI flag — see `python3 check_hetzner.py --help`.

### Set a price ceiling, or you'll get one useless alert and then silence

At the time of writing, the 32 GB shared-vCPU line looks like this in the EU:

| Type | Arch | RAM | Price/mo | fsn1 / nbg1 / hel1 |
|---|---|---|---|---|
| `cx53` | x86 | 32 GB | €34.99 | sold out |
| `cax41` | arm | 32 GB | €48.49 | sold out |
| `cpx62` | x86 | 32 GB | €152.99 | **in stock** |

Everything on that list is "shared vCPU, 32 GB", so a filter based only on RAM and
`cpu_type` matches `cpx62` — which is always in stock at four times the price. You'd
get one alert immediately, and then, because alerts are deduplicated, nothing at all
when `cx53` actually came back.

Family names don't rescue you either: `cpx51` is €279/mo while `cpx62` is €153/mo, so
the numbering doesn't order by price. **`MAX_PRICE_MONTHLY` is the filter that
expresses what "cost-optimized" actually means.** `SERVER_TYPE_FAMILIES=cx,cax` also
works, but it silently excludes any new cheap family Hetzner introduces.

## Running it locally

```bash
export HCLOUD_TOKEN='your-read-only-token'
export NTFY_TOPIC='your-topic'

python3 check_hetzner.py                      # one poll, alert if something is new
python3 check_hetzner.py --notify-mode never  # look, don't alert
python3 check_hetzner.py --json               # machine-readable result
python3 check_hetzner.py --reset-state        # re-alert on stock you were already told about

# No token? Exercise the whole pipeline against the bundled sample response:
python3 check_hetzner.py --fixture tests/fixture_server_types.json
python3 -m unittest discover -s tests -v
```

### If your token lives under a different name

A keyfile holding many credentials needs disambiguating names (`RO_TW_HETZNER`:
read-only, TW server, Hetzner). A single-purpose environment wants the ecosystem
convention — `HCLOUD_TOKEN` is what the `hcloud` CLI and the Terraform provider read,
so anything you drop in works untranslated. Both names are right in their own scope.

Rather than renaming either side or copying the secret twice, point at it:

```bash
source ~/your-keyfile
HCLOUD_TOKEN_VAR=RO_TW_HETZNER python3 check_hetzner.py
```

`HCLOUD_TOKEN_VAR` names the variable to read the token *from*, and takes precedence
over `HCLOUD_TOKEN`. Handy for switching projects, too — the same repo can be pointed
at a different Hetzner project by changing one variable name. (Availability and pricing
are identical across projects, so one token is enough for monitoring.)

## How availability is actually determined

Hetzner used to report stock under `datacenters[].server_types.available`. **That field
is marked deprecated in the current [OpenAPI spec](https://docs.hetzner.cloud/cloud.spec.json)**,
so this script does not use it. The live signal now lives on the server type itself, in
`GET /v1/server_types` → `server_types[].locations[]`:

- the type is **only offered** in the locations listed;
- `available: false` → **temporarily** out of stock;
- `deprecation != null` → being retired in that location;
- `deprecation.unavailable_after` in the past → **permanently** gone.

A location counts as a hit when it is listed, not sold out, and not retired. That is one
API call per poll (plus a best-effort `GET /v1/locations` purely to print "Falkenstein, DE"
instead of "fsn1" — if it fails, the alert still goes out).

If a location entry has **no** `available` key at all, it is treated as *available*. This
tool's job is to err toward telling you too early rather than staying silent, and the
deduplication state means a false positive costs you one notification, not a stream.

## Repeat-alert suppression

The set of already-announced `type@location` pairs is written to `.state/seen.json` and
carried between runs by the Actions cache. You get one alert per pair; if a plan sells
out and comes back, the pair disappears from the set and the return is announced again.

To deliberately re-alert on everything currently in stock:

```bash
gh workflow run monitor.yml -f reset_state=true
```

## Layout

```
check_hetzner.py                  the checker: fetch, filter, dedupe, notify
.github/workflows/monitor.yml     the schedule + manual dispatch
.github/workflows/tests.yml       runs the suite on every push
tests/test_check_hetzner.py       23 tests, standard library only
tests/fixture_server_types.json   sample API response covering the tricky cases
```
