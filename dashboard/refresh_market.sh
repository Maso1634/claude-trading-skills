#!/bin/sh
# Writes fresh market reports into reports/ for the dashboard.
# Runs inside the dashboard image (see the "refresh" service in compose.yaml).
# Breadth, uptrend, sector and theme need no API key; regime, top risk and FTD
# use the FMP key with yfinance where FMP's free plan says no. Their reports
# feed the exposure posture. VCP runs when RUN_VCP=true;
# VCP_DATA_SOURCE picks its prices (see below). The drawdown circuit breaker
# runs when CB_ACCOUNT_SIZE is set. At the end, notify_refresh.py sends an ntfy
# push (NTFY_URL) if a step failed or the VCP data needs a look.
#
#   refresh_market.sh           everything (weekday 4:20 PM cron)
#   refresh_market.sh circuit   only the circuit breaker (weekday morning cron)
set -u
cd /repo || exit 1
mkdir -p reports
status=0
failed=""

fail() {
  status=1
  failed="$failed $1"
}

notify() {
  python /app/notify_refresh.py --failed "$failed" --vcp-report "${1:-}" || true
}

run_circuit() {
  if [ -z "${CB_ACCOUNT_SIZE:-}" ]; then
    echo "== circuit breaker: skipped (set CB_ACCOUNT_SIZE in .env)"
    return 0
  fi
  echo "== circuit breaker"
  set -- --state-dir state/theses --account-size "$CB_ACCOUNT_SIZE" --output-dir reports
  [ -n "${CB_MAX_DAILY_LOSS_PCT:-}" ] && set -- "$@" --max-daily-loss-pct "$CB_MAX_DAILY_LOSS_PCT"
  [ -n "${CB_LOSING_STREAK_N:-}" ] && set -- "$@" --losing-streak-n "$CB_LOSING_STREAK_N"
  [ -n "${CB_COOLDOWN_HOURS:-}" ] && set -- "$@" --cooldown-hours "$CB_COOLDOWN_HOURS"
  [ -n "${CB_WEEKLY_DRAWDOWN_PCT:-}" ] && set -- "$@" --weekly-drawdown-pct "$CB_WEEKLY_DRAWDOWN_PCT"
  [ -n "${CB_MONTHLY_DRAWDOWN_PCT:-}" ] && set -- "$@" --monthly-drawdown-pct "$CB_MONTHLY_DRAWDOWN_PCT"
  python skills/drawdown-circuit-breaker/scripts/check_circuit_breaker.py "$@" || return 1
}

if [ "${1:-}" = "circuit" ]; then
  run_circuit || fail circuit-breaker
  notify
  exit $status
fi

newest() { ls -1t reports/"$1"*.json 2>/dev/null | head -n 1; }

# run_report VAR NAME PREFIX CMD...: runs CMD, then sets VAR to the JSON
# report it wrote (the newest reports/PREFIX*.json, only if this run created
# it). A failed step or an old leftover report leaves VAR empty, so Exposure
# Coach treats that input as missing instead of scoring yesterday's data.
run_report() {
  var=$1 name=$2 prefix=$3
  shift 3
  eval "$var=''"
  echo "== $name"
  before=$(newest "$prefix")
  "$@" || { fail "$name"; return 0; }
  after=$(newest "$prefix")
  if [ -n "$after" ] && [ "$after" != "$before" ]; then
    eval "$var=\$after"
  else
    echo "no new report written" >&2
    fail "$name"
  fi
}

run_report BREADTH breadth market_breadth_ \
  python skills/market-breadth-analyzer/scripts/market_breadth_analyzer.py --output-dir reports
run_report UPTREND uptrend uptrend_analysis_ \
  python skills/uptrend-analyzer/scripts/uptrend_analyzer.py --output-dir reports
# Macro regime: ETF ratios + Treasury yields (FMP if allowed, else yfinance).
run_report REGIME regime macro_regime_ \
  python skills/macro-regime-detector/scripts/macro_regime_detector.py --output-dir reports
# Market top risk: distribution days, leaders, 200DMA breadth (FMP + yfinance).
run_report TOP_RISK top-risk market_top_ \
  python skills/market-top-detector/scripts/market_top_detector.py --output-dir reports
# Follow-Through Day state for the S&P 500 and Nasdaq Composite.
run_report FTD ftd ftd_detector_ \
  python skills/ftd-detector/scripts/ftd_detector.py --output-dir reports
# Sector rotation from TraderMonty's public CSV (no key). The JSON also goes
# to stdout, so drop that copy.
run_report SECTOR sector sector_rotation_ \
  sh -c 'python skills/sector-analyst/scripts/analyze_sector_rotation.py --json --save --output-dir reports >/dev/null'
# Themes from yfinance ETF data + public FINVIZ industry groups (no key).
# "theme_detector_2" skips theme_detector_history.json in the same folder.
run_report THEME theme theme_detector_2 \
  python skills/theme-detector/scripts/theme_detector.py --output-dir reports

echo "== exposure posture"
# Institutional flow is left out on purpose: no free source, and 13F data is
# a quarter old. Exposure Coach rescales the weights over what it gets.
if [ -n "$BREADTH" ] && [ -n "$UPTREND" ]; then
  set -- --breadth "$BREADTH" --uptrend "$UPTREND" --output-dir reports
  [ -n "$REGIME" ] && set -- "$@" --regime "$REGIME"
  [ -n "$TOP_RISK" ] && set -- "$@" --top-risk "$TOP_RISK"
  [ -n "$FTD" ] && set -- "$@" --ftd "$FTD"
  [ -n "$SECTOR" ] && set -- "$@" --sector "$SECTOR"
  [ -n "$THEME" ] && set -- "$@" --theme "$THEME"
  python skills/exposure-coach/scripts/calculate_exposure.py "$@" || fail exposure
else
  echo "skipped: breadth or uptrend report missing" >&2
  fail exposure
fi

VCP_REPORT=""
if [ "${RUN_VCP:-false}" = "true" ]; then
  echo "== VCP screener"
  # VCP_DATA_SOURCE:
  #   auto  Alpaca SIP daily bars (ALPACA_API_KEY/SECRET), then yfinance, then
  #         FMP; the shown results are cross-checked against a second source.
  #   fmp   FMP quotes (default, for old .env files). A free FMP key allows 250
  #         calls a day and no batch quotes, so set VCP_UNIVERSE to a watchlist
  #         of about 100 symbols, or the full S&P 500 won't fit.
  BEFORE=$(newest vcp_screener_)
  set -- --output-dir reports --data-source "${VCP_DATA_SOURCE:-fmp}" \
    --max-api-calls "${VCP_MAX_API_CALLS:-240}"
  [ -n "${VCP_MAX_CANDIDATES:-}" ] && set -- "$@" --max-candidates "$VCP_MAX_CANDIDATES"
  # shellcheck disable=SC2086  # word-split the watchlist into symbols on purpose
  [ -n "${VCP_UNIVERSE:-}" ] && set -- "$@" --universe $VCP_UNIVERSE
  python skills/vcp-screener/scripts/screen_vcp.py "$@" || fail vcp
  AFTER=$(newest vcp_screener_)
  [ -n "$AFTER" ] && [ "$AFTER" != "$BEFORE" ] && VCP_REPORT="$AFTER"
fi

run_circuit || fail circuit-breaker

notify "$VCP_REPORT"
exit $status
