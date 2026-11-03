# Blog draft (Best Blog side track)

**Title suggestions**
- "Release the Kraken: catching contradicting decisions in team chat with Switch"
- "Our chat channels disagreed about user_id, so we built an agent crew to referee"

## 1. The bug nobody decided to ship
- `#backend` says UUID string, `#mobile` parses an int, and `#product` says 100 requests a day while `limits.yaml` says 1000.
- Each decision was reasonable alone; the mismatch ships as a bug and nobody can trace why.

## 2. What Kraken does
- Sonar listens across channels, extracts claims, and opens a resolution room when two citable sides conflict.
- The settled decision becomes a brief that flows through Spark, Charter, Forge, Storm and Hallmark.

## 3. Why this needed Switch
- One `scope=all` event stream lets a single agent hear every room it joins.
- Room templates and `run_template` make each stage a room; runs give a lineage trail; room documents carry the ledger.
- Bring-any-agent: bidders from different vendors share a room.

## 4. Code owns the pipeline, agents own the work
- The conductor is a deterministic state machine; agents only post fixed markers (`BID:`, `ACCEPTED`, `SURVIVED`, `FAILED`).
- Why that made the demo reliable, and where the LLM calls sit (filter, extract, judge).

## 5. Reactions as commands
- Switch does not forward reactions people add, so emoji replies work everywhere and a small Discord relay covers real reactions.
- A receipt in the thread keeps every decision visible.

## 6. Letting agents bid, then break the result
- Charter: bids, critiques, and losers becoming reviewers.
- Storm: an attacker picks faults aimed at the diff, a defender patches, a coach scores; Auto, Drill and Skip modes.

## 7. Keeping false positives down
- Confidence threshold of 0.75, both sides must be citable, and `not a conflict` dismissals down-weight future matches.
- The golden set of labelled pairs with a 0.9 precision target (fill in your measured number after running it).

## 8. What we would do next
- More chat apps for the relay, richer code indexing, multi-repo support.
- Lessons from building on a new agent platform in four days.
