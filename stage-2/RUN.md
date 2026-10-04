# Run Stage 2

From this folder, build and start the standalone service:

```sh
docker build -t tablekeeper-stage-2 .
docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper-stage-2
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

Local source-level verification (Python 3.11 or later, with IANA timezone data installed):

```sh
python -m unittest -v test_service.py test_stage2.py
node test_ui.js
```

The migration check in `test_stage2.py` reads the preserved sibling `stage-1/service.py`.
The JavaScript checks use an in-memory DOM and mocked responses; they are state-flow checks,
not browser rendering or visual verification. Node is needed only for those local checks,
not for running the service image.
