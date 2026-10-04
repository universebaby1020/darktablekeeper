# Run Stage 4

From this folder, build and start the standalone service:

```sh
docker build -t tablekeeper-stage-4 .
docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper-stage-4
```

The service listens on all interfaces. `GET /health` reports readiness. Initialize restaurants,
tables and accounts using the unauthenticated JSON `POST /_test/reset` fixture endpoint.
No external service or runtime network access is needed. State is held in memory and resets
when the container restarts.

Open `http://localhost:8080/` to search availability and book a single table or an approved
pair. Signup, login and reservation lookup are available at `/signup`, `/login` and `/lookup`.
Browser assets are included in the image, with no external fonts or scripts. A lost booking
response can be retried from the unchanged form using its original request identity.

The public browsing endpoints are `GET /restaurants`, `GET /restaurants/{id}` and
`GET /availability?restaurant_id=...&date=YYYY-MM-DD&party_size=...`.
Signup and login return bearer tokens for reservation operations. Creation and atomic batch
moves require `Idempotency-Key`; retries preserve the original response.

`GET /_test/export` and `POST /_test/import` transfer the complete state. Export files contain
private account hashes and session tokens and should be kept private.
Stage 1 exports are accepted without losing accounts, bearer tokens, references or completed
retry receipts. Stage 2 fixtures can declare `combinable` table pairs and seed cancelled bookings.

Stage 3 adds public `GET /restaurants/{id}/policies`, manager-only idempotent
`POST /restaurants/{id}/policies`, optional availability `explain=true`, owner-only
`GET /reservations/{reference}/history` and `/decision`, and recurring adoption via
idempotent `POST /series` followed by owner-only `GET /series/{id}`. Manager IDs are supplied
in reset fixtures. Policies are complete dated snapshots; booking decisions and amendments
use the appropriate local-date policy while cancellation keeps the accepted cutoff.
PATCH and collective moves may send a positive `expected_revision` for optimistic updates.
No-op changes preserve accepted terms, history and revision. Series adoption and collective
changes commit reservations, histories, revisions, exception flags and retry receipts together.
Both stage 1 and stage 2 exports are accepted.

Stage 4 adds manager-only idempotent `POST /restaurants/{id}/replans` previews and
`POST /restaurants/{id}/replans/{plan_id}/apply` for closure repairs. Preview optimizes changed
seating sets, unused seats and reference-ordered option ranks, using each booking's accepted
capacities. Application checks the restaurant revision and commits seating, closure and history
atomically. Applied closures exclude overlapping table choices from every availability and
booking decision. Operator seating repairs preserve times, terms and recurring exceptions.

Owners can use idempotent `POST /series/{id}/amend` with `expected_revision`, `from_index` and
`local_time` to change eligible recurring occurrences together. Original scheduled dates remain
fixed; cancelled occurrences and diner exceptions are excluded. Changed occurrences use their
resulting date's policy, with one series and restaurant revision for the entire transaction.
Stage 1, 2 and 3 exports remain importable, including their original receipts and edited series.

Local source-level verification (Python 3.11 or later, with IANA timezone data installed):

```sh
python -m unittest -v test_service.py test_stage2.py test_stage3.py test_stage4.py
node test_ui.js
```

The migration check in `test_stage2.py` reads the preserved sibling `stage-1/service.py`.
The JavaScript checks use an in-memory DOM and mocked responses; they are state-flow checks,
not browser rendering or visual verification. Node is needed only for those local checks,
not for running the service image.
