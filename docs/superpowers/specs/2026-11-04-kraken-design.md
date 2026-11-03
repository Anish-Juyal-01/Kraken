# Kraken — Design Spec

**Date:** 2026-11-04
**Event:** Flip the Switch Hackathon (WeMakeDevs × DevTools Academy × Switch), Nov 4–7 2026
**Track:** 01 — Best Business Workflow
**Tagline:** *Release the Kraken on your codebase.*

## 1. Problem

Teams make decisions in many chat channels. Those decisions silently contradict
each other (`#backend` says `user_id` is a UUID string, `#mobile` parses it as an
int) or drift from the code (`#product` says 100 req/day, `limits.yaml` says 1000).
The mismatch ships as a bug, the fix is untested under failure, and nobody can
trace why any of it happened.

## 2. What Kraken is

An AI crew that lives in team chat through Switch. It runs one pipeline:

```
#kraken-requests ─► SPARK ─► SONAR pre-flight ─► CHARTER ─► FORGE ─► STORM ─► close
                     intake     conflict check     bidding    build    chaos test
                                     ▲                                       │
                     SONAR (always listening across all rooms)      HALLMARK (decision ledger)
```

| Name | Role |
|---|---|
| **Sonar** | Always-on watcher. Listens to every room it belongs to, extracts decision claims, detects contradictions (chat vs chat, chat vs code), and pre-flight checks briefs. |
| **Spark** | Intake. A planner agent asks clarifying questions and writes a brief with acceptance criteria. |
| **Charter** | Bidding room. Several coding agents (Claude Code, Codex, OpenCode) bid; one is chosen; the losers become reviewers. |
| **Forge** | Build room. Winner implements on a branch; reviewers push back until acceptance criteria pass. |
| **Storm** | Chaos test of the specific change. Modes: **Auto** (agent attacker + agent defender, no humans), **Drill** (humans defend), **Skip**. |
| **Hallmark** | Decision ledger: every accepted decision with who, where, when, and source link. |

Two entry points:

- **A — a person asks for something** in `#kraken-requests` → Spark → Sonar pre-flight → Charter → Forge → Storm → close.
- **B — Sonar finds a conflict on its own** → opens a resolution room with both sides → the decision people settle on becomes the brief → straight to Charter.

## 3. Why Switch is central

Kraken cannot exist without Switch. It depends on:

- **Rooms across apps:** humans and agents share one room per stage.
- **`scope=all` event stream:** Sonar hears every room it is a member of on one connection.
- **Templates + `run_template`:** each stage is a room template; Kraken creates rooms on demand.
- **Runs:** every room created for one request is recorded in one Switch run, giving a full lineage trail.
- **Room documents:** Hallmark is mirrored as a room document; briefs are room documents in their stage rooms.
- **Bring-any-agent:** Charter's bidders are different vendors' agents in the same room.

## 4. Architecture

Two kinds of participants:

1. **Kraken core** — one Python service we write. It is a Switch agent (registered via
   `POST /agents/register-known`) that speaks the agent protocol directly (SSE down,
   HTTP ops up). It contains **Sonar**, **Hallmark**, and the **Conductor** (the
   deterministic pipeline state machine). Keeping transitions in code, not in LLM
   instructions, makes the demo reliable.
2. **Stage agents** — off-the-shelf coding agents (Claude Code, Codex, OpenCode) run by
   Switch Console, configured purely by our room templates' instructions:
   Spark planner, Charter bidders/reviewers, Storm attacker (`squall`), Storm
   defender, Storm coach.

### 4.1 Repo layout

```
Kraken/
  kraken/
    switch_client.py      # SSE stream, heartbeat, ops calls, post_message
    llm.py                # Claude API wrapper (structured JSON output)
    config.py             # kraken.yaml loader (rooms, autonomy, thresholds)
    sonar/
      extract.py          # message -> Claim | None
      normalize.py        # subject aliasing (user_id / userId / "user id")
      detect.py           # same-subject claim pairs -> Conflict | Update | None
      code_check.py       # claim vs repo files (yaml/json/openapi/py types)
      preflight.py        # brief -> list of conflicts with Hallmark + code
    hallmark/
      ledger.py           # SQLite store of claims and accepted decisions
      publish.py          # mirror ledger into the #kraken-hallmark room document
    conductor/
      state.py            # per-request state machine
      commands.py         # parse replies, emoji replies and relayed reactions into commands
    relay/
      discord_relay.py    # discord.py bot: reactions on [K-n] prompts -> POST /relay/reaction
      server.py           # local HTTP endpoint inside Kraken core receiving relay events
      stages.py           # run_template per stage, pass params, record room ids
    main.py               # entrypoint: connect, dispatch events
  templates/
    kraken-hq.template.yaml        # group: #kraken-requests, #kraken-hallmark
    spark.template.yaml
    sonar-resolution.template.yaml
    charter.template.yaml
    forge.template.yaml
    storm.template.yaml
  storm/
    faults.py             # fault toolkit the attacker calls: kill, latency, bad payload, flood, bad config
  demo/
    shopfront/            # tiny 2-service app (FastAPI api + python client) with seeded drift
    seed_chat.md          # scripted messages for #backend / #mobile / #product demo channels
  tests/
  kraken.yaml             # runtime config
  README.md
```

