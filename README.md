# DarkHarness — Tablekeeper

Team: **Universebaby** (UOS) · Track: Tablekeeper · Factory: [DarkHarness](https://github.com/universebaby1020/darkharness) (commit `e633fe4`)

This repository is the Tablekeeper result built by three AI seats working together through DarkHarness:
- **Coordinator:** split the work and sent complete requirements with every handoff.
- **Builder:** implemented and committed each stage.
- **Reviewer:** checked every committed revision independently and sent defects back for repair.

A single initial message assigned all four stages. No other human or operator message was sent to the room. All four stages were independently accepted. The final implementation revision is `8ef4e4ddb979e77815ed64e27e4cf99a7f919301`. The only later human-side commits add `room.json`, this README, `FACTORY.md` and the license; they do not touch any stage folder.

See **FACTORY.md** for how the factory works, what failed along the way, and the measured time and cost.

## Repository

- `stage-1/`: reservation API, retries, time handling and state transfer
- `stage-2/`: browser booking flow and combined tables
- `stage-3/`: policy snapshots, history and recurring reservations
- `stage-4/`: seating replans and recurring amendments
- `mandates/`: the generic seat instructions, committed before the task was sent
- `room.json`: the unchanged full-session export of the submitted room
- `FACTORY.md`: design, failures, measurements and limits

Each stage is a complete folder with its own `Dockerfile` and `RUN.md`. To run a stage, follow that folder's `RUN.md`. State is kept in memory, and each `RUN.md` explains how to reset it with the fixture endpoint.

## Try the UI

Each `RUN.md` names the `POST /_test/reset` fixture endpoint but gives no sample data. After the run, we wrote the sample below in the specification's format. It was not produced by the seats. Accounts and data are fictional.

Start stage 4 as its `RUN.md` says, then load the sample:

```sh
curl -i -X POST http://localhost:8080/_test/reset -H 'Content-Type: application/json' -d '{
  "users": [{"id": "manager_demo", "email": "manager@example.com",
             "password": "Demo-manager-only", "display_name": "Demo Manager"}],
  "restaurants": [{"id": "r_demo", "name": "Cedar House", "timezone": "Europe/Berlin",
    "slot_minutes": 30, "reservation_duration_minutes": 90, "cancellation_cutoff_minutes": 120,
    "opening_hours": [{"weekday": "thu", "opens": "18:00", "closes": "23:00"}],
    "tables": [{"id": "t_window", "label": "Window", "capacity": 2},
               {"id": "t_garden", "label": "Garden", "capacity": 2},
               {"id": "t_terrace", "label": "Terrace", "capacity": 4}],
    "combinable": [["t_window", "t_garden"], ["t_garden", "t_terrace"]],
    "manager_user_ids": ["manager_demo"]}],
  "reservations": []
}'
```

- **Expected response.** The call returns `204 No Content`.
- **Try it.** Open `http://localhost:8080/`, sign up, choose **Cedar House**, pick an upcoming **Thursday** and search. Single tables and the declared pairs (for example Window+Garden for a party of four) can be booked, looked up and cancelled.
- **Stage 3** uses the same sample.
- **Stage 2:** remove `manager_user_ids`.
- **Stage 1** has no UI.

## Verification

- **Official checks.** After `room.json` was committed, the official isolated checks were run on a separate fresh clone. Stages 1–4 passed with cumulative totals of 120, 145, 152 and 158. These totals overlap, so they are not 575 different tests.
- **Coverage.** The shipped checks cover only part of each specification, and the reviewer added its own checks.
- **RUN.md run-through.** After the run, our operator (a separate Codex session, not a seat; see FACTORY.md) followed each stage's `RUN.md` literally in a fresh clone.
  - All four stages built and became healthy within about 3 seconds each (warm Docker cache).
  - Each full session, including the `RUN.md` local tests and the browser flows, took 15–48 seconds.
  - The stage 2–4 UI flows were exercised and recorded with a headless browser at 375 px and 1440 px: sign-up, login, search, booking, retry, lookup, cancellation, combined tables, and at stage 4 the current seat after a replan.
  - The browser recorded no page errors, no console errors and no 4xx/5xx responses.
  - Extra steps were needed:
    - sample data for the reset endpoint (see above), because `RUN.md` gives none;
    - local PATH and bytecode settings;
    - the official runner image's Chromium, because the host lacked a browser library.
- **No visual review.** No seat reviewed the rendered screens visually.

## License

MIT. The official specification text quoted inside `room.json` stays under its original Apache-2.0 license.

The team wrote this README and FACTORY.md in Korean and translated them into English with an AI assistant.
