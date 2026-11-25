# Trading Desk dashboard

A small web dashboard for the reports and theses that the skills in this repo produce,
plus a trade journal. It runs in one Docker container on your home network.

What it shows:

| Section | Where the data comes from |
|---|---|
| Exposure ceiling, breadth, uptrend tiles and history charts | `reports/exposure_posture_*.json`, `market_breadth_*.json`, `uptrend_analysis_*.json` |
| Circuit breaker | `reports/circuit_breaker_decision_*.json` (drawdown-circuit-breaker, run by the refresh job when `CB_ACCOUNT_SIZE` is set) |
| Broker account and positions | Alpaca API, read-only (GET account and positions only) |
| Theses, review-due list, journal | `state/**/th_*.yaml` and `state/**/pm_*.md` (trader-memory-core). **New thesis** adds one through trader-memory-core |
| Latest VCP screen | `reports/vcp_screener_*.json` |
| Trade journal (the only page that saves) | `trade-journal/journal.db` + `trade-journal/screenshots/`, filled by hand or from a Robinhood CSV |
| Reports page | Every report above, rendered from its `.md` or `.json` |
| Chart page (read-only) | IBKR price bars for any ticker: 1 day of 5-minute bars, 5 days of 30-minute, 3 months daily or 1 year weekly, with the last price cross-checked against Alpaca |
| Options calculator (read-only) | Your inputs. With IBKR on: the stock price, real expirations and strikes, and each leg's bid/ask, IV and Greeks from IB Gateway, cross-checked against Alpaca. Otherwise the last IEX trade and daily bars from Alpaca |

What it can't do: place, change or cancel orders. The Alpaca client in
`app/alpaca.py` only sends GET requests: `/v2/account` and `/v2/positions`, plus a
stock's latest trade and daily bars from the market data API for the Options page. The `reports/`
and `state/` folders are mounted read-only into the web container, except `state/theses/`,
which the New thesis page writes to. `trade-journal/` is the only other folder it can write to.

Your balances, P&L and journal stay on the machine that runs the dashboard.
`reports/`, `state/`, `trade-journal/` and `dashboard/.env` are all git-ignored, so they never reach GitHub.

---

## Set up on the homelab (CT 104 on juno)

These steps follow the same pattern as the Jarvis containers. Run them in order.
Anything in `<angle brackets>` is a value you fill in.

