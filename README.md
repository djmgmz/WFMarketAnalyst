# WF Market Analyst

Warframe is an online game with a busy player-run economy. Players sell each other weapons, mods and other gear for platinum, the game's premium currency, and most of that trading happens on a fan-made site called [warframe.market](https://warframe.market).

That site is great for finding someone to trade with right now. What it doesn't show is the bigger picture. Is this item getting cheaper? Did something just spike? Is this a good week to sell?

I built WF Market Analyst to answer those questions. It collects the market's public data over time and turns it into charts, rankings and a short-term price forecast, all in a dashboard that runs on your own computer. It's a portfolio project, recorded as a demo rather than hosted online.

## What you can do with it

- **See an item's price history.** How its price and trading activity changed over the last few months, side by side with what sellers are currently asking.
- **Spot the movers.** Which items rose or fell the most over the past week.
- **Find the best ducat deals.** Prime parts can be traded to an in-game merchant for ducats, a second currency. The dashboard ranks which parts give you the most ducats for the least platinum.
- **Keep a watchlist.** Follow the items you care about and note the price you'd be happy to pay.
- **Look ahead.** A forecast of tomorrow's price and next week's average for each item, with a likely range rather than a single number.
- **Run the data collection from the dashboard.** Downloading price history can take a while, so it runs in the background. You can start it, stop it, watch its progress, and pick up where it left off.

## About the forecast

Predicting prices is hard, and it's easy to make a forecast look smarter than it really is. So every forecast here is measured against the simplest guess there is: "the price won't change."

The model learns from past prices, then gets tested on two recent weeks it never saw while learning, the same way it would face the real future. In testing it has been clearly better than "no change" at predicting next week's average price. For groups of items where it isn't better, the dashboard quietly falls back to "no change" and labels it that way. The Forecast page shows the current results, including where the model is and isn't trusted.

Completed trades on warframe.market are self-reported, and some are faked (one item "sold" 199 times at 99,999p in a day). The dashboard spots days like that and leaves them out of prices, rankings and forecasts, saying on the item's page which days it left out. Items that rarely trade aren't ranked at all, because a single sale can move them.

It's a demonstration, not trading advice. Game updates and in-game events can move prices in ways no chart can see coming.

## Data source and responsible use

All data comes from the public warframe.market API, and the project follows its usage rules. Requests are rate-limited to stay below the published limit of three per second, every request identifies the application, and downloaded data is stored locally so it is never requested twice unnecessarily.

Item images come from the Warframe community's open item database ([WFCD warframe-items](https://github.com/WFCD/warframe-items)). They are downloaded once and kept on your computer; they are not part of this repository.

This project is not affiliated with warframe.market or with Digital Extremes, the developer of Warframe. The colour scheme follows warframe.market's for familiarity; the name, logo and layout are original.

## How it's put together

There are two halves. A Python program collects the data and keeps it in a small database file on your computer. A website shows it: the charts, tables and forecasts. Nothing is sent anywhere except the requests to warframe.market itself.

Built with Python, FastAPI and SQLite for the data side, LightGBM for the forecast, and Angular for the website.

## Running it yourself

You'll need Python 3.12 and Node.js installed. Then open two terminals.

In the first one, set up the data side and start it:

```sh
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m wfm sync-catalog    # downloads the list of tradable items (no prices yet)
.venv/Scripts/python -m wfm serve           # keeps running while you use the dashboard
```

On macOS or Linux, use `.venv/bin/python` instead of `.venv/Scripts/python`.

In the second one, start the website:

```sh
cd frontend
npm install
npm start
```

Then open http://localhost:4200.

The first time, the dashboard is mostly empty because there's no price history yet. On the Overview page, start **Price history backfill** for a category (Arcane is a quick one to begin with), then **Price forecast** once it's done. **Item images** fetches a picture for nearly every item (about 70 MB, a few minutes). Running the backfill again each day keeps the history growing.

If you share your copy, set the `WFM_CONTACT` environment variable to your email or repository link, so the warframe.market team can see who is asking for data.

Both halves have automated tests: `pytest` in `backend` and `npx ng test` in `frontend`.
