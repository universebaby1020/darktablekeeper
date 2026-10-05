# DarkHarness — factory notes

Team: **Universebaby** (UOS) · Track: Tablekeeper · Factory: [DarkHarness](https://github.com/universebaby1020/darkharness) at commit `e633fe4c73b0578fe1393aa271ca910a4a53557c`

## Summary

- **What it is.** DarkHarness runs three AI seats (coordinator, builder, reviewer) in one Band room and lets them build software on their own.
- **The submitted run.** One initial message assigned all four Tablekeeper stages. After that, no human or operator message was sent to the room.
- **Result.** All four stages were independently accepted within 1 h 58 min, and the coordinator's final report followed at 2 h 3 min. The final revision passed all 158 shipped checks.
- **The reviewer mattered.** Twice in this run it rejected work that had already passed every shipped check, and both times the builder repaired a real defect.

## Why we built it

We wanted a harness that works on its own. Once the task is sent, the seats plan, build, review, repair and report without a person steering them. The hard part was not making the agents write code. It was making the system keep going safely when something fails.

## Seats and ownership

| Seat | Responsibility | Model |
|---|---|---|
| dh coordinator | Splits the task, sends complete requirements with every handoff, routes reviews, writes the final report | gpt-6.1-sol, high effort |
| dh builder | Implements and commits each stage, repairs rejected work | gpt-6.1-sol, high effort |
| dh reviewer | Checks an independent snapshot of each committed revision, runs the official and its own checks, accepts or returns the work | gpt-6-astra, high effort |

- **Runtime.** Each seat is a distinct Band seat identity in the room. Its runtime is our Band SDK adapter (Band SDK 4.0.0) running Codex CLI 0.159.3, with a 3600-second turn limit, rather than a runtime started by Band Desktop. The mandates name the harness as `Codex (DarkHarness Band SDK adapter)`.
- **The operator.** A separate Codex session, which we call the operator, prepared the run, watched it and shut it down after the final report. It is not a seat.
- **The initial task.** The operator sent it once through the official Band CLI, under the human's explicit delegation. The sent bytes matched the prepared message exactly.
- **During the run.** The operator made no implementation, repair or steering action.
- **Two operator-side fixes.** Neither touched the room, Git or any seat:
  - After dispatch, the operator fixed a field name in its own read-only observation helper.
  - About 29 minutes after the final report, its first shutdown command gave the next revision number (2) instead of the current one (1). It was rejected before any effect and re-sent with the correct value.

## Our main design idea: tell dangerous failures from simple ones

A harness that runs alone will meet failures, and the question is what it may do next without a human. We sort failures into three kinds.

| Kind | Example | What the factory does |
|---|---|---|
| **Simple failure.** Provably did nothing, so it is safe to retry. | The turn failed before it started; a provider overload with zero effects | Retries automatically within fixed limits. Pre-start failures get 3 retries, 60 s apart. Allowed provider errors get up to 3 retries at 60/180/300 s (a usage limit waits for its reset time), and only when it is proved that nothing happened. |
| **Dangerous failure.** The effects are uncertain. | A message may or may not have been delivered; a commit may or may not have landed | Never replays the work, because that could double-send or double-commit. The work is fenced (held, not retried), the coordinator is told, and the coordinator records the blocker as the stage outcome. |
| **Safety refusal** | The provider's safety filter refuses a turn | Never retried. It is treated as the provider's judgment and reported as a blocker. |

- **When each policy is fixed.**
  - Timeout recovery and provider recovery are switched on before the task is sent, in a scoped permission record (the "Grant").
  - The pre-start retry limit is built into the factory code.
  - Nobody can change either during the run.
  - "Settled-timeout recovery" means: if a turn times out and every effect it made can be proved finished, the factory continues the work once without repeating those effects.
- **What happened in this run.** No automatic failure recovery was triggered: zero timeout, provider or pre-start retries. The six check continuations in the run were the normal way the reviewer resumes after an official check, not recoveries.

## How the factory catches a bad result

The builder never accepts its own work. For every committed revision, the reviewer does three things:
1. Makes a fresh snapshot of exactly that revision, separate from the builder's working folder.
2. Runs the official checks on it through a broker that records the revision and report hashes.
3. Runs its own clause-by-clause review and its own tests, built from the full specification.

**Mechanical checks and the reviewer catch different things.** In the submitted run, both rejections were of revisions that had passed every shipped check.

| Stage | Shipped checks on the rejected revision | What the reviewer reproduced | Repair |
|---|---|---|---|
| 1 | 120/120 passed | An out-of-range number (such as 1e309) in an unknown JSON field could make receipt saving fail after the state had already changed, so the change was not atomic | Exact number parsing; receipt and state are now saved together |
| 4 | 158/158 passed (stages 1–4) | A diner's booking response was lost, then a manager's seating plan moved that booking. Retrying the same form showed the original table from the stored receipt instead of the current one | The screen reloads the current reservation and keeps the retry identity; single-table and paired-table regressions pass |

- The same pattern appeared in our third attempt. The reviewer caught three import-validation defects, at stages 2, 3 and 4, in revisions that had passed all shipped checks.
- The shipped tests cover only part of each specification, so the independent reviewer is what holds the quality line.

## Other design choices and what they cost

- **Every handoff carries the complete task and the full applicable specification.** The official rules require this. Handoffs were 31–68 KB each, which costs tokens but means no seat works from a partial copy.
- **One durable queue per seat and one ledger** (a single SQLite writer). A busy seat cannot silently drop a handoff, and every attempt, send and check is recorded. The cost is more machinery than a simple chat loop.
- **The permission record (Grant) limits authority outside the prompts.**
  - It bounds what the factory's controller tools will do for a seat (commits, official checks and room sends) to the agreed workspace, result repository, room and checks.
  - It does not by itself limit what a seat's native sandbox can read. That is a separate, weaker boundary (see Limits).
  - The cost is preparation before every run.
- **SDK progress telemetry is turned off.** In earlier attempts, failed automatic progress reports could block a seat. The cost is that the room shows only the messages the seats wrote themselves.
- **When a run fails, the coordinator writes the report, not the operator.** After an unrecoverable failure, the operator keeps a 60-minute window open for the coordinator's blocker report. The window is extended while a seat is still working and restarts on any new failure, always within the 8-hour run deadline.

## What failed along the way

Earlier code, sessions and room content were not reused as input for the submitted run.

| Run | Factory | What happened | What we changed |
|---|---|---|---|
| Rehearsal 1 | before fe25695 | Stopped early | A check-receipt format mismatch blocked the reviewer's verification; we fixed receipt handling (fe25695) |
| Exploration run | 3240f7f | All four stages reached in 2 h 34 m; a development run in which problems were logged as they appeared | Used the log to plan the next fixes |
| Rehearsal 2 | 1853500 | Stage 1 accepted; stage 2 only partly reviewed | Replaced an instruction that required a browser review tool the reviewer did not have. The new rule: official checks plus a clause-by-clause code comparison, with visual review recorded as not performed |
| Rehearsal 3 | 1853500 → de2033b | All four stages accepted (2 h 48 m), but one provider overload needed a manual operator repair | Added a factory path for an explicit controller restart after a settled capacity failure |
| Attempt 1 (room "Tablekeeper") | de2033b | Stages 1–2 accepted; stage 3 committed, but its review never started | Three automatic diff events carried 67,587 bytes of metadata, over the 65,536-byte limit. Their delivery stayed unconfirmed and blocked the coordinator's queue. Fix: check event size before sending. |
| Attempt 2 (room "Tablekeeper 2") | 656ef48 | Stopped in stage 1 (114/120, not accepted) | The builder's permission check failed for a moment and its queue was fenced. Our best explanation, not proven, is a temporary file the sandbox creates during commands. A read-only static sweep then listed 81 candidate paths where a seat could stall and ranked the top 12. We pinned the permission check, turned telemetry off, and added the simple-failure retries and the blocker report. |
| Sweep rehearsal | e6dd3dd | All four stages accepted (2 h 08 m) with no operator action before the final report | Fixed how the initial human message is bound to the run |
| Attempt 3 (room "Tablekeeper 3") | c79e829 | Stages 1–3 accepted; the stage-4 repair passed 158/158, but its review was cut off | The reviewer's turn was refused by OpenAI's cyber safety filter. **This was a false positive.** The reviewer had been assigned to re-verify an import-validation fix: feeding our own app deliberately inconsistent import data to confirm that it rejects it. Nothing in the task targeted any real system or person. The factory correctly did not retry and told the coordinator. But our operator stop rule ended the run less than two minutes after the refusal, before the coordinator could write its report. |
| Attempt 4 (new room "Tablekeeper"; a different room with the same title as attempt 1) | e633fe4 | **Completed.** All four stages accepted, final report written | The operator now waits for the coordinator's blocker report. We also fixed native error-variant matching and raised the pre-start limit to 3 retries. We deliberately did **not** add an automatic retry for safety refusals. |

The two hardest problems were exactly the subject of our design idea: classifying failures correctly, and handling a false-positive safety refusal without trying to get around it.

## Measured time and spend

Attempt 4, times in KST (UTC+9), 5 October 2026:

| Step | Time | Duration |
|---|---|---|
| Initial task sent | 04:15:16 | — |
| Stage 1 accepted (after one rejection and repair) | 04:46:27 | 31 m 12 s |
| Stage 2 accepted | 05:11:49 | 25 m 22 s |
| Stage 3 accepted | 05:35:32 | 23 m 43 s |
| Stage 4 accepted (after one rejection and repair) | 06:13:17 | 37 m 45 s |
| Coordinator final report | 06:18:13 | total 2 h 02 m 57 s |

- **Shipped checks.** 120 (stage 1) + 25 (stage 2, including 17 browser tests) + 7 (stage 3) + 6 (stage 4) = 158, all passed. After the room export was committed, the operator re-ran the official isolated checks for each stage on a separate fresh clone: 120 / 145 / 152 / 158 (these counts are cumulative).
- **Work distribution.**
  - 37 seat turns in total: coordinator 14, reviewer 15 (6 of them continuing after a check), builder 8.
  - The seats posted 83 messages, and the reviewer ran the official checks 6 times.
  - All 6 stage commits were made by the builder. The reviewer's independent checks and rejections are in the room log, not in Git.
- **Model spend.** Our account's weekly usage window went from 16% to 19% used. This is a rounded, shared-account figure read about 29 minutes after the final report, not an exact token count or dollar cost.
- **Per-seat tokens** (native counts from the run database, read-only; not a dollar cost):

  | Seat | Input (of which cached) | Output (of which reasoning) | Total |
  |---|---:|---:|---:|
  | coordinator | 5,771,325 (5,432,448) | 28,918 (1,270) | 5,800,243 |
  | builder | 10,191,381 (9,654,272) | 134,847 (26,841) | 10,326,228 |
  | reviewer | 8,999,421 (8,552,576) | 47,134 (4,436) | 9,046,555 |
  | **all seats** | **24,962,127 (23,639,296)** | **210,899 (32,547)** | **25,173,026** |

  - **Method.** We took the last cumulative value of each seat's native thread. The 38 duplicate cumulative notifications are not double-counted.
  - **What it includes.** Input includes context that was re-sent on every turn. About 95% of it was served from cache.
  - **What it excludes.** The operator, preparation and earlier attempts.

## Limits

- No seat looked at rendered screens. UI quality was checked by the official browser tests, source comparison and scripted UI tests, not by visual review. After the run, our operator recorded the real UI flows with a headless browser (see README). That was post-run verification by us, not part of the factory's review.
- Seat runtimes are our Band SDK adapter, not runtimes started by Band Desktop. The harness line in each mandate says this.
- The operator's preparation and observation scripts are not published. The setup steps below describe what they do.
- The usage figure above is shared and rounded.
- The default sandbox does not give a complete confidentiality boundary between processes of the same user.

## How to stand it up

**Prerequisites.**
- Linux (we used WSL), Python 3.12+, Node with the pinned Codex CLI, Docker, Band SDK 4.0.0.
- Separately authenticated Band seats and Codex.
- Credentials, state and run configuration stay outside the public repositories.

**Steps.**
1. Clone the factory at the commit above, create a venv, install `.[band]` and confirm Band SDK 4.0.0. Follow `README.md`, `docs/sdk-support.md` and `docs/seat-settings.md` at that commit; parts of the two docs are written in Korean.
2. Create a fresh Band room with exactly the three seats. Use their real participant identities and choose each seat's model and effort explicitly.
3. Clone the official checker, install its requirements and Playwright Chromium in their own environment, and leave the specification files unchanged. Create a fresh workspace and clone the empty result repository on `main`.
4. Write a run configuration from `darkharness/integration/run.example.json`. It needs absolute paths, run/room/permission identities, an external credential-file path, CODEX_HOME, and each seat's model, effort and 3600-second limit.
5. Run `validate` and `prepare`. Commit only the generated mandates before any task is sent, and check that each file hash equals the runtime prompt hash.
6. Record the permission record (Grant) before the run.
   - Scope: exact workspace, repository, room, seats and the four official checks.
   - Recovery: settled-timeout recovery and zero-effect provider recovery.
   - Dispatch binding: bound to the exact initial message.
   - Leave out: no extra checks, no discretionary continuation, no safety-refusal retry.
7. Start the gateway and follow the documented controller sequence (`hello`, `grant.record`, `run.bind`, `integration.start`, wait for READY, `integration.status`).
8. Before sending the task, confirm:
   - the loaded factory commit, the mandate hashes, the permission record and model availability;
   - that the initial message hash is frozen.

   Send the task once. Never resend on uncertainty. The exact initial message we used is the first human message in `room.json`, right after the three seat-join records.

To point the factory at another problem, keep the mandates and change only the specification locations, check registrations and workspace/room/run values before sending the task.

## What we would do next

Add a GUI to DarkHarness that shows the work as it happens:
- which seat is working;
- what was handed off;
- what the reviewer found;
- why a failure was classified as simple or dangerous.

## About this document

The team wrote this document in Korean and translated it into English with an AI assistant. The factory was developed with AI coding assistants under the team's direction.
