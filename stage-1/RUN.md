# Run Stage 1

From this folder, build and start the standalone service:

```sh
docker build -t tablekeeper-stage-1 .
docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper-stage-1
```

The service listens on all interfaces. `GET /health` reports readiness. Initialize restaurants,
tables and accounts using the unauthenticated JSON `POST /_test/reset` fixture endpoint.
No external service or runtime network access is needed. State is held in memory and resets
when the container restarts.

The public browsing endpoints are `GET /restaurants`, `GET /restaurants/{id}` and
`GET /availability?restaurant_id=...&date=YYYY-MM-DD&party_size=...`.
Signup and login return bearer tokens for reservation operations. Creation and atomic batch
moves require `Idempotency-Key`; retries preserve the original response.

`GET /_test/export` and `POST /_test/import` transfer the complete state. Export files contain
private account hashes and session tokens and should be kept private.

Local source-level verification (Python 3.11 or later, with IANA timezone data installed):

```sh
python -m unittest -v test_service.py
```
