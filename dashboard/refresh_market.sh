#!/bin/sh
# Writes fresh market reports into reports/ for the dashboard.
# Runs inside the dashboard image (see the "refresh" service in compose.yaml).
# Breadth, uptrend and exposure need no API key. VCP runs when RUN_VCP=true;
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

echo "== market breadth"
python skills/market-breadth-analyzer/scripts/market_breadth_analyzer.py --output-dir reports || fail breadth

echo "== uptrend ratio"
python skills/uptrend-analyzer/scripts/uptrend_analyzer.py --output-dir reports || fail uptrend

newest() { ls -1t reports/"$1"*.json 2>/dev/null | head -n 1; }
BREADTH=$(newest market_breadth_)
UPTREND=$(newest uptrend_analysis_)

echo "== exposure posture"
if [ -n "$BREADTH" ] && [ -n "$UPTREND" ]; then
  python skills/exposure-coach/scripts/calculate_exposure.py \
    --breadth "$BREADTH" --uptrend "$UPTREND" --output-dir reports || fail exposure
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