| Setting | Value |
|---|---|
| Node | juno |
| CT ID | 104 (check it's free) |
| Hostname | `trading` |
| Template | Debian 13 standard, unprivileged, `nesting=1` (for Docker) |
| CPU / RAM / swap / disk | 1 core / 1024 MB / 512 MB / 8 GB on `local-zfs` |
| Network | `vmbr0`, 192.168.40.26/24, gateway and DNS 192.168.40.1 |
| Name | `https://trading.lab.silkytech.org` through Caddy |

### 1. Create the container (shell on juno)

```bash
pct list                     # 104 must not be listed
pveam list local | grep debian-13
pct create 104 local:vztmpl/<debian-13 template file> \
  --hostname trading --ostype debian --unprivileged 1 --features nesting=1 \
  --cores 1 --memory 1024 --swap 512 --rootfs local-zfs:8 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.40.26/24,gw=192.168.40.1 \
  --nameserver 192.168.40.1 --onboot 1
pct start 104
pct enter 104
```

### 2. Inside the container: basics, time zone and Docker

```bash
passwd                                   # save it in Vaultwarden
apt update && apt full-upgrade -y
apt install -y ca-certificates curl git cron
timedatectl set-timezone America/New_York

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt update && apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
docker compose version                   # should print a version
```

### 3. Get the repo and make the data folders

```bash
git clone https://github.com/Maso1634/claude-trading-skills.git /opt/claude-trading-skills
cd /opt/claude-trading-skills
mkdir -p reports state/theses state/journal trade-journal
chown -R 1000:1000 reports state trade-journal   # the containers run as user 1000
```

To bring your existing theses over from the laptop, run this **on the laptop**
(adjust the path if your repo lives somewhere else):

```bash
scp -r ~/claude-trading-skills/state/. root@192.168.40.26:/opt/claude-trading-skills/state/
scp -r ~/claude-trading-skills/reports/. root@192.168.40.26:/opt/claude-trading-skills/reports/
```

Then on the CT run `chown -R 1000:1000 /opt/claude-trading-skills/reports /opt/claude-trading-skills/state` again.

### 4. Add your keys

```bash
cd /opt/claude-trading-skills/dashboard
cp .env.example .env
chmod 600 .env
nano .env        # paste your Alpaca PAPER key and secret, keep ALPACA_ENV=paper
```

Your keys go in this file and in Vaultwarden. Don't paste them into chat or into the vault.

### 5. Start it

```bash
cd /opt/claude-trading-skills/dashboard
docker compose up -d --build
docker compose ps                        # STATUS should say "healthy" after a minute
curl -s http://127.0.0.1:8080/healthz    # {"ok": true, ...}
```

Open `http://192.168.40.26:8080` from the Admin Workstation or over WireGuard.

### 6. Fill it with market data, then refresh every weekday

Run the refresh once by hand. It takes a few minutes and writes breadth, uptrend, macro
regime, market top, Follow-Through Day, sector rotation, theme and exposure reports:

```bash
cd /opt/claude-trading-skills/dashboard
docker compose run --rm refresh
```

Then schedule it for 4:20 PM ET on weekdays with `crontab -e`, adding this line:

```cron
20 16 * * 1-5 cd /opt/claude-trading-skills/dashboard && docker compose run --rm refresh >> /var/log/trading-refresh.log 2>&1
```

Exposure-coach gets 7 of its 8 inputs. Breadth, uptrend, sector and theme need no key;
macro regime, market top and FTD use `FMP_API_KEY` (a free key is enough: the S&P 500 and
Nasdaq indexes, VIX and Treasury rates are on the free plan) with yfinance where FMP says
no. Institutional flow is left out: it needs paid FMP 13F data that is a quarter old. A
step that fails, or writes no new report, is left out of that day's posture rather than
reusing an old report, and the summary lists it under Missing Inputs.
To also run the VCP screen nightly, set `RUN_VCP=true` and `VCP_DATA_SOURCE=auto` in `.env`.
`auto` screens the whole S&P 500 from Alpaca SIP daily bars (the same Alpaca keys the
dashboard uses; the free plan is enough because the job runs after the close), falls back
to yfinance and then FMP for anything Alpaca misses, and cross-checks the shown results
against a second source. The Overview's VCP table says where the prices came from and
marks a stock "check chart" when the sources disagree.

`VCP_DATA_SOURCE=fmp` keeps the old FMP-only screen. A free FMP key allows 250 calls a day
and no batch quotes, so with `fmp` also set `VCP_UNIVERSE` to a watchlist of about 100
symbols. `VCP_MAX_API_CALLS` (default 240) stops the run before the daily limit.

To get a push on your phone when a step fails, a backup source was used, stocks went
unevaluated, or the cross-check flags a stock, set `NTFY_URL` (and `NTFY_TOKEN` if your
topic needs one). A clean run sends nothing. Test it with
`docker compose run --rm --entrypoint python refresh /app/notify_refresh.py --failed test`.

### 7. Give it a name in Caddy

On CT 102, open `/etc/caddy/Caddyfile` and copy the existing Jarvis block (the one that
points at `192.168.40.25:8080`). Paste the copy just above the catch-all `abort`, then change
`jarvis` to `trading` and `192.168.40.25:8080` to `192.168.40.26:8080`. Jarvis is a plain web
app on port 8080 like this one, so its block is the right template. The Proxmox blocks carry
extra TLS settings you don't want.

```bash
cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-trading
nano /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --envfile /etc/caddy/caddy.env
systemctl reload caddy
```

`--envfile` matters: the Cloudflare token lives in `caddy.env`, which only the Caddy service
loads. Without it, a manual validate fails with `API token '' appears invalid`, even when the
Caddyfile is fine. A `not formatted` warning is harmless; `caddy fmt --overwrite /etc/caddy/Caddyfile` tidies it.

Watch for the colon before the port. `192.168.40.26.8080` passes `caddy validate` but gives a 502.

On janus: **Services → Unbound DNS → Overrides → Hosts → +**, host `trading`, domain
`lab.silkytech.org`, IP `192.168.40.22` (Caddy). Save and Apply.

### Updating

```bash
cd /opt/claude-trading-skills && git pull
cd dashboard && docker compose up -d --build
```

---

## Trade journal

Open **Journal** in the top menu. It's for your own trades (Robinhood today), separate
from the Alpaca account the Overview shows.

- **New trade**: symbol, buy or sell, quantity, price, fee, time (Eastern), setup, planned
  stop and target, why you took it. Add the exit later as another fill on the trade's page.
- **Import Robinhood CSV**: in Robinhood, Account → Reports and statements → Reports,
  generate an account activity report and upload the CSV. Stocks, ETFs and options come
  in; dividends, deposits and transfers are skipped. Re-importing the same file is safe.
  Fills are grouped into trades for you: a trade opens when a position goes from flat to
  open, and ends when it's flat again.
- **Trade page**: fills, P&L after fees, R multiple, grade, mistakes, lesson, screenshots,
  an optional link to a trader-memory-core thesis, and Merge (to join the legs of an
  option spread, which import as separate trades).
- **Stats**: net P&L, win rate, average win and loss, profit factor, expectancy, an equity
  curve, and breakdowns by setup, mistake, weekday and asset type.
- **Export CSV**: every fill with its trade's setup and grade.

How the data is kept: three ideas, three tables. An *instrument* is what you traded (a
stock, one option contract, a coin). A *trade* is why (setup, plan, review). A *fill* is
what happened at the broker. P&L is always worked out from the fills, first in first out,
using exact decimals, so options (multiplier 100, multi-leg) and crypto (fractional coins,
fees paid in coin) fit without changes.

The journal has no login. It relies on being reachable only from your LAN or WireGuard,
and it refuses form posts that come from any other site. Don't publish it to the internet.

**Upgrading an existing CT** (it was set up before the journal existed):

```bash
cd /opt/claude-trading-skills && git pull
mkdir -p trade-journal && chown 1000:1000 trade-journal
cd dashboard && docker compose up -d --build
```

If the Journal page says "Journal not set up", the `trade-journal` folder is missing or
isn't owned by user 1000. Run the `mkdir` and `chown` lines above and restart.

**Backups**: everything is in `/opt/claude-trading-skills/trade-journal`, which the nightly
backup of the CT covers. For an extra copy, use **Export CSV** now and then.

---

## Options calculator

Open **Options** in the top menu. Pick a strategy (long call, long put, covered call,
cash-secured put, or a bull call / bear put spread), type the strike, expiration and
premium, and the page shows the payoff at expiration (solid line) and today (dashed),
breakeven, max profit and loss, cash needed, the model's chance of profit, and the
position Greeks, each with a one-line explanation.

- Type a ticker and it fills in the stock price (last IEX trade from Alpaca) and a
  starting IV from 30-day historical volatility. For accurate numbers, replace IV and
  the premium with what Robinhood shows on the contract's page.
- Leave a premium blank and it's estimated with Black-Scholes (same formulas as
  `skills/options-strategy-advisor`, standard library only).
