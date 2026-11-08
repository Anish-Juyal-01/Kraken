# 🐙 Kraken

**Your team decides in chat. Kraken turns the decision into a tested pull request.**

Kraken is a crew of AI agents that lives in your team chat (Discord, through [Switch](https://docs.switchagents.ai)). You ask for a feature in a channel. Agents bid for the job, one builds it, the others review it, chaos agents attack it, and a person approves the pull request. People get the first word at every step; if nobody answers in 2 minutes, an agent takes over so work never stalls.

Built for the **Flip the Switch hackathon** (WeMakeDevs, Nov 4–7 2026), **Track 01: Best Business Workflow**.

- 🎬 **Demo video:** <https://youtu.be/o_IiLbg4gio>
- 📦 **Repo:** <https://github.com/Anish-Juyal-01/Kraken>

---

## What it looks like

```
#kraken-requests
  you     @kraken connect https://github.com/your-team/your-api
  kraken  Connected. your-api · python · tests: pytest · runs on :8000
  you     @kraken build your-api: add a JSON endpoint GET /api/todos/{id}

#spark-k-5     the planner writes a brief            → "Who's handling this? 🙋 you / 🤖 the agent"
#charter-k-5   Claude, Gemini, Codex… agents bid     → "Pick one: 1️⃣ 2️⃣ 3️⃣ 4️⃣"
#forge-k-5     the winner builds, the others review  → "Reviewers accepted. Merge?"
#storm-k-5     chaos agents attack the running app   → "Who defends? 🙋 you / 🤖 the defender"
               Storm survived. Open a pull request?  → 👍 (always a person)
```

In a real run on a repo Kraken had never seen ([fastapi-todo](https://github.com/renceInbox/fastapi-todo)), Storm found a bug that the build and both reviewers missed: `GET /api/todos/9223372036854775808` crashed with a 500. The defender agent fixed it (now a 404) and added regression tests.

## The pipeline

```
#kraken-requests ─► SPARK ─► SONAR pre-flight ─► CHARTER ─► FORGE ─► STORM ─► DELIVER
                    brief    conflict check      bidding    build    chaos    pull request
                                                            + review  test    (a person 👍)
          SONAR listens to every room          HALLMARK records every decision
```

| Stage | What happens | Where a person comes in |
|---|---|---|
| **Spark** | A planner agent turns the request into a brief with acceptance criteria. | Answer its questions and approve the brief, or hand it to the agent. |
| **Sonar** | Watches all rooms for decisions that contradict each other or the code (`#product` says 100 requests a day, `limits.yaml` says 1000) and checks every brief before work starts. | Settle a conflict in the resolution room it opens. |
| **Charter** | Every coding agent bids, naming the real files it will touch. | Pick the winner, or let the judge pick and record why. |
| **Forge** | The winner builds on its own branch; the losing bidders review it against the brief and post `ACCEPTED` only when it's met. | Give the merge OK. |
| **Storm** | Squall (the attacker) throws bad input, floods, kills and corrupt config at the running app. | Defend yourself (fix what breaks), or let `storm-defender` do it. Then accept the result. |
| **Deliver** | The coder pushes the branch and opens a pull request with the brief, the reviews and the Storm scorecard. | **Always a person's 👍.** Nothing leaves the chat without it. |
| **Hallmark** | The decision ledger: who decided what, and why. Mirrored into a Switch room document. | Read it any time in `#kraken-hallmark`. |

## How Kraken uses Switch

Switch is not a plug-in here: Kraken is built out of it, and without Switch there is nothing left to run.

| Switch feature | What Kraken does with it |
|---|---|
| **Rooms on Discord** (bridge) | Every stage of every request gets its own room (`#spark-k-5`, `#charter-k-5`, …), so people and agents work side by side in normal Discord channels. |
| **Agent protocol** (SSE in, HTTP ops out) | Kraken is itself a Switch agent. One event stream (`scope=all`) lets it hear every room it belongs to. |
| **Room templates + `run_template`** | Each stage is a template in `templates/`. Kraken creates the rooms on demand, with the right agents and people in them. |
| **Bring any agent** | The crew is real coding agents in Switch, on whatever vendor you choose: Claude Code, Codex, OpenCode (Gemini, Kimi, Qwen, …) or anything that speaks the protocol. Our runs used Claude Code and Gemini through OpenCode, side by side in the same rooms. |
| **Agent list** (`list_agents`) | Any new coding agent added in Switch Console joins the crew and bids at the next Charter on its own: no code change, no command. |
| **Runs** | Every room made for one request is recorded in one Switch run, a full trail from request to pull request. |
| **Room documents** | Briefs live as documents in their stage rooms; the Hallmark ledger is mirrored as a document. |
| **Crew template** | `templates/kraken-crew.template.yaml` creates the whole crew in Switch Console in one click. |

The order of the stages lives in code (a small state machine), not in prompts. Agents report results with fixed markers on their own line (`BID:`, `ACCEPTED`, `SURVIVED`, `FAILED`, `PR:`), and Kraken reads only those.

## Run it yourself

**You need:** Python 3.12+, [Switch Console](https://docs.switchagents.ai/switch-rooms/getting-started) with Docker (Console runs the local Switch server), a Discord server you control, and **any one** AI agent or model: Claude Code, Codex, OpenCode, Gemini CLI, or an API key for OpenAI, Gemini, Anthropic or any OpenAI-compatible service (OpenRouter, Groq, a local Ollama, …). Claude is not required.

### 1. Set up Switch and Discord

Install Switch Console and start it; it starts the Switch server in Docker. The [10-minute quickstart](https://docs.switchagents.ai/switch-rooms/getting-started) walks through it.

**Connect your Discord server.** There is no Kraken bot to invite: Kraken talks to Discord through Switch's Discord connection. You make one Discord bot for your team and connect it once. These steps follow [Switch's Discord guide](https://docs.switchagents.ai/switch-rooms/deploy/messaging-apps/discord):

1. **Create the bot.** In the [Discord Developer Portal](https://discord.com/developers/applications), select **New Application** and name it (for example "Kraken"). Open the **Bot** tab and copy the bot's token. Keep it secret.
2. **Turn on two intents.** Still on the **Bot** tab, under *Privileged Gateway Intents*, turn on **Server Members Intent** and **Message Content Intent**, then **Save Changes**. Without Message Content, agents receive empty messages.
3. **Invite the bot to your server.** Open **OAuth2 → URL Generator**. Tick the scopes `bot` and `applications.commands`, then these permissions:
   - View Channels
   - Send Messages and Send Messages in Threads
   - Manage Webhooks (each agent posts under its own name)
   - Manage Channels and Manage Roles
   - Read Message History
   - Attach Files and Add Reactions

   Open the URL it generates and add the bot to your Discord server.
4. **Copy your server ID.** In Discord, open **User Settings → Advanced** (called *Developer* in some versions), turn on **Developer Mode**, then right-click your server's icon and select **Copy Server ID**.
5. **Connect it in Switch Console.** Open your Switch server's **Home** page, find **Messaging apps** and select **Connect → Discord**. Enter:
   - **Name:** for example `Dc Switch`. You'll put this name in `kraken.yaml` later (section 4 below).
   - **Bot Token:** the token you copied from the Bot tab.
   - **Guild Id:** the server ID you just copied.

   Select **Connect**, then pick your own Discord account and select **This is me**.

**Your teammates install nothing.** They only need to be in the Discord server and listed under `people:` in `kraken.yaml` (section 4 below), so Kraken adds them to every request's rooms. Whoever asks for something is added to that request's rooms automatically.

### 2. Install Kraken

```bash
git clone https://github.com/Anish-Juyal-01/Kraken.git
cd Kraken
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 3. Register Kraken as a Switch agent

```bash
cp .env.example .env               # Windows: copy .env.example .env
```

In `.env`, set `SWITCH_URL` (your local Switch server) and `SWITCH_REGISTRATION_TOKEN` (from Switch Console). Then:

```bash
python scripts/register.py
```

It prints `SWITCH_AGENT_ID=...` and `SWITCH_API_KEY=...`. Paste both lines into `.env`.

Kraken also needs a model for its own thinking (spotting conflicts, judging bids). With nothing set, it uses the first agent CLI it finds signed in (Claude Code, Codex, OpenCode or Gemini CLI). To choose, see [Kraken's own model](#krakens-own-model).

### 4. Create the crew and the rooms (Switch Console → Templates)

1. Run **`templates/kraken-crew.template.yaml`**. Its **provider** field picks the coding agent the crew runs on: `claude` (default), `codex`, `opencode`, or whatever your Console lists. It creates three coders named after that agent (`codex-coder`, `codex-coder-2`, `codex-coder-3` for `codex`; `claude-coder`, … for `claude`) plus `kraken-planner`, `squall`, `storm-defender` and `storm-coach`. The coders join Kraken's crew on their own at the first Charter.
2. Run **`templates/kraken-hq.template.yaml`**. It creates `#kraken-requests` and `#kraken-hallmark` on your Discord, with Kraken in both.
3. In `kraken.yaml`, set `bridge:` to the **Name** you gave the Discord connection in section 1 (for example `Dc Switch`), and `people:` to your team's Discord usernames (for example `people: [anish_2007, priya]`). Stage rooms then open as Discord channels with all of you in them. Restart Kraken after adding someone.

Want Gemini, Codex or another vendor too? Create that agent in Switch Console (set its addressing to **Anyone**, so it answers Kraken's mentions). It joins the crew by itself.

### 5. Start Kraken

```bash
python -m kraken.main
```

Run it from the repo root (it reads `kraken.yaml` and `.env` there). It connects to Switch and starts listening.

### 6. Use it from Discord

In `#kraken-requests`:

```
@kraken connect https://github.com/you/your-repo
@kraken build your-repo: add input validation to POST /orders
```

Then follow the rooms Kraken opens. Each question says what you can reply.

### Check it works

```bash
pytest                             # 469 offline tests (fake Switch, fake LLM), no keys needed
python scripts/smoke.py            # optional live end-to-end check (see below)
```

<details>
<summary>Optional extras: the demo app, the smoke test, the reaction relay, resetting</summary>

- **Shopfront demo app** (the bundled app Sonar's demo conflicts are about):
  `SHOPFRONT_CHAOS=1 python demo/shopfront/run.py` (PowerShell: `$env:SHOPFRONT_CHAOS = "1"; python demo/shopfront/run.py`). Run `templates/kraken-demo.template.yaml` for the demo channels (`#backend`, `#mobile`, `#product`, `#partners`).
- **Smoke test:** register a second agent with `python scripts/register.py --name kraken-smoke`, paste the printed `SMOKE_AGENT_ID` / `SMOKE_API_KEY` into `.env`, add it to the demo rooms, and run `python scripts/smoke.py` while Kraken runs. It posts `demo/seed_chat.md` and passes when Sonar opens a resolution room (exit 0 OK, 1 timeout, 2 setup missing).
- **Reaction relay:** Switch doesn't forward emoji reactions, so replying with the emoji as a message is the normal way. To use real reactions, set `DISCORD_RELAY_TOKEN` and `KRAKEN_RELAY_SECRET` in `.env`, restart Kraken, and run `python -m kraken.relay.discord_relay`.
- **Reset before a demo:** stop Kraken, delete `kraken.db`, start Kraken again.
- **Real-LLM precision check for Sonar:** `python tests/golden/run_golden.py`.

</details>

## Human first

Every step belongs to a person until they say otherwise. When a step needs someone, Kraken asks **"Who's handling this?"**:

- reply **🙋** or `me` (or just start answering): it's yours, **no time limit**;
- reply **🤖** or `agent`: the agent does it;
- **no answer in 2 minutes:** the agent takes it, and Kraken says so in the room.

| Step | If you take it | If the agent takes it |
|---|---|---|
| **Spark** | You answer the planner and approve the brief. | The planner decides open questions itself and lists them as *Assumptions*. |
| **Charter** | You pick a bid (1️⃣ 2️⃣ 3️⃣ …). | The judge picks the best bid and records why. |
| **Forge** | You give the merge OK. | The reviewers' `ACCEPTED` is enough. |
| **Storm** | You defend: fix what Squall breaks, the coach scores you. Then you accept the result. | `storm-defender` patches the faults; the result is accepted. |
| **Pull request** | You approve it. | Never: the pull request always waits for a person. |

A handoff covers one step of one request; the next request starts human-first again. Set `human_wait_s: 0` in `kraken.yaml` to always wait for people, or set a step's `autonomy` key to `auto` to always give it to agents.

## Working as a team

- **Anyone can ask.** Any person in `#kraken-requests` can start a request. Each request gets its own rooms and tag (`K-12`), and whoever asked is added to those rooms and mentioned, along with everyone in `people:`.
- **Anyone can answer.** Any person in a stage room can approve, reject, pick or take a step. If two people answer at once, the first answer counts.
- **Take over from a teammate.** If someone else is handling a step, reply 🙋 or `me` and it moves to you; Kraken says so in the room.
- **One build per repo at a time.** Each agent keeps one checkout of a repo, and Storm runs the app on that repo's port. So when two requests reach Forge on the same repo, the second waits ("waiting for its turn: K-11 is still building or testing your-repo") and starts as soon as the first is done. Requests on different repos run side by side.

## Kraken's own model

Besides the crew, Kraken itself thinks a little: Sonar spots conflicting decisions, and the judge picks a bid when a person hands that step over. Any model works. Set `KRAKEN_LLM` in `.env`, or leave it as `auto`:

| `KRAKEN_LLM` | Uses | Set |
|---|---|---|
| `auto` (default) | The first one that is set up, in this order: an API key below, then a signed-in agent CLI on your PATH (Claude Code, Codex, OpenCode, Gemini CLI) | nothing |
| `claude-code` / `codex` / `opencode` / `gemini-cli` | That agent's CLI, already signed in: no API key | optionally `KRAKEN_LLM_MODEL` |
| `openai` | Any OpenAI-compatible API: OpenAI, OpenRouter, Groq, DeepSeek, Mistral, Kimi, a local Ollama or LM Studio | `KRAKEN_LLM_API_KEY` (or `OPENAI_API_KEY`), `KRAKEN_LLM_BASE_URL` (default OpenAI), `KRAKEN_LLM_MODEL` |
| `gemini` | Google's Gemini API | `GEMINI_API_KEY`, optionally `KRAKEN_LLM_MODEL` (default `gemini-2.5-flash`) |
| `api` | Anthropic's API | `ANTHROPIC_API_KEY` |
| `cli` | Any command you like. The prompt goes on stdin, or wherever you write `{prompt}` | `KRAKEN_LLM_CMD`, e.g. `my-agent --ask {prompt}` |

`KRAKEN_LLM_FAST_MODEL` optionally sets a cheaper model for Sonar's quick "is this a decision?" filter. Agent CLIs run in a fresh, empty folder, so they never touch your files. Examples:

```bash
KRAKEN_LLM=openai KRAKEN_LLM_BASE_URL=http://localhost:11434/v1 KRAKEN_LLM_MODEL=llama3.1   # local Ollama
KRAKEN_LLM=gemini GEMINI_API_KEY=...                                                         # Gemini
KRAKEN_LLM=codex                                                                             # your Codex login
```

## Chat commands

Every prompt carries a tag like `[K-12]` and lists the replies it accepts. A leading `@kraken` is optional in stage rooms.

| Command | Emoji | What it does |
|---|---|---|
| `approve` / `reject [reason]` | 👍 / 👎 | Answer the pending question |
| `pick <agent>` | 1️⃣ 2️⃣ 3️⃣ … | Pick a bid |
| `me` / `agent` | 🙋 / 🤖 | Take a step yourself / hand it to the agent |
| `storm auto` / `storm drill` / `storm skip` | 🌪️ / 🧪 / ⏭️ | Agents defend / people defend / no Storm |
| `not a conflict` | 🙅 | Dismiss a Sonar conflict |
| `retry` | | Retry a stage that failed to open |
| `connect <git url> [name]` | | Add a repo: Kraken clones it and works out the language, tests and run command |
| `projects` / `use <name>` | | List repos / choose the default |
| `project set <name> <field> <value>` | | Fix a detected value, e.g. `project set api test npm run test:ci` |
| `crew` / `enlist <name> [as <role>]` / `retire <name>` | | Show / add to / remove from the crew |

`connect`, `projects`, `use`, `project set`, `crew`, `enlist` and `retire` work in `#kraken-requests`.

## Works on any repo, with any agent

**Repos.** `@kraken connect <url>` detects Python (FastAPI, Flask, Django), Node (Express, Fastify, Nest, Koa, Hono), Go and Rust; anything else connects too, and `project set` fills in the commands. Each agent clones the repo into its own folder and works on branch `kraken/K-<n>`. Storm attacks from the outside, so the app needs no changes: with a run command it starts the app and injects kills, latency, dropped connections, bad and oversized input, floods, corrupt config and missing environment variables; for a library or CLI it runs the tests under faults and compares with a clean run.

**Agents.** Use any vendor for any seat. The crew template's `provider` field sets the vendor for the whole starting crew; to mix, add more agents in Switch Console, or move one into a role with `@kraken enlist <name> as planner` (or `attacker`, `defender`, `coach`). At every Charter Kraken asks Switch for its agents. Any coding agent not yet on the crew (Claude Code, Codex, OpenCode with Gemini, Kimi or Qwen, Antigravity, Cursor, …) becomes a bidder, and Kraken announces it. `@kraken retire <name>` removes one; `charter.auto_join: false` turns this off. If more than 5 bidders are online, Kraken seats a mix of vendors and rotates who gets in.

<details>
<summary>Configuration (<code>kraken.yaml</code>)</summary>

| Key | Default | Meaning |
|---|---|---|
| `people` | `[]` | Discord usernames added to every stage room |
| `bridge` | | Name of your Discord bridge in Switch |
| `human_wait_s` | `120` | Seconds Kraken waits for a person before the agent takes the step (`0` = always wait) |
| `agent_quiet_s` | `600` | Nudge agents that have been silent this long |
| `autonomy.brief` / `pick` / `merge` / `storm_result` | `human` | `auto` gives that step to agents for every request |
| `storm.default_mode` | `drill` | `drill` (people asked first), `auto` or `skip` |
| `storm.max_rounds` | `3` | Storm → Forge loops before a person is pinged |
| `deliver` | `pr` | `pr` opens a pull request after a person's 👍; `branch` keeps work local |
| `charter.auto_join` | `true` | New Switch agents join the crew by themselves |
| `charter.never_join` | `[]` | Agent names that never auto-join |
| `charter.max_bidders` | `5` | Most bidders in one Charter |
| `projects` / `default_project` | shopfront | Seed repos; more are added from chat and kept in `kraken.db` |
| `bidders` | `[]` | Extra bidders to seed; the crew template's coders join on their own, so this stays empty |
| `agents.*` | `kraken-planner`, `squall`, `storm-defender`, `storm-coach` | The single seats; after the first start the crew lives in `kraken.db` |
| `sonar.threshold` | `0.75` | Minimum confidence to raise a conflict |
| `rooms.requests` / `rooms.hallmark` | `kraken-requests` / `kraken-hallmark` | Room names |
| `timeouts_s.*` | 15–60 min | Per-stage timeout; on expiry Kraken pings the owner |
| `db_path` | `kraken.db` | SQLite file for requests, claims and decisions |

Environment variables: `SWITCH_URL`, `SWITCH_AGENT_ID`, `SWITCH_API_KEY` (required); `KRAKEN_LLM` and its keys (see [Kraken's own model](#krakens-own-model)); `SWITCH_REGISTRATION_TOKEN` (registration only); `SMOKE_*`, `DISCORD_RELAY_TOKEN`, `KRAKEN_RELAY_SECRET` (optional extras).

</details>

## What's verified

- **Offline:** 469 tests pass (`pytest`), with fake Switch and fake LLM servers: the state machine, human-first waits and handoffs, auto-join, the command parser, Sonar, Storm faults, repo detection and the Switch client.
- **Live on Discord through Switch:**
  - the full pipeline on the bundled shopfront app;
  - `@kraken connect` and a full build on [fastapi-todo](https://github.com/renceInbox/fastapi-todo) (request K-5), where Storm found and fixed the 64-bit ID crash;
  - human-first prompts with the 2-minute takeover;
  - Gemini (through OpenCode) auto-joining and bidding next to the Claude coders.
- **Not done live yet:** a pull request opened on a real remote repo (the K-5 work stayed on a local branch), and Sonar's real-LLM precision run.

## Repo map

| Path | What's there |
|---|---|
| `kraken/` | The service: `conductor/` (state machine and stage runner), `dispatch.py` (chat events, human-first waits), `sonar/`, `hallmark/`, `crew.py`, `projects.py`, `init.py` (repo detection), `switch_client.py`, `main.py` |
| `templates/` | Switch templates: one per stage, plus the crew, HQ and demo rooms |
| `storm/` | Fault toolkit: starts the app, proxies it, injects faults |
| `demo/` | Shopfront demo app and seed chat |
| `tests/` | Offline test suite and Sonar's golden set |
| `docs/` | Demo script and blog draft |
