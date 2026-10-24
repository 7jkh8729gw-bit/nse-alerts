# NSE Breakout Alerts: free, real-time, whole-market

**Flow:** 08:10 IST scan of all NSE stocks → ranked watchlist to Telegram → 09:15–15:30 live websocket
monitor → buy alert (entry zone, stop, T1/T2) → follow-up alerts when T1/SL hit → end-of-day summary.

## 1. Accounts (all free)
1. **Upstox demat account** (free, paperless KYC). Upstox's trading **and data** APIs are free, and the live
   feed allows up to 2,000 stocks.
2. **Analytics Token** (read-only, valid 1 year, no daily login): log in at https://upstox.com/developer/apps →
   create an app if asked (any name; redirect URL can be `https://127.0.0.1`) → open the **Analytics** tab →
   **Generate Token** → copy it. It cannot place orders, so it is safe for this system. Put a reminder in your
   calendar for ~11 months; the bot also warns you on Telegram 14 days before expiry.
3. **Telegram bot**: you already have `BOT_TOKEN` and `CHAT_ID`.
4. **A machine that stays on** (Oracle Cloud Always Free VM in Mumbai, an old laptop/Raspberry Pi, or the GitHub
   Actions fallback in section 5).

*Prefer Angel One?* Set `BROKER=angel` and fill the `ANGEL_*` variables (see `.env.example`).

## 2. Install
```bash
git clone <your repo> && cd nse_alerts_project      # or unzip
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env                    # fill secrets (never commit .env)
set -a; source .env; set +a
python -m nse_alerts.selftest                        # step-by-step check (run once in market hours too)
python -m nse_alerts.premarket --seed-days 700       # one-time: ~2.5 years of NSE history (~5 min)
```
`selftest` shows a parsed live tick; price, volume and VWAP must all be non-zero (run it again during market hours).

## 3. Run
```bash
sudo cp deploy/nse-alerts.service /etc/systemd/system/   # edit User/paths first
sudo systemctl enable --now nse-alerts
journalctl -u nse-alerts -f                              # live logs
```
One process does everything and restarts itself. You get a "monitor started" message at 09:00; silence after
that means "nothing qualified", and a stalled feed or crash is reported to Telegram.

## 4. Validate before trusting it
```bash
python -m nse_alerts.backtest --sessions 150    # does the ranking beat 'all liquid stocks' next day?
python -m nse_alerts.calibrate                  # replaces my default intraday-volume curve with real NSE data
```
Everything is logged to `data/alerts_log.csv` (alert, T1, SL, T2, EOD with R-multiples). After ~100+ alerts
review it; tune thresholds in `config.py` (every value is an env var) only with that evidence.

## 5. No-server fallback (GitHub Actions)
Add the secrets (BOT_TOKEN, CHAT_ID, UPSTOX_ANALYTICS_TOKEN) to the repo, keep only `.github/workflows/session.yml`
(delete your old workflows). It scans, monitors, and re-dispatches itself before GitHub's 6-hour cap.
Caveats: GitHub may start the job late, and the live feed from GitHub's IPs is untested; if it fails, use a VPS/PC.

## Why these criteria (evidence from your 43 alerts, Sep 2026)
Result: 30% wins, 44% stopped out, only 14% reached a target, **average −0.31R**.

| Finding in your data | Cause | Change |
|---|---|---|
| Alerts after 13:00 averaged −0.58R vs ≈0R before (22 vs 21 alerts) | late breakouts have no time to follow through; 12-bar volume baseline makes normal afternoon bars look like spikes | no new alerts after 14:00; 13:00–14:00 needs 1.25× volume |
| Highest scores did *worst* (score ≥90: −0.48R; vol ratio >4×: −0.56R) | extreme volume/RSI = chasing exhaustion; score wasn't predictive | removed the score as a gate; require a **tight entry zone** above the level instead of a 6–8% extension cap |
| Stops averaged 4.7% away (1.2× *daily* ATR) while T1 was 1.5× ATR | stop too wide for an intraday trigger, reward barely above risk | stop just under the breakout level (≈1.5–3%); T1 = 1.5R, T2 = 3R; stop → entry after T1 |
| "Volume ratio" = last 5-min bar vs previous 12 bars | ignores NSE's U-shaped volume | RVOL = cumulative volume ÷ volume expected **by this minute of the day** |
| Signals evaluated on delayed Yahoo 5-min bars via 5-min cron | latency + partial candles | live websocket ticks → 1-minute bars; fire on a closed bar |
| Liquidity = ≥500k shares & price ≥₹100 | excludes ₹9,000 stocks trading ₹100+ Cr/day; includes thin cheap names | liquidity = 20-day **median traded value ≥ ₹5 Cr** |
| Self-learning model retrained on 43 trades | ~18 features on 43 samples = noise | removed; features/outcomes are logged so a model can be trained at 300+ alerts |
| Candlestick-pattern detectors | subjective, untestable | replaced by objective rules: trend gate, 20-day-high pivot, volatility contraction, accumulation volume, delivery %, relative-strength rank |
| BE-series stocks included | trade-for-trade: cannot be sold intraday | EQ series only |

**Honest caveats:** 43 alerts over 14 days is a small, single-regime sample, so the differences above are
suggestive, not proven (win rate ±7%). Score weights and thresholds are well-established priors, **not**
fitted values; I could not backtest the intraday rules because that needs your broker's historical data.
Treat every alert as an idea to review, not an order. Not financial advice.
