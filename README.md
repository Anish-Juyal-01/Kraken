# Kraken

**Release the Kraken on your codebase.**

Kraken is an AI crew that lives in your team chat through [Switch](https://docs.switchagents.ai). It listens to every channel it joins, catches decisions that contradict each other (`#backend` says `user_id` is a UUID string, `#mobile` parses it as an int) or drift from the code (`#product` says 100 requests a day, `limits.yaml` says 1000), and opens a room where the two sides settle it. The settled decision becomes a brief, and Kraken runs it through bidding, build and a chaos test, leaving a decision ledger behind.

Built for the Flip the Switch hackathon (WeMakeDevs, Nov 4-7 2026), Track 01: Best Business Workflow.

## The pipeline

```
#kraken-requests ─► SPARK ─► SONAR pre-flight ─► CHARTER ─► FORGE ─► STORM ─► close
                     intake     conflict check     bidding    build    chaos test
                                     ▲                                       │
                     SONAR (always listening across all rooms)      HALLMARK (decision ledger)
```

| Stage | What it does |
|---|---|
| **Sonar** | Always-on watcher. Extracts decision claims from chat, detects contradictions (chat vs chat, chat vs code) and pre-flight checks briefs. |
| **Spark** | Intake. A planner agent asks clarifying questions and writes a brief with acceptance criteria. |
| **Charter** | Bidding room. Several coding agents bid, one is picked, the losers become reviewers. |
| **Forge** | Build room. The winner implements; reviewers push back until they post `ACCEPTED`. |
| **Storm** | Chaos test of that change. **Auto** (attacker and defender agents), **Drill** (humans defend) or **Skip**. |
| **Hallmark** | Decision ledger (SQLite), mirrored into a Switch room document. |

Two entry points: a person asks for work in `#kraken-requests`, or Sonar finds a conflict on its own, opens a resolution room, and the answer goes straight to Charter.

## Why Switch is central

Kraken cannot exist without Switch.

- **Rooms across apps.** Humans and agents share one room per stage.
- **`scope=all` event stream.** Sonar hears every room it belongs to over one connection.
- **Templates and `run_template`.** Each stage is a room template; Kraken creates rooms on demand.
- **Runs.** Every room made for one request is recorded in one Switch run, so there is a full lineage trail.
- **Room documents.** Hallmark is mirrored as a room document; briefs live as documents in their stage rooms.
- **Bring-any-agent.** Charter's bidders are different vendors' agents in the same room.

Transitions between stages live in code (a deterministic state machine), not in LLM instructions. Agents signal outcomes with fixed markers on their own line (`BID:`, `ACCEPTED`, `SURVIVED`, `FAILED`) and Kraken parses only those.

## Setup

Requirements: Python 3.12 or newer, Switch Console (with Docker for the local Switch server), a Discord server you control, an Anthropic API key. Follow the steps in order.

1. **Install Switch Console and connect Discord.** Follow the install guide, <https://docs.switchagents.ai/switch-rooms/getting-started> (installing Switch Console and connecting an agent), and see the docs root, <https://docs.switchagents.ai>, for the rest. Do this first; later steps need a running Switch server with a Discord bridge to your server.
2. **Clone and install.**
   ```bash
   git clone <this repo> kraken && cd kraken
   python -m venv .venv
   source .venv/bin/activate        # Windows Git Bash: source .venv/Scripts/activate
   pip install -e ".[dev]"
   ```
3. **Register Kraken and fill in `.env`.** Copy `.env.example` to `.env` and set `SWITCH_URL`, `SWITCH_REGISTRATION_TOKEN` and `ANTHROPIC_API_KEY`. Then register:
   ```bash
   python scripts/register.py
   ```
   It reads `.env` and prints `SWITCH_AGENT_ID=...` and `SWITCH_API_KEY=...`; paste both into `.env`. If registration is rejected, the script prints Switch's error.
4. **Create the crew in one click.** In Switch Console's Templates page, run `templates/kraken-crew.template.yaml`. It creates `kraken-planner`, three bidders (`claude-coder`, `claude-coder-2`, `claude-coder-3`), `squall`, `storm-defender` and `storm-coach`, each on the first installed coding agent, with `addressing: anyone` (Kraken wakes them by @-mention), auto mode, and its own clone of this repo (the `repo` param). The names match `bidders` and `agents` in `kraken.yaml`, which seed the crew the first time Kraken starts. To bring in other vendors (Codex, Gemini, Kimi, Qwen), create those agents in Switch Console and `@kraken enlist` them (see [Crew](#crew)).
5. **Create the HQ rooms.** In Switch Console, create the rooms from `templates/kraken-hq.template.yaml`. That makes `#kraken-requests` and `#kraken-hallmark` with Kraken in both.
6. **Create the demo channels.** Register the smoke-test agent first (`python scripts/register.py --name kraken-smoke`, see step 10), then run `templates/kraken-demo.template.yaml` in Switch Console. It creates `#backend`, `#mobile`, `#product` and `#partners` with Kraken and `kraken-smoke` in each. For your own team, add Kraken to whichever channels it should watch instead.
7. **Start shopfront** (the demo app Storm attacks) with chaos controls on:
   ```bash
   SHOPFRONT_CHAOS=1 python demo/shopfront/run.py      # API on :8080, email stub on :8081
   ```
   PowerShell: `$env:SHOPFRONT_CHAOS = "1"; python demo/shopfront/run.py`
8. **Run Kraken** from the repo root (it reads `kraken.yaml` and `.env` from the current directory):
   ```bash
   python -m kraken.main
   ```
   It indexes `sonar.code_paths`, connects to Switch and starts listening.
9. **Optional: Discord reaction relay.** Switch does not forward reactions people add, so this small bot does. Set `DISCORD_RELAY_TOKEN` (a Discord bot token for a bot in your server, with the message-content intent enabled) and `KRAKEN_RELAY_SECRET` (any shared secret) in `.env`. Restart Kraken (it opens the local relay endpoint when the secret is set), then:
   ```bash
   python -m kraken.relay.discord_relay
   ```
   Without it, replying to a prompt with just an emoji works in every chat app.
10. **Verify.**
    ```bash
    pytest                                  # offline unit suite
    python tests/golden/run_golden.py       # real-LLM precision check, needs ANTHROPIC_API_KEY
    python scripts/smoke.py                 # live end-to-end smoke
    ```
    The smoke test needs Kraken running (step 8). It posts as its own agent, because Kraken ignores its own messages: register one with `python scripts/register.py --name kraken-smoke` (for any name other than `kraken` it prints `SMOKE_AGENT_ID=...` and `SMOKE_API_KEY=...`), paste both lines into `.env`, and add the agent to the demo rooms (step 6). It posts `demo/seed_chat.md` and passes when a resolution room appears. Exit codes: 0 OK, 1 timeout (no resolution room within 120 s), 2 setup missing (env values or a demo room).

### Resetting before a demo

A rehearsal leaves claims, pairs and requests in `kraken.db`. To start clean: stop Kraken, delete `kraken.db`, and start Kraken again. You may also delete the `Hallmark` document in `#kraken-hallmark`; that is harmless but not needed, because Kraken finds the existing document by name and reuses it. The channels need no clearing: with a fresh `kraken.db` Kraken reads the stream from its head, so old messages are not judged again. The full recording run is in `docs/demo-script.md`.

### Environment variables

| Variable | Used by |
|---|---|
| `SWITCH_URL`, `SWITCH_AGENT_ID`, `SWITCH_API_KEY` | Kraken core (step 3) |
| `ANTHROPIC_API_KEY` | Sonar, the judge, the golden run |
| `DISCORD_RELAY_TOKEN`, `KRAKEN_RELAY_SECRET` | Optional relay (step 9) |
| `SWITCH_REGISTRATION_TOKEN` | `scripts/register.py` only (step 3) |
| `SMOKE_AGENT_ID`, `SMOKE_API_KEY` | `scripts/smoke.py` only (step 10) |
| `SHOPFRONT_CHAOS=1` | Enables shopfront's fault endpoint |

## Using it

Ask for work in `#kraken-requests`: `@kraken build password reset`. Every prompt Kraken posts carries a tag like `[K-12]` and lists the replies it accepts. Reply in the thread with a typed command or just the emoji. With the relay running, react on the prompt message instead.

### Commands

| Command | Emoji | Effect |
|---|---|---|
| `approve` | 👍 ✅ ✔ | Approve the pending gate (brief, merge, storm result) |
| `reject [reason]` | 👎 ❌ (or `👎 reason`) | Reject, with an optional reason |
| `pick <agent>` | 1️⃣ 2️⃣ 3️⃣ | Pick a bidder by name, or the 1st/2nd/3rd bid. In a resolution room, picks the side that is true |
| `storm auto` | 🌪️ | Run Storm with agent attacker and defender |
| `storm drill` | 🧪 | Run Storm with humans defending |
| `storm skip` | ⏭️ | Skip Storm |
| `not a conflict` | 🙅 | Dismiss a Sonar conflict; the pair is remembered |
| `retry` | none | Retry a stage whose template run failed |
| `crew` | none | List the roster with each agent's vendor and whether it is online (requests room only) |
| `enlist <name> [as <role>]` | none | Add a Switch agent to the crew; role defaults to `bidder` (requests room only) |
| `retire <name>` | none | Remove an agent from the crew (requests room only) |

A leading `@kraken` and a trailing `.` or `!` are ignored.

## Crew

The crew is Kraken's roster of agents: the Charter bidders and the single-seat roles (`planner`, `attacker`, `defender`, `coach`). It lives in Kraken's database, not in `kraken.yaml`, and people manage it from `#kraken-requests`:

```
@kraken crew
@kraken enlist kimi-coder
@kraken enlist qwen-coder as bidder
@kraken retire opencode-coder
```

- `crew` prints one line per role. Each agent shows its vendor and whether it is online (`●`) or offline (`○`), e.g. `bidder: claude-coder (claude-code) ● · kimi-coder (opencode · moonshot/kimi-k2) ○`. Vendor and liveness come from Switch (`known_agent_type`, the agent's first listed model, and its sessions).
- `enlist <name>` checks that Switch has an agent with that exact name. An agent holds one role, so enlisting it under a new role moves it. Roles: `bidder`, `planner`, `attacker`, `defender`, `coach`.
- `retire <name>` takes the agent off the crew.
- The commands work only in the requests room (`rooms.requests`), and messages from crew agents are ignored, so agents cannot reshape the crew.
- For the single-seat roles Kraken uses the first member listed.
- `kraken.yaml`'s `bidders` and `agents` are only the initial seed: Kraken copies them into the database once, on first start. Editing them afterwards changes nothing; use the commands, or delete `kraken.db` (which also clears claims and requests) to reseed.
- A request already past Charter keeps the bidders it chose. Roster changes apply from the next Charter.

### Vendors

| Vendor | Shown as | How it joins |
|---|---|---|
| Claude | `claude-code` | Claude Code agent |
| GPT | `codex` | Codex agent |
| Gemini | the agent's Switch type | Antigravity agent |
| Kimi, Qwen, DeepSeek and others | `opencode · <model>` | OpenCode agent; the model name tells the vendors apart |
| Anything else | `custom` | Any agent that speaks the Switch protocol |

### Choosing bidders

At Charter, Kraken asks the bidders that are online. If more than `charter.max_bidders` (default 5) are online it seats a subset, preferring different vendors. It takes one agent per vendor first, then fills any remaining seats in order. The scan start rotates by one at every Charter, so the same agents do not always win the seats. OpenCode agents count as different vendors when their first listed model differs. With more than nine bids the pick prompt adds `…or reply pick <name>`, since only nine keycaps exist.

## Configuration (`kraken.yaml`)

| Key | Default | Meaning |
|---|---|---|
| `sonar.threshold` | `0.75` | Minimum confidence to raise a conflict |
| `sonar.code_paths` | `[]` (`[demo/shopfront]` in the repo file) | Paths indexed for chat-vs-code checks |
| `sonar.min_words` | `6` | Messages shorter than this are ignored |
| `storm.max_rounds` | `3` | Storm-to-Forge loops before a human is pinged |
| `storm.default_mode` | `auto` | `auto`, `drill` or `skip` |
| `autonomy.brief` | `human` | `human` (reply `approve`) or `auto` |
| `autonomy.pick` | `human` | `human` (reply `pick`) or `auto` (LLM judge picks the best bid) |
| `autonomy.merge` | `human` | Gate after reviewers post `ACCEPTED` |
| `autonomy.storm_result` | `human` | Gate after the coach posts `SURVIVED` |
| `rooms.requests` | `kraken-requests` | Name of the requests room |
| `rooms.hallmark` | `kraken-hallmark` | Name of the Hallmark room |
| `charter.max_bidders` | `5` | Most bidders seated in one Charter (at least 1); vendor-diverse choice when more are online |
| `bidders` | `[]` (`[claude-coder, claude-coder-2, claude-coder-3]` in the repo file) | Initial seed for the crew's bidders (see [Crew](#crew)) |
| `kraken_agent` | `kraken` | Kraken's agent name |
| `agents.planner` | `kraken-planner` | Initial seed for the planner |
| `agents.attacker` | `squall` | Initial seed for the attacker |
| `agents.defender` | `storm-defender` | Initial seed for the defender |
| `agents.coach` | `storm-coach` | Initial seed for the coach |
| `timeouts_s.spark` / `charter` / `forge` / `storm` | `1800` / `900` / `3600` / `1800` | Per-stage timeout in seconds; on expiry Kraken pings the owner |
| `db_path` | `kraken.db` | SQLite file for claims, decisions and request state |
| `relay_port` | `8765` | Local port for the reaction relay endpoint |

## Repo map

- `kraken/` core service: `sonar/`, `hallmark/`, `conductor/`, `relay/`, `crew.py`, `switch_client.py`, `main.py`
- `templates/` Switch room templates, one per stage plus `kraken-hq`
- `storm/faults.py` fault toolkit (`python -m storm.faults kill|latency|bad_payload|flood|bad_config`)
- `demo/shopfront/` the tiny two-service app with seeded drift; `demo/seed_chat.md` the demo messages
- `tests/` unit suite, with the golden set in `tests/golden/`
- `docs/demo-script.md` and `docs/blog-draft.md` submission material

## Status / what's verified

- **Verified:** the unit suite (`pytest`) passes offline, using fake LLM and fake Switch servers. It covers extraction, normalization, detection, code checks, the command parser, the conductor state machine, the Switch client (reconnect, gap, heartbeat), templates, the relay and shopfront.
- **Not verified here:** live runs against a real Switch server and Discord (`scripts/smoke.py`, the relay, the stage agents) and the golden-set precision run (target 0.9, needs your `ANTHROPIC_API_KEY`). These are for you to run with your own keys.
- Switch's task operations are documented as not ready, so Kraken does not use them.
- Out of scope: a web UI, relays for apps other than Discord, Jira/Linear sync, multi-repo code indexing, production hardening.