- Account size defaults to `CB_ACCOUNT_SIZE` from `.env`; max loss above 2% of it is flagged.
- Spreads are marked "needs Level 3" (Robinhood options level).
- A journal trade on a single option contract gets an **Open in Options calculator** button.

It saves nothing and needs no new keys or folders.

## Circuit breaker

The drawdown-circuit-breaker skill decides whether you should take **new** trade risk today,
based on realized losses in your trader-memory-core theses. It answers TRADING ALLOWED,
COOLDOWN (after a losing streak) or HALTED (a daily, weekly or monthly loss limit was hit).
It's advice only: it never touches orders.

1. In `.env`, set `CB_ACCOUNT_SIZE` to your trading account size in dollars. The default
   limits are 2% a day, a 24-hour cooldown after 2 losses in a row, 5% a week and 8% a month.
   Override any of them with the `CB_*` lines in `.env.example`.
2. Rebuild once so the image gets `pandas-market-calendars` (market holidays):
   `docker compose up -d --build`.
3. Test: `docker compose run --rm refresh circuit`. The Overview tile then shows the
   decision, realized P&L for today, the week and the month, and the losing streak.
4. The 4:20 PM refresh already includes it. Add a morning run so the tile is fresh before
   the open (`crontab -e`):
   ```cron
   0 9 * * 1-5 cd /opt/claude-trading-skills/dashboard && docker compose run --rm refresh circuit >> /var/log/trading-refresh.log 2>&1
   ```

It only knows about trades recorded as theses with an outcome (closed through
trader-memory-core). With no closed theses it reports TRADING ALLOWED with data quality
"empty state". The refresh job mounts `state/theses` read-only, so it can't change them.

---

## Adding theses

**Theses → New thesis** creates a trader-memory-core thesis from a form: ticker, type,
setup, catalyst, planned entry, stop, target, date and the thesis itself. The dashboard
calls trader-memory-core's own `manual` ingest and `register()`, the same code as
`trader_memory_cli.py ingest --source manual`, so the file, ID and index are identical
to a CLI-made thesis and the skills (circuit breaker, reviews) see it right away.

- New theses start as **IDEA**. Moving one to ENTRY_READY or ACTIVE is still done with the
  CLI (`transition`, `open-position`).
- Long stock theses only, and the stop must be below the planned entry. trader-memory-core
  refuses anything else, and the form shows its error.
- It needs the repo's `skills/` folder mounted read-only (`SKILLS_DIR`) and `state/theses`
  mounted writable (`THESES_DIR`). `compose.yaml` does both.

