# Gold & Forex Scalper (Deriv)

Two ways to run the same strategy (same signals, same risk rules):

- **Web app (Vercel)**: `index.html` + `strategy.js` + `api/`. Runs in your browser tab. Deploy this repo on Vercel with the "Other" preset and no build command.
- **Python (self-host)**: `python/deriv_bot.py`. Run on a computer or server: `cd python`, `pip install -r requirements.txt`, then `python deriv_bot.py live --symbols frxXAUUSD`.

Never put your token in this repo. The web app asks for it on the page and keeps it in memory only.

Start on a demo account. Educational software, not financial advice.
