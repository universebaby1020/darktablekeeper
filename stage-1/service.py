"""Tablekeeper reservations API. All state transitions share a single transaction lock."""
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


class APIError(Exception):
    def __init__(self, status=422, code='validation_failed', message=None):
        self.status, self.code = status, code
        self.message = message or code.replace('_', ' ')


def fail(status=422, code='validation_failed'):
    raise APIError(status, code)


def string(obj, key, *, identifier=False):
    if key not in obj:
        fail()
    value = obj[key]
    if not isinstance(value, str):
        fail(400, 'malformed_request')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        fail()
    if identifier and not 1 <= len(value) <= 64:
        fail()
    return value


def integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        fail()
    return value


def local_datetime(value):
    if not isinstance(value, str):
        fail(400, 'malformed_request')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', value):
        fail()
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M')
    except ValueError:
        fail()


def local_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        fail()
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail()


def clock_minutes(value):
    if not isinstance(value, str):
        fail(400, 'malformed_request')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        fail()
    return int(value[:2]) * 60 + int(value[3:])


def resolve(wall, zone):
    # Round-trip through UTC excludes skipped local times. fold=0 is the first occurrence.
    aware = wall.replace(tzinfo=zone, fold=0)
    if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != wall:
        fail(422, 'invalid_local_time')
    return aware


def canonical(value):
    # Preserve JSON numbers without float overflow, underflow or precision loss.
    # Normalize their spelling without rounding; booleans remain distinct from numbers.
    def encode(v):
        if isinstance(v, dict):
            return '{' + ','.join(json.dumps(k) + ':' + encode(v[k]) for k in sorted(v)) + '}'
        if isinstance(v, list):
            return '[' + ','.join(encode(x) for x in v) + ']'
        if type(v) in (int, float) or isinstance(v, Decimal):
            number = v if isinstance(v, Decimal) else Decimal(str(v))
            if not number.is_finite():
                fail(400, 'malformed_request')
            sign, digits, exponent = number.as_tuple()
            coefficient = ''.join(str(d) for d in digits)
            if not number:
                return '0'
            while coefficient.endswith('0'):
                coefficient = coefficient[:-1]
                exponent += 1
            prefix = '-' if sign else ''
            # Bound representation size, not numeric range; large exponents stay compact.
            if 0 <= exponent <= 300:
                return prefix + coefficient + '0' * exponent
            if -300 <= exponent < 0:
                point = len(coefficient) + exponent
                if point > 0:
                    return prefix + coefficient[:point] + '.' + coefficient[point:]
                return prefix + '0.' + '0' * (-point) + coefficient
            return prefix + coefficient + 'e' + str(exponent)
        return json.dumps(v, allow_nan=False)
    return encode(value)


def parse_json(value):
    try:
        return json.loads(value, parse_float=Decimal,
                          parse_constant=lambda _: fail(400, 'malformed_request'))
    except DecimalException:
        fail(400, 'malformed_request')


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 160000)
    return 'pbkdf2_sha256$160000$' + salt + '$' + digest.hex()


def password_matches(password, stored):
    _, rounds, salt, digest = stored.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
    return hmac.compare_digest(actual.hex(), digest)


def empty_state():
    return {'users': {}, 'tokens': {}, 'restaurants': {}, 'reservations': {}, 'receipts': {}}


