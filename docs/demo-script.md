# Kraken demo script (about 3:00)

Setup before recording: Kraken running, shopfront running with `SHOPFRONT_CHAOS=1`, stage agents created, Discord relay running, `kraken-smoke` agent registered and added to the demo rooms (for the rehearsal run), screen split between Discord (`#backend`, `#mobile`, `#product`, `#partners`, `#kraken-requests`) and Switch Console. Keep `autonomy: human` for all gates so every approval is visible.

Messages come from `demo/seed_chat.md`.

| Time | Shot | Say / do | On screen |
|---|---|---|---|
| 0:00-0:15 | Title | "Teams decide things in many channels, and the decisions quietly contradict each other. Release the Kraken on your codebase." | Title card, pipeline diagram from the README |
| 0:15-0:35 | Seed `#backend` | Post: `The user_id is a UUID string everywhere in the shopfront API now.` | Message in `#backend`; Kraken connected in Switch Console |
| 0:35-0:50 | Seed `#mobile` | Post only: `The mobile app stores user_id as an int in the local database.` (skip the second `#mobile` line in `seed_chat.md`; it would raise a second, duplicate room) | Message in `#mobile` |
| 0:50-1:10 | Conflict caught | "Nobody asked Kraken. It noticed." | A new Sonar resolution room appears with you and Kraken in it. Its text names both speakers and quotes both messages with their channels (`#backend`, `#mobile`), then asks "Which one is true?" |
| 1:10-1:25 | Resolve | React 1️⃣ on the prompt (relay), or reply `1️⃣` in the thread | Receipt line in the thread; the decision is recorded and becomes the brief |
| 1:25-1:50 | Charter bids | "Three agents from different vendors bid in one room." | Charter room with `BID:` posts from each bidder (enlist Codex / Gemini / Kimi agents beforehand for the cross-vendor shot; `@kraken crew` shows the vendors) |
| 1:50-2:00 | Pick | React 1️⃣ on the `[K-n]` pick prompt | Winner announced; Forge room opens with the losers invited as reviewers |
| 2:00-2:15 | Forge | "Reviewers push back until the acceptance criteria pass." | Reviewer posts, then `ACCEPTED` on its own line; react 👍 to approve the merge |
| 2:15-2:40 | Storm | Storm opens in auto mode (the default, `storm.default_mode`); nothing to pick. "Squall picks faults aimed at the change; the defender patches." | Squall runs faults against shopfront (kill, latency, bad payload and so on); defender diagnoses; coach scorecard ending `SURVIVED`; react 👍 |
| 2:40-2:55 | Pull request | "Nothing leaves the chat without a person." | Storm room asks `[K-n] Storm survived. Open a pull request?`; react 👍; the coder posts `PR: <url>`; Kraken posts the link in `#kraken-requests`; open the PR on GitHub (brief, reviews, Storm scorecard) |
| 2:55-3:05 | Hallmark | Open the Hallmark room document | The `Hallmark` document listing the settled decision with who, where, when and source link |
| 3:05-3:10 | Close | "Switch rooms, streams, templates and runs, in one crew." | Switch run lineage view if time allows, then the repo URL |

## Any codebase (strong extra beat)

Show it isn't tied to the demo: in `#kraken-requests` post `@kraken connect https://github.com/<you>/<a small API you forked>`. Kraken replies with what it detected (language, test and run commands, endpoints). Then `@kraken build <name>: <small change>` and cut to the Charter bids naming that repo's real files. Fork the repo first so the crew can push and open the PR.

## Optional extra beats (cut if over time)

- `#product`: post `The free tier rate limit is 100 requests per day for each customer.` then `Marketing copy on the pricing page says free users get 100 calls daily.` Sonar flags the chat-vs-code mismatch against `limits.yaml`.
- `#backend` and `#partners`: `Infra says we are shutting down the v1 users endpoint this Friday.` and `Please make sure every client switches over to the v2 endpoint before then.` against `We promised Acme that the v1 endpoint stays available until March.` and `Acme said their integration cannot migrate to v2 before next spring.`

## Recording tips

- Rehearse once with `python scripts/smoke.py` to confirm a resolution room appears, then reset before recording: stop Kraken, delete `kraken.db`, optionally delete the `Hallmark` document in `#kraken-hallmark` (harmless; Kraken now finds and reuses it), and start Kraken again. The channels need no clearing: a fresh `kraken.db` starts the stream at the head, so the rehearsal messages are not read again.
- If a reaction does not register, reply with the emoji in the thread; it behaves the same.
- Storm starts in Drill (you defend). For the video, reply 🤖 in the Storm room to hand defending to `storm-defender`; it also shows the human-first handoff on camera.