**Upgrading an existing CT**: `git pull`, check that `state/theses` is owned by user 1000
(`ls -ln /opt/claude-trading-skills/state`; if not, `chown -R 1000:1000 state`), then
`docker compose up -d --build`. The rebuild installs `jsonschema`, which trader-memory-core needs.

---

## Interactive Brokers (IBKR) data (optional, phase 1)

IBKR's API has no API key. Programs talk to **IB Gateway**, a small logged-in Java app
from IBKR. The `ib-gateway` service runs the community image
[gnzsnz/ib-gateway](https://github.com/gnzsnz/ib-gateway-docker), which bundles IBC
(it types the login, restarts the gateway every night, and waits for your 2FA approval).

Safety by design:
- The gateway runs with `READ_ONLY_API=yes` and `app/ibkr.py` connects with
  `readonly=True`, so nothing can place an order.
- It has no published port. Only containers in this Compose project can reach it.
- It logs in to the **paper** account. The login lives in `ibkr.env`, which only the
  gateway container reads.

Turn it on (CT 104 needs about 1 GB more RAM, so give it 3 GB and 2 cores first):

```bash
cd /opt/claude-trading-skills/dashboard
cp ibkr.env.example ibkr.env && chmod 600 ibkr.env && nano ibkr.env   # paper username + password
# in .env: uncomment COMPOSE_PROFILES=ibkr and set IBKR_HOST=ib-gateway
docker compose up -d --build
docker compose logs -f ib-gateway        # approve the login in IBKR Mobile if asked
docker compose exec dashboard python -m app.ibkr SPY
```

The last command prints a quote and says whether it is `delayed` or `live`.

With `IBKR_HOST` set, the **Options** page uses IBKR first: type a ticker and the expiration becomes a list of real expirations, strikes are suggested from the chain, and a *Market vs model* table shows each leg's bid/ask/mid, IBKR's implied volatility and delta next to the Black-Scholes model. Blank premium and IV fields are filled from the market mid and IV. If the gateway isn't answering, the page falls back to Alpaca and says so. Delayed data is
free. `IBKR_MARKET_DATA=live` needs the paid subscriptions on your IBKR account.

Only one session per IBKR username can be logged in. Logging into the paper account
somewhere else (browser, TWS) kicks the gateway off until its next restart.

## Switching from paper to your real (live) account

The dashboard stays read-only either way. Live only changes which account it shows.

1. In Alpaca, switch to your live account and create a live API key pair. Save it in Vaultwarden.
2. On the CT, edit `.env`:
   ```bash
   cd /opt/claude-trading-skills/dashboard && nano .env
   ```
   Set `ALPACA_ENV=live` and replace `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` with the **live** pair.
   Paper keys don't work against the live API, and live keys don't work against paper.
3. Restart: `docker compose up -d`
4. Check the top of the page: a red **LIVE ACCOUNT** banner replaces the blue **PAPER ACCOUNT** one.

To go back, set `ALPACA_ENV=paper`, put the paper keys back, and restart. Any value other
than exactly `live` is treated as paper, so a typo can't point you at real money.

---

## Preview with made-up data (no keys needed)

```bash
cd dashboard
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/make_sample_data.py
mkdir -p sample-data/journal
REPORTS_DIR=sample-data/reports STATE_DIR=sample-data/state JOURNAL_DIR=sample-data/journal \
  flask --app 'app:create_app()' run
```

Open http://127.0.0.1:5000. Every number in `sample-data/` is invented.

## Tests

```bash
cd dashboard
pip install -r requirements-dev.txt
python -m pytest
```

## Troubleshooting

- **A tile says "stale":** that report is more than 30 hours old. Check `/var/log/trading-refresh.log` and run `docker compose run --rm refresh` by hand.
- **"Couldn't reach Alpaca":** check the keys and `ALPACA_ENV` in `.env`. Paper keys need `paper`, live keys need `live`.
- **Refresh fails with "Permission denied" on reports/:** run `chown -R 1000:1000 /opt/claude-trading-skills/reports`.
- **`https://trading.lab.silkytech.org` hangs:** the Caddy upstream must be plain `192.168.40.26:8080`, not `https://...` with `tls_insecure_skip_verify` (that's the Proxmox pattern). Test on CT 102 with `curl -sk --resolve trading.lab.silkytech.org:443:127.0.0.1 https://trading.lab.silkytech.org/healthz`.
- **The name doesn't load but `http://192.168.40.26:8080` does:** the Unbound override must point at Caddy (`192.168.40.22`), not at CT 104. Check with `resolvectl query trading.lab.silkytech.org` on the laptop, then `resolvectl flush-caches`.
- **Theses missing:** the dashboard reads every `th_*.yaml` under `state/`. Files that fail to parse are listed on the Theses page.