### 4.2 Sonar — detection

1. **Filter.** Ignore Kraken's own posts and messages under 6 words. A cheap LLM call
   (Haiku-class) classifies "states a fact or decision?" — chit-chat dropped.
2. **Extract.** Second call returns a `Claim`:
   `{subject, attribute, value, polarity, speaker, room_id, message_id, thread_id, timestamp, confidence}`.
3. **Normalize.** `subject` lower-cased, snake-cased, and mapped through an alias table
   that grows as Sonar sees new spellings (LLM decides "same entity?" once per new pair,
   result cached).
4. **Detect.** For every stored claim with the same `(subject, attribute)` and a
   different `value`, a Sonnet-class call classifies the pair as `conflict`,
   `update` (newer supersedes older in the same scope) or `compatible`.
   Only `conflict` with confidence ≥ `sonar.threshold` (default 0.75) is raised.
5. **Code check.** Claims whose subject matches a configured code index
   (`kraken.yaml: sonar.code_paths`) are compared to values found in those files
   (YAML/JSON keys, OpenAPI schema types, Python type hints). Mismatch → `conflict`
   with the file path and line as the second source.
6. **Raise.** Conductor runs `sonar-resolution` template: new room with both speakers,
   both quoted messages with links, and the question *"Which one is true?"*.
   Dedupe: the same claim pair is never raised twice.

**False-positive control:** a conflict is only raised when both sides are citable.
Humans reply `@kraken not a conflict` or react 🙅; the pair is stored as `dismissed` and the
subject/attribute pattern is down-weighted for future matches.

### 4.3 Hallmark — ledger

SQLite tables: `claims`, `decisions` (accepted claims, with `supersedes`), `aliases`,
`dismissals`, `requests` (conductor state). After any decision change, `publish.py`
rewrites the `Hallmark` room document in `#kraken-hallmark` as a readable list, so
every agent in that room reads the current truth.

### 4.4 Conductor — pipeline

Per-request state: `SPARK → PREFLIGHT → CHARTER → FORGE → STORM → CLOSED`
(plus `RESOLVING` for entry B, which enters at `CHARTER`). Stored in Hallmark's
`requests` table so a restart resumes.

| Transition | Trigger | Gate (configurable in `kraken.yaml: autonomy`) |
|---|---|---|
| request → SPARK | message in `#kraken-requests` addressing `@kraken` | none |
| SPARK → PREFLIGHT | planner posts brief as room document | `brief: human` (reply `approve`) or `auto` |
| PREFLIGHT → CHARTER | Sonar finds no open conflicts, or each one is answered | always automatic once conflicts are resolved |
| CHARTER → FORGE | all bids in, or bid timeout | `pick: human` (reply `pick <agent>`) or `auto` (highest-scored bid by an LLM judge against the brief) |
| FORGE → STORM | reviewers post `ACCEPTED` | `merge: human` (reply `approve`) or `auto` |
| STORM → CLOSED | coach posts `SURVIVED` | `storm_result: human` or `auto` |
| STORM → FORGE | coach posts `FAILED` with findings | automatic; loop capped at `storm.max_rounds` (default 3), then a human is pinged |

#### Reactions as commands

Switch adds its own activity reactions (👀, ⏳) but does not forward reactions people
add, so Kraken masks reactions as text in two layers. Both map to the same command set:

| Reaction | Command |
|---|---|
| 👍 / ✅ | `approve` |
| 👎 / ❌ | `reject` (reason = rest of the message, if any) |
| 1️⃣ 2️⃣ 3️⃣ | `pick` the 1st / 2nd / 3rd bid |
| 🌪️ / 🧪 / ⏭️ | `storm auto` / `storm drill` / `storm skip` |
| 🙅 | `not a conflict` |

1. **Emoji replies (every chat app).** A reply in the thread whose text is only one
   of the emojis above is parsed as that command. Typed commands
   (`approve`, `pick codex`, …) keep working too.
