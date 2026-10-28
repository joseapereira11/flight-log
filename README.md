# PS-SRN flight tracker

This tracks **PS-SRN**, a Gulfstream G550 with ICAO hex `E4A0C5`. It sends a Telegram message every time the plane takes off or lands, with the departure and arrival airports. Every completed flight is logged to `flights.csv`.

The tracker runs free on GitHub Actions every ~5 minutes. Its data comes from community ADS-B networks: adsb.fi, adsb.lol and airplanes.live. Unlike FlightAware and Flightradar24, these don't hide private jets.

## Setup (about 10 minutes)

### 1. Create the Telegram bot
1. In Telegram, open **@BotFather** and send `/newbot`. Pick any name. BotFather replies with a **token** like `123456:ABC-DEF…`.
2. Open your new bot and send it any message, e.g. "hi".
3. In a browser, open `https://api.telegram.org/bot<TOKEN>/getUpdates`, putting your token in place of `<TOKEN>`. Find `"chat":{"id":123456789` in the reply. That number is your **chat ID**.

### 2. Create the GitHub repository
1. Go to github.com → **New repository**. Give it any name, e.g. `flight-log`, and make it **Public**. Public repos get unlimited free Actions minutes; see the note below.
2. Click **uploading an existing file** and drag in `tracker.py`, `airports.csv` and `README.md`. Commit.
3. Click **Add file → Create new file**. Type the name `.github/workflows/track.yml`; the slashes create the folders. Paste in the contents of `track.yml` and commit.

### 3. Add the secrets
Open the repo's **Settings → Secrets and variables → Actions → New repository secret** and add two secrets:
- `TELEGRAM_BOT_TOKEN`: the BotFather token
- `TELEGRAM_CHAT_ID`: the chat ID number

### 4. Turn it on and test
1. Open the **Actions** tab and enable workflows if GitHub asks.
2. Click **Track aircraft → Run workflow**, tick **Send a Telegram test message**, and run it.
3. You should get "✅ Tracker for PS-SRN is working" in Telegram. From then on it runs by itself.

## What the alerts look like
```
🛫 PS-SRN took off as PSSRN
From: SBSP/CGH – Congonhas–Deputado Freitas Nobre Airport, São Paulo (BR)
Time: 21/09 11:33 (Sao Paulo) · 14:33Z

🛬 PS-SRN landed
At: SBRJ/SDU – Santos Dumont Airport, Rio de Janeiro (BR)
From: SBSP/CGH … Flight time: 0h40
```

## How it decides
- **Takeoff:** the plane appears airborne when its last state was on the ground or unknown. The departure airport is the nearest airport to where it was last parked. If the tracker first picks the plane up already in flight, the alert says the departure is unknown.
- **Landing:** the plane is reported on the ground, which gives the nearest airport. Many smaller airfields have no ground coverage, so the tracker also counts a landing when the signal disappears below 6,000 ft for 25+ minutes. In that case it uses the airport nearest the last position, and the alert says it is an estimate.
- **Ocean crossings / no coverage at altitude:** the plane stays "in flight" and the tracker waits. When the plane reappears you get a "back in coverage" notice, and the landing is reported normally. After 18 h of silence it gives up on that leg and alerts on the next sighting.

## Notes
- **Private repo?** Each run uses about 1 billed minute. Every 5 minutes comes to about 8,600 min/month, and private repos only get 2,000 free. If you want the repo private, change the cron in `track.yml` to `"*/30 * * * *"`.
- **Coverage:** the tracker depends on volunteer ADS-B receivers. Coverage is good around São Paulo, Rio and most of Brazil's south and southeast, Europe and the US. It is patchy in the Amazon/interior and over oceans.
- GitHub can delay scheduled runs by a few minutes at busy times, so expect alerts within roughly 5–15 minutes of the event.
- GitHub pauses scheduled workflows in repos with no activity for 60 days. The tracker writes a weekly heartbeat to `state.json` to stay active.
- **Tracking a different plane:** add `TRACK_HEX` (the ICAO hex code) and `TRACK_REG` under `env:` in the workflow, or run a separate copy of the repo.