class Service:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = empty_state()

    def request(self, method, target, headers=None, body=None):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        parsed = urlsplit(target)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        with self.lock:
            try:
                return self.dispatch(method, path, query, headers, body)
            except APIError as error:
                return error.status, {'error': {'code': error.code, 'message': error.message}}

    def user(self, headers):
        auth = headers.get('authorization', '')
        match = re.fullmatch(r'Bearer ([^\s]+)', auth, flags=re.IGNORECASE)
        uid = self.state['tokens'].get(match[1]) if match else None
        if uid not in self.state['users']:
            fail(401, 'unauthenticated')
        return uid

    @staticmethod
    def object_body(body):
        if not isinstance(body, dict):
            fail(400, 'malformed_request')

    def restaurant(self, rid):
        if not isinstance(rid, str):
            fail(400, 'malformed_request')
        if not 1 <= len(rid) <= 64:
            fail()
        if rid not in self.state['restaurants']:
            fail(404, 'not_found')
        return self.state['restaurants'][rid]

    def owned(self, reference, uid):
        if not isinstance(reference, str) or not reference or len(reference) > 64:
            fail()
        res = self.state['reservations'].get(reference)
        if not res or res['user_id'] != uid:
            fail(404, 'not_found')
        return res

    @staticmethod
    def public(res):
        return {k: copy.deepcopy(v) for k, v in res.items() if k != 'user_id'}

    def receipt(self, method, path, uid, headers, body):
        key = headers.get('idempotency-key')
        if key is None or key == '':
            fail(400, 'missing_idempotency_key')
        if not 1 <= len(key) <= 255:
            fail()
        index = canonical([uid, method, path, key])
        serialized_body = canonical(body)
        saved = self.state['receipts'].get(index)
        if saved:
            if saved['body'] != serialized_body:
                fail(409, 'idempotency_key_reuse')
            return index, serialized_body, copy.deepcopy(saved['response'])
        return index, serialized_body, None

    def validated_booking(self, rid, fields):
        # Validate without modifying either occupancy or records.
        table_id = string(fields, 'table_id', identifier=True)
        wall = local_datetime(string(fields, 'starts_at_local'))
        if 'party_size' not in fields:
            fail()
        party = integer(fields['party_size'])
        restaurant = self.restaurant(rid)
        table = next((t for t in restaurant['tables'] if t['id'] == table_id), None)
        if table is None:
            fail(404, 'not_found')
        if party > table['capacity']:
            fail(422, 'party_exceeds_capacity')
        zone = ZoneInfo(restaurant['timezone'])
        start = resolve(wall, zone)
        try:
            end = (start.astimezone(UTC) + timedelta(minutes=restaurant['reservation_duration_minutes'])).astimezone(zone)
        except (OverflowError, ValueError):
            fail()
        intervals = [h for h in restaurant['opening_hours'] if h['weekday'] == DAYS[wall.weekday()]]
        minute = wall.hour * 60 + wall.minute
        in_hours = False
        on_grid = False
        for interval in intervals:
            opening, closing = clock_minutes(interval['opens']), clock_minutes(interval['closes'])
            close_wall = datetime.combine(wall.date(), datetime.min.time()) + timedelta(minutes=closing)
            try:
                close = resolve(close_wall, zone)
            except APIError:
                continue
            if opening <= minute < closing and end.astimezone(UTC) <= close.astimezone(UTC):
                in_hours = True
                if (minute - opening) % restaurant['slot_minutes'] == 0:
                    on_grid = True
        if not in_hours:
            fail(422, 'outside_opening_hours')
        if not on_grid:
            fail(422, 'not_on_slot_grid')
        return {'restaurant_id': rid, 'table_id': table_id, 'party_size': party,
                'starts_at_local': wall.isoformat(timespec='minutes'),
                'starts_at': start.isoformat(timespec='seconds'), 'ends_at': end.isoformat(timespec='seconds')}

    @staticmethod
    def overlaps(a, b):
        return (a['restaurant_id'] == b['restaurant_id'] and a['table_id'] == b['table_id']
                and datetime.fromisoformat(a['starts_at']) < datetime.fromisoformat(b['ends_at'])
                and datetime.fromisoformat(b['starts_at']) < datetime.fromisoformat(a['ends_at']))

    def available(self, candidate, excluded=()):
        return all(not self.overlaps(candidate, r) for ref, r in self.state['reservations'].items()
                   if ref not in excluded and r['status'] == 'confirmed')

    def change_allowed(self, reservation):
        if reservation['status'] == 'cancelled':
            fail(409, 'reservation_cancelled')
        cutoff = self.restaurant(reservation['restaurant_id'])['cancellation_cutoff_minutes']
        if (datetime.fromisoformat(reservation['starts_at']) - datetime.now(UTC)).total_seconds() <= cutoff * 60:
            fail(409, 'cutoff_passed')

    def amended(self, reservation, fields):
        self.change_allowed(reservation)
        merged = {k: fields.get(k, reservation[k]) for k in ('table_id', 'starts_at_local', 'party_size')}
        result = copy.deepcopy(reservation)
        result.update(self.validated_booking(reservation['restaurant_id'], merged))
        return result

    def dispatch(self, method, path, query, headers, body):
        if method == 'GET' and path == '/health':
            return 200, {'status': 'ok'}
        if method == 'POST' and path in ('/_test/reset', '/_test/import'):
            self.object_body(body)
            if path.endswith('/reset'):
                replacement = self.fixture(body)
            else:
                replacement = self.import_state(body)
            self.state = replacement
            return 204, None
        if method == 'GET' and path == '/_test/export':
            return 200, {'track': 'tablekeeper', 'format_version': 1, 'state': copy.deepcopy(self.state)}
        if method == 'POST' and path in ('/auth/signup', '/auth/login'):
            self.object_body(body)
            email, password = string(body, 'email'), string(body, 'password')
            if not re.fullmatch(r'[^\s@]+@[^\s@]+', email):
                fail()
            existing = next((u for u in self.state['users'].values() if u['email'] == email), None)
            if path.endswith('/signup'):
                display = string(body, 'display_name')
                if len(password) < 8:
                    fail()
                if existing:
                    fail(409, 'email_taken')
                uid = 'u_' + secrets.token_hex(12)
                existing = {'id': uid, 'email': email, 'display_name': display, 'password_hash': password_hash(password)}
                self.state['users'][uid] = existing
                status = 201
            else:
                if existing is None or not password_matches(password, existing['password_hash']):
                    fail(401, 'unauthenticated')
                status = 200
            token = secrets.token_urlsafe(32)
            self.state['tokens'][token] = existing['id']
            return status, {'user_id': existing['id'], 'display_name': existing['display_name'], 'token': token}
        if method == 'GET' and path == '/restaurants':
            return 200, {'restaurants': [{k: r[k] for k in ('id', 'name', 'timezone')}
                                         for r in self.state['restaurants'].values()]}
        if method == 'GET' and re.fullmatch(r'/restaurants/[^/]+', path):
            return 200, copy.deepcopy(self.restaurant(path.split('/')[-1]))
        if method == 'GET' and path == '/availability':
            if any(k not in query or not query[k][0] for k in ('restaurant_id', 'date', 'party_size')):
                fail()
            day = local_date(query['date'][0])
            party_string = query['party_size'][0]
            if not re.fullmatch(r'[0-9]+', party_string) or len(party_string) > 100:
                fail()
            party = integer(int(party_string))
            restaurant = self.restaurant(query['restaurant_id'][0])
            slots, seen = [], set()
            for hours in restaurant['opening_hours']:
                if hours['weekday'] != DAYS[day.weekday()]:
                    continue
                for minute in range(clock_minutes(hours['opens']), clock_minutes(hours['closes']), restaurant['slot_minutes']):
                    wall = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
                    local = wall.isoformat(timespec='minutes')
                    if local in seen:
                        continue
                    zone = ZoneInfo(restaurant['timezone'])
                    try:
                        start = resolve(wall, zone)
                        end = (start.astimezone(UTC) + timedelta(minutes=restaurant['reservation_duration_minutes'])).astimezone(zone)
                        closing = resolve(datetime.combine(day, datetime.min.time()) + timedelta(minutes=clock_minutes(hours['closes'])), zone)
                        if end.astimezone(UTC) > closing.astimezone(UTC):
                            continue
                    except (APIError, OverflowError, ValueError):
                        continue
                    seen.add(local)
                    candidate = {'restaurant_id': restaurant['id'], 'starts_at': start.isoformat(), 'ends_at': end.isoformat()}
                    tables = []
                    for table in restaurant['tables']:
                        candidate['table_id'] = table['id']
                        if table['capacity'] >= party and self.available(candidate):
                            tables.append(table['id'])
                    slots.append({'starts_at_local': local, 'starts_at': start.isoformat(timespec='seconds'), 'available_table_ids': tables})
            slots.sort(key=lambda s: s['starts_at_local'])
            return 200, {'restaurant_id': restaurant['id'], 'date': day.isoformat(), 'timezone': restaurant['timezone'], 'slots': slots}
        uid = self.user(headers)
        if method == 'POST' and path in ('/reservations', '/reservation-moves'):
            self.object_body(body)
            receipt, serialized_body, replay = self.receipt(method, path, uid, headers, body)
            if replay is not None:
                return 200, replay
            if path == '/reservations':
                candidate = self.validated_booking(string(body, 'restaurant_id', identifier=True), body)
                if not self.available(candidate):
                    fail(409, 'table_unavailable')
                reference = secrets.token_hex(5).upper()
                while reference in self.state['reservations']:
                    reference = secrets.token_hex(5).upper()
                candidate.update(reservation_id='res_' + secrets.token_hex(12), reference=reference,
                                 status='confirmed', user_id=uid, created_at=datetime.now(UTC).isoformat(timespec='seconds'))
                candidates = [candidate]
                response = self.public(candidate)
            else:
                moves = body.get('moves')
                if not isinstance(moves, list) or not 1 <= len(moves) <= 8:
                    fail()
                references = []
                for move in moves:
                    if not isinstance(move, dict) or not isinstance(move.get('reference'), str) or not move['reference'] or len(move['reference']) > 64:
                        fail()
                    references.append(move['reference'])
                if len(set(references)) != len(references):
                    fail()
                candidates, rid = [], None
                for move in moves:
                    old = self.owned(move['reference'], uid)
                    if rid is not None and old['restaurant_id'] != rid:
                        fail()
                    rid = old['restaurant_id']
                    candidates.append(self.amended(old, move))
                for i, candidate in enumerate(candidates):
                    if not self.available(candidate, references) or any(self.overlaps(candidate, earlier) for earlier in candidates[:i]):
                        fail(409, 'table_unavailable')
                response = {'reservations': [self.public(r) for r in candidates]}
            # Prepare all potentially failing receipt work before the transaction commits.
            completed = {'body': serialized_body, 'response': copy.deepcopy(response)}
            for candidate in candidates:
                self.state['reservations'][candidate['reference']] = candidate
            self.state['receipts'][receipt] = completed
            return 201, response
        if method == 'GET' and path == '/reservations':
            reservations = [r for r in self.state['reservations'].values() if r['user_id'] == uid]
            reservations.sort(key=lambda r: datetime.fromisoformat(r['starts_at']), reverse=True)
            return 200, {'reservations': [self.public(r) for r in reservations]}
        match = re.fullmatch(r'/reservations/([^/]+)(/cancel)?', path)
        if match and ((method in ('GET', 'PATCH') and not match[2]) or (method == 'POST' and match[2])):
            reservation = self.owned(match[1], uid)
            if method == 'GET':
                return 200, self.public(reservation)
            if method == 'POST':
                if body is not None:
                    self.object_body(body)
                if reservation['status'] != 'cancelled':
                    self.change_allowed(reservation)
                    reservation['status'] = 'cancelled'
                return 200, self.public(reservation)
            self.object_body(body)
            candidate = self.amended(reservation, body)
            if not self.available(candidate, (reservation['reference'],)):
                fail(409, 'table_unavailable')
            self.state['reservations'][reservation['reference']] = candidate
            return 200, self.public(candidate)
        fail(404, 'not_found')

    def fixture(self, fixture):
        replacement = empty_state()
        for key in ('users', 'restaurants', 'reservations'):
            if key not in fixture:
                fail()
            if not isinstance(fixture[key], list):
                fail(400, 'malformed_request')
        for user in fixture['users']:
            self.object_body(user)
            uid = string(user, 'id', identifier=True)
            email, password, display = string(user, 'email'), string(user, 'password'), string(user, 'display_name')
            if uid in replacement['users'] or any(u['email'] == email for u in replacement['users'].values()):
                fail()
            if not re.fullmatch(r'[^\s@]+@[^\s@]+', email):
                fail()
            replacement['users'][uid] = {'id': uid, 'email': email, 'display_name': display, 'password_hash': password_hash(password)}
        for supplied in fixture['restaurants']:
            self.object_body(supplied)
            rid = string(supplied, 'id', identifier=True)
            restaurant = {k: string(supplied, k) for k in ('name', 'timezone')}
            restaurant['id'] = rid
            if rid in replacement['restaurants']:
                fail()
            try:
                ZoneInfo(restaurant['timezone'])
            except (ZoneInfoNotFoundError, ValueError):
                fail()
            for key in ('slot_minutes', 'reservation_duration_minutes', 'cancellation_cutoff_minutes'):
                if key not in supplied:
                    fail()
                if type(supplied[key]) is not int:
                    fail(400, 'malformed_request')
                restaurant[key] = integer(supplied[key], 0 if key == 'cancellation_cutoff_minutes' else 1)
            for key in ('opening_hours', 'tables'):
                if key not in supplied:
                    fail()
                if not isinstance(supplied[key], list):
                    fail(400, 'malformed_request')
                restaurant[key] = []
            for hours in supplied['opening_hours']:
                self.object_body(hours)
                clean = {k: string(hours, k) for k in ('weekday', 'opens', 'closes')}
                if clean['weekday'] not in DAYS or clock_minutes(clean['opens']) >= clock_minutes(clean['closes']):
                    fail()
                restaurant['opening_hours'].append(clean)
            table_ids = set()
            for table in supplied['tables']:
                self.object_body(table)
                tid = string(table, 'id', identifier=True)
                if tid in table_ids:
                    fail()
                table_ids.add(tid)
                if 'capacity' not in table:
                    fail()
                if type(table['capacity']) is not int:
                    fail(400, 'malformed_request')
                restaurant['tables'].append({'id': tid, 'label': string(table, 'label'), 'capacity': integer(table['capacity'])})
            replacement['restaurants'][rid] = restaurant
        validator = Service()
        validator.state = replacement
        for seeded in fixture['reservations']:
            self.object_body(seeded)
            uid = string(seeded, 'user_id', identifier=True)
            rid = string(seeded, 'restaurant_id', identifier=True)
            res_id, reference = string(seeded, 'id', identifier=True), string(seeded, 'reference')
            if uid not in replacement['users'] or reference in replacement['reservations'] or any(r['reservation_id'] == res_id for r in replacement['reservations'].values()):
                fail()
            if not re.fullmatch(r'[A-Z0-9]{6,12}', reference):
                fail()
            candidate = validator.validated_booking(rid, seeded)
            if not validator.available(candidate):
                fail()
            candidate.update(reservation_id=res_id, reference=reference, user_id=uid, status='confirmed',
                             created_at=datetime.now(UTC).isoformat(timespec='seconds'))
            replacement['reservations'][reference] = candidate
        return replacement

    def import_state(self, envelope):
        # Validate a detached copy completely before replacing destination state.
        try:
            if envelope.get('track') != 'tablekeeper' or type(envelope.get('format_version')) is not int or envelope['format_version'] != 1:
                fail()
            state = copy.deepcopy(envelope['state'])
            if not isinstance(state, dict) or set(state) != set(empty_state()) or any(not isinstance(v, dict) for v in state.values()):
                fail()
            emails = set()
            for uid, user in state['users'].items():
                if not isinstance(user, dict) or uid != string(user, 'id', identifier=True):
                    fail()
                email = string(user, 'email')
                string(user, 'display_name')
                if email in emails or not re.fullmatch(r'[^\s@]+@[^\s@]+', email):
                    fail()
                emails.add(email)
                if not re.fullmatch(r'pbkdf2_sha256\$160000\$[0-9a-f]{32}\$[0-9a-f]{64}', string(user, 'password_hash')) or 'password' in user:
                    fail()
            for token, uid in state['tokens'].items():
                if not isinstance(token, str) or not token or not isinstance(uid, str) or uid not in state['users']:
                    fail()
            # Reuse configuration validation without changing imported hashes or timestamps.
            configuration = self.fixture({'users': [], 'restaurants': list(state['restaurants'].values()), 'reservations': []})
            if configuration['restaurants'] != state['restaurants']:
                fail()
            validator = Service()
            validator.state = state
            ids = set()
            confirmed = []
            for ref, res in state['reservations'].items():
                if not isinstance(res, dict) or res.get('reference') != ref or not re.fullmatch(r'[A-Z0-9]{6,12}', ref):
                    fail()
                res_id = string(res, 'reservation_id', identifier=True)
                if res_id in ids or res.get('user_id') not in state['users'] or res.get('status') not in ('confirmed', 'cancelled'):
                    fail()
                ids.add(res_id)
                validated = validator.validated_booking(string(res, 'restaurant_id', identifier=True), res)
                if any(res.get(k) != v for k, v in validated.items()):
                    fail()
                created = datetime.fromisoformat(string(res, 'created_at'))
                if created.utcoffset() is None:
                    fail()
                if res['status'] == 'confirmed':
                    if any(self.overlaps(res, other) for other in confirmed):
                        fail()
                    confirmed.append(res)
            for index, receipt in state['receipts'].items():
                scope = parse_json(index)
                if not isinstance(scope, list) or len(scope) != 4 or scope[0] not in state['users'] or scope[1] != 'POST' or scope[2] not in ('/reservations', '/reservation-moves') or not isinstance(scope[3], str) or not 1 <= len(scope[3]) <= 255 or canonical(scope) != index:
                    fail()
                if not isinstance(receipt, dict) or set(receipt) != {'body', 'response'} or not isinstance(receipt['body'], str):
                    fail()
                request = parse_json(receipt['body'])
                if not isinstance(request, dict) or canonical(request) != receipt['body'] or not isinstance(receipt['response'], dict):
                    fail()
                responses = [receipt['response']] if scope[2] == '/reservations' else receipt['response'].get('reservations')
                if not isinstance(responses, list) or not responses:
                    fail()
                for response in responses:
                    if not isinstance(response, dict) or response.get('reference') not in state['reservations']:
                        fail()
                    current = state['reservations'][response['reference']]
                    if current['user_id'] != scope[0] or any(response.get(k) != current[k] for k in ('reference', 'reservation_id', 'restaurant_id', 'created_at')):
                        fail()
                    valid = validator.validated_booking(current['restaurant_id'], response)
                    if response.get('status') != 'confirmed' or any(response.get(k) != v for k, v in valid.items()):
                        fail()
            return state
        except (APIError, KeyError, ValueError, TypeError, OverflowError, AttributeError):
            fail()