2. **Reaction relay (Discord).** `kraken/relay/discord_relay.py` is a small discord.py
   bot that listens for reactions added to Kraken's prompt messages. Every prompt Kraken
   posts carries a short tag such as `[K-12]`. On a reaction, the relay reads the tag from
   the reacted message and sends `{tag, emoji, user}` to Kraken core over local HTTP
   (`POST /relay/reaction`, shared-secret header). Kraken treats it exactly like an
   emoji reply and posts a one-line receipt in the thread (*"👍 from @priya — brief
   approved"*) so the decision is visible in chat and in the room history.

Each prompt Kraken posts lists the reactions it accepts, for example:
*"[K-12] Brief ready. 👍 approve · 👎 reject"*.

Agents signal stage outcomes with fixed markers the templates require them to post on
their own line (`BID:`, `ACCEPTED`, `SURVIVED`, `FAILED`). The conductor parses only
those markers, never free text, to move state.

The Switch task operations are documented as "not ready for use", so Kraken does not
depend on them.

### 4.5 Charter — bidding

Template params: brief, bidder agents (1–3), reviewer policy. Kickoff asks each bidder
to reply once with:

```
BID: <agent>
plan: <5 bullets max>
files: <list>
estimate: <S|M|L>
risks: <bullets>
```

Bidders may critique other bids in one follow-up each. The winner gets the Forge room;
the others are invited to it as reviewers.

### 4.6 Storm — chaos test

Template param `mode: auto | drill | skip`.

- `squall` (attacker agent) reads the brief and diff, then picks 3 faults from
  `storm/faults.py` aimed at the changed code (for example: kill email service, 2s
  latency, malformed payload, request flood, bad config value) and runs them against
  `demo/shopfront`.
- **Auto:** a defender agent diagnoses and patches; the coach scores.
- **Drill:** humans in the room defend; the defender only gives hints when asked.
- Coach posts a scorecard (detected? time to diagnose, fixed? regression added?) and
  ends with `SURVIVED` or `FAILED`.

### 4.7 Demo app — shopfront

A deliberately small FastAPI `api` service plus a Python `client`, with `config/limits.yaml`
and an OpenAPI spec. Seeded drift: `user_id` type, a rate-limit value, and a v1-endpoint
shutdown date. `demo/seed_chat.md` holds the scripted messages posted into `#backend`,
`#mobile`, and `#product` for the demo video.

## 5. Error handling

- **Stream drops:** reconnect with the same connection id and `Last-Event-ID`; on a
  `gap` frame, call `read_context` for each affected room and re-scan.
- **LLM failures or invalid JSON:** retry once with the validation error appended; then
  skip the message and log it. Sonar never raises a conflict from an unparsed result.
- **Template run failure:** conductor posts the error in the request thread (fail loud)
  and keeps the request in its current state so `@kraken retry` works.
- **Stuck stage:** each stage has a timeout in `kraken.yaml`; on expiry the conductor
  pings the request owner.
- **Agent offline:** before `run_template`, check `list_agents`; if a bidder is offline,
  run Charter with the ones that are live and say so.

## 6. Testing

- **Unit:** `extract`, `normalize`, `detect` with a fake LLM returning fixtures;
  `code_check` against fixture files; `commands` parser; conductor transitions as a
  pure state machine.
- **Golden set:** ~30 labelled message pairs (conflict / update / compatible /
  chit-chat) run against the real LLM in `tests/golden/`, reporting precision/recall.
  Target: precision ≥ 0.9 on the set.
- **Client:** `switch_client` against a fake SSE server (reconnect, gap, heartbeat).
- **End-to-end smoke:** `scripts/smoke.py` posts `seed_chat.md` into a local Switch
  server and asserts a resolution room is created.

## 7. Scope and schedule

Priority order (cut from the bottom if time runs out):

1. **Sonar + Hallmark + resolution room** (the unique part) — Day 1–2
2. **Spark + Charter + Forge templates and conductor** — Day 2–3
3. **Storm, Auto mode** — Day 3
4. Storm Drill mode, auto-pick judge — Day 3–4 if time allows
5. **README, demo video, blog post, #HowISwitch post** — Day 4 (submission due Nov 7, 11:59 PM IST)

The Discord reaction relay ships with item 2; emoji replies are the fallback if it
slips.

Out of scope: a web UI, reaction relays for apps other than Discord, Jira/Linear sync, multi-repo code
indexing, production hardening.

## 8. Stack

- Python 3.12, `httpx` (HTTP + SSE), `pydantic` (models), `sqlite3`, `pyyaml`, `pytest`.
- Anthropic Python SDK; Haiku-class model for filtering, Sonnet-class model for
  extraction and conflict judgement (exact model ids fixed at implementation time).
- Switch Console (desktop) running the local Switch server in Docker; Discord as the
  demo chat app.

## 9. Open questions

- Team size (affects how stages are split between people).
- Which chat app to demo on: Discord is assumed.