SERVICE = Service()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def send_error(self, code, message=None, explain=None):
        # Parser-level HTTP errors must use the same JSON envelope as API errors.
        status = 404 if code == 501 else (400 if code >= 500 else code)
        error = 'not_found' if status == 404 else 'malformed_request'
        payload = json.dumps({'error': {'code': error, 'message': 'Invalid HTTP request'}}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if getattr(self, 'command', None) != 'HEAD':
            self.wfile.write(payload)

    def handle_api(self):
        try:
            body = None
            if self.command in ('POST', 'PATCH', 'PUT'):
                length = self.headers.get('Content-Length', '0')
                if not re.fullmatch(r'[0-9]+', length):
                    fail(400, 'malformed_request')
                size = int(length)
                raw = self.rfile.read(size)
                if raw:
                    try:
                        body = parse_json(raw.decode('utf-8'))
                    except (ValueError, UnicodeDecodeError, RecursionError):
                        fail(400, 'malformed_request')
                elif self.path.split('?')[0] != '/health' and not re.fullmatch(r'/reservations/[^/]+/cancel', self.path.split('?')[0]):
                    fail(400, 'malformed_request')
            status, response = SERVICE.request(self.command, self.path, dict(self.headers), body)
        except APIError as error:
            status, response = error.status, {'error': {'code': error.code, 'message': error.message}}
        except (ValueError, TypeError, OverflowError, RecursionError, KeyError):
            status, response = 400, {'error': {'code': 'malformed_request', 'message': 'Invalid request data'}}
        payload = b'' if response is None else json.dumps(response, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = handle_api

    def log_message(self, format, *args):
        # HTTP paths, headers, credentials and bodies are not written to logs.
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '8080'))), Handler).serve_forever()
