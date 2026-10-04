"""Tablekeeper reservations API. All state transitions share a single transaction lock."""
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
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
    return {key: {} for key in ('users', 'tokens', 'restaurants', 'reservations', 'receipts',
                                'policies', 'histories', 'series', 'restaurant_revisions')}


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
            previous = self.state
            if method != 'GET':
                # Writes operate on a private transaction image, including all metadata.
                self.state = copy.deepcopy(previous)
            try:
                return self.dispatch(method, path, query, headers, body)
            except APIError as error:
                self.state = previous
                return error.status, {'error': {'code': error.code, 'message': error.message}}
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                self.state = previous
                return 400, {'error': {'code': 'malformed_request', 'message': 'Invalid request data'}}

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

    @staticmethod
    def booking_fields(record):
        fields = {k: record[k] for k in ('starts_at_local', 'party_size')}
        fields['table_ids'] = record.get('table_ids', [record.get('table_id')])
        return fields

    def base_terms(self, rid):
        restaurant = self.restaurant(rid)
        return {'policy_version': 0, **{k: copy.deepcopy(restaurant[k]) for k in
                ('slot_minutes', 'reservation_duration_minutes', 'cancellation_cutoff_minutes', 'opening_hours')},
                'capacities': {t['id']: t['capacity'] for t in restaurant['tables']}}

    def selected_terms(self, rid, day):
        eligible = [p for p in self.state['policies'].get(rid, []) if p['effective_from'] <= day]
        if not eligible:
            return self.base_terms(rid)
        policy = max(eligible, key=lambda p: (p['effective_from'], p['policy_version']))
        return {k: copy.deepcopy(v) for k, v in policy.items() if k != 'effective_from'}

    def policy_body(self, rid, supplied):
        try:
            if not isinstance(supplied, dict):
                fail()
            effective = string(supplied, 'effective_from')
            local_date(effective)
            policy = {'effective_from': effective}
            for field, minimum, maximum in (('slot_minutes', 1, 1440),
                                           ('reservation_duration_minutes', 1, 1440),
                                           ('cancellation_cutoff_minutes', 0, 10080)):
                value = supplied.get(field)
                if type(value) is not int or not minimum <= value <= maximum:
                    fail()
                policy[field] = value
            hours = supplied.get('opening_hours')
            if not isinstance(hours, list):
                fail()
            policy['opening_hours'], weekdays = [], set()
            for item in hours:
                if not isinstance(item, dict):
                    fail()
                clean = {key: string(item, key) for key in ('weekday', 'opens', 'closes')}
                if clean['weekday'] not in DAYS or clean['weekday'] in weekdays or clock_minutes(clean['opens']) >= clock_minutes(clean['closes']):
                    fail()
                weekdays.add(clean['weekday'])
                policy['opening_hours'].append(clean)
            capacities = supplied.get('capacities')
            if not isinstance(capacities, dict) or set(capacities) != {t['id'] for t in self.restaurant(rid)['tables']}:
                fail()
            if any(type(value) is not int or not 1 <= value <= 100 for value in capacities.values()):
                fail()
            policy['capacities'] = copy.deepcopy(capacities)
            return policy
        except APIError:
            fail()

    @staticmethod
    def changes(old, new):
        changes = []
        if old is None or old['table_ids'] != new['table_ids']:
            field = 'table_ids' if len(new['table_ids']) > 1 or (old and len(old['table_ids']) > 1) else 'table_id'
            changes.append({'field': field, 'from': None if old is None else copy.deepcopy(old[field]),
                            'to': copy.deepcopy(new[field])})
        for field in ('starts_at_local', 'party_size'):
            if old is None or old[field] != new[field]:
                changes.append({'field': field, 'from': None if old is None else old[field], 'to': new[field]})
        return changes

    def history_entry(self, old, new, event, at=None):
        entries = self.state['histories'].setdefault(new['reference'], [])
        instant = datetime.fromisoformat(at) if at else datetime.now(UTC)
        if entries:
            instant = max(instant, datetime.fromisoformat(entries[-1]['at']))
        entries.append({'seq': len(entries) + 1, 'at': instant.isoformat(), 'event': event,
                        'changes': [] if event == 'cancelled' else self.changes(old, new),
                        'revision': new['revision'], 'accepted_terms': copy.deepcopy(new['accepted_terms'])})

    def series_for(self, reference):
        for series in self.state['series'].values():
            for occurrence in series['occurrences']:
                if occurrence['reference'] == reference:
                    return series, occurrence
        return None, None

    def series_response(self, series):
        return {'series_id': series['series_id'], 'revision': series['revision'],
                'interval_weeks': series['interval_weeks'],
                'occurrences': [{**copy.deepcopy(item), 'reservation': self.public(self.state['reservations'][item['reference']])}
                                for item in series['occurrences']]}

    def record_changes(self, candidates, event='changed'):
        affected_series, affected_restaurants = set(), set()
        for candidate in candidates:
            reference = candidate['reference']
            old = self.state['reservations'].get(reference)
            if old is not None and old['revision'] == candidate['revision']:
                continue
            self.state['reservations'][reference] = candidate
            self.history_entry(old, candidate, 'created' if old is None else event,
                               candidate['created_at'] if old is None else None)
            affected_restaurants.add(candidate['restaurant_id'])
            series, occurrence = self.series_for(reference)
            if series:
                affected_series.add(series['series_id'])
                if event == 'changed':
                    occurrence['exception'] = True
        for sid in affected_series:
            self.state['series'][sid]['revision'] += 1
        for rid in affected_restaurants:
            self.state['restaurant_revisions'][rid] += 1

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

    def selection_ids(self, rid, fields):
        if 'table_id' in fields and 'table_ids' in fields:
            fail()
        if 'table_ids' in fields:
            table_ids = fields['table_ids']
            if not isinstance(table_ids, list):
                fail(400, 'malformed_request')
            if not table_ids:
                fail()
            for tid in table_ids:
                string({'id': tid}, 'id', identifier=True)
            if len(set(table_ids)) != len(table_ids):
                fail()
            if len(table_ids) > 2:
                fail(422, 'combination_not_allowed')
        else:
            table_ids = [string(fields, 'table_id', identifier=True)]
        restaurant = self.restaurant(rid)
        tables = {t['id']: t for t in restaurant['tables']}
        if any(tid not in tables for tid in table_ids):
            fail(404, 'not_found')
        if len(table_ids) == 2:
            pair = next((p for p in restaurant['combinable'] if set(p) == set(table_ids)), None)
            if pair is None:
                fail(422, 'combination_not_allowed')
            table_ids = list(pair)
        return table_ids

    def validated_booking(self, rid, fields, terms=None):
        # Validate against the selected policy or an immutable accepted snapshot.
        table_ids = self.selection_ids(rid, fields)
        wall = local_datetime(string(fields, 'starts_at_local'))
        if 'party_size' not in fields:
            fail()
        party = integer(fields['party_size'])
        restaurant = self.restaurant(rid)
        terms = copy.deepcopy(terms if terms is not None else self.selected_terms(rid, wall.date().isoformat()))
        if party > sum(terms['capacities'][tid] for tid in table_ids):
            fail(422, 'party_exceeds_capacity')
        zone = ZoneInfo(restaurant['timezone'])
        start = resolve(wall, zone)
        try:
            end = (start.astimezone(UTC) + timedelta(minutes=terms['reservation_duration_minutes'])).astimezone(zone)
        except (OverflowError, ValueError):
            fail()
        intervals = [h for h in terms['opening_hours'] if h['weekday'] == DAYS[wall.weekday()]]
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
                if (minute - opening) % terms['slot_minutes'] == 0:
                    on_grid = True
        if not in_hours:
            fail(422, 'outside_opening_hours')
        if not on_grid:
            fail(422, 'not_on_slot_grid')
        result = {'restaurant_id': rid, 'table_ids': list(table_ids), 'party_size': party,
                'starts_at_local': wall.isoformat(timespec='minutes'),
                'starts_at': start.isoformat(timespec='seconds'), 'ends_at': end.isoformat(timespec='seconds'),
                'accepted_terms': terms}
        if len(table_ids) == 1:
            result['table_id'] = table_ids[0]
        return result

    @staticmethod
    def overlaps(a, b):
        return (a['restaurant_id'] == b['restaurant_id']
                and bool(set(a.get('table_ids', [a.get('table_id')])) & set(b.get('table_ids', [b.get('table_id')])))
                and datetime.fromisoformat(a['starts_at']) < datetime.fromisoformat(b['ends_at'])
                and datetime.fromisoformat(b['starts_at']) < datetime.fromisoformat(a['ends_at']))

    def available(self, candidate, excluded=()):
        return all(not self.overlaps(candidate, r) for ref, r in self.state['reservations'].items()
                   if ref not in excluded and r['status'] == 'confirmed')

    def change_allowed(self, reservation):
        if reservation['status'] == 'cancelled':
            fail(409, 'reservation_cancelled')
        cutoff = reservation['accepted_terms']['cancellation_cutoff_minutes']
        if (datetime.fromisoformat(reservation['starts_at']) - datetime.now(UTC)).total_seconds() <= cutoff * 60:
            fail(409, 'cutoff_passed')

    def amended(self, reservation, fields):
        if 'expected_revision' in fields:
            expected = integer(fields['expected_revision'])
            if expected != reservation['revision']:
                fail(409, 'stale_revision')
        self.change_allowed(reservation)
        merged = {k: fields.get(k, reservation[k]) for k in ('starts_at_local', 'party_size')}
        if 'table_id' in fields or 'table_ids' in fields:
            merged.update({k: fields[k] for k in ('table_id', 'table_ids') if k in fields})
        else:
            merged['table_ids'] = reservation['table_ids']
        ids = self.selection_ids(reservation['restaurant_id'], merged)
        local = local_datetime(string(merged, 'starts_at_local')).isoformat(timespec='minutes')
        party = integer(merged['party_size'])
        if ids == reservation['table_ids'] and local == reservation['starts_at_local'] and party == reservation['party_size']:
            return copy.deepcopy(reservation)
        result = copy.deepcopy(reservation)
        result.pop('table_id', None)
        result.update(self.validated_booking(reservation['restaurant_id'], merged))
        result['revision'] += 1
        return result

    def new_reservation(self, rid, fields, uid, reserved_references=()):
        candidate = self.validated_booking(rid, fields)
        reference = secrets.token_hex(5).upper()
        while reference in self.state['reservations'] or reference in reserved_references:
            reference = secrets.token_hex(5).upper()
        candidate.update(reservation_id='res_' + secrets.token_hex(12), reference=reference, revision=1,
                         status='confirmed', user_id=uid, created_at=datetime.now(UTC).isoformat(timespec='seconds'))
        return candidate

    def adopt(self, body, uid):
        count, interval = body.get('count'), body.get('interval_weeks')
        if type(count) is not int or not 2 <= count <= 12 or type(interval) is not int or not 1 <= interval <= 4:
            fail()
        anchor = self.owned(string(body, 'anchor_reference', identifier=True), uid)
        if anchor['status'] == 'cancelled':
            fail(409, 'reservation_cancelled')
        if self.series_for(anchor['reference'])[0]:
            fail(409, 'already_in_series')
        self.change_allowed(anchor)
        wall = local_datetime(anchor['starts_at_local'])
        candidates = []
        for index in range(1, count):
            try:
                next_wall = wall + timedelta(days=index * interval * 7)
            except OverflowError:
                fail()
            fields = {'table_ids': anchor['table_ids'], 'party_size': anchor['party_size'],
                      'starts_at_local': next_wall.isoformat(timespec='minutes')}
            candidate = self.new_reservation(anchor['restaurant_id'], fields, uid, [r['reference'] for r in candidates])
            if not self.available(candidate) or any(self.overlaps(candidate, old) for old in candidates):
                fail(409, 'table_unavailable')
            candidates.append(candidate)
        sid = 'series_' + secrets.token_hex(12)
        series = {'series_id': sid, 'user_id': uid, 'restaurant_id': anchor['restaurant_id'],
                  'revision': 1, 'interval_weeks': interval,
                  'occurrences': [{'index': index, 'reference': res['reference'], 'exception': False}
                                  for index, res in enumerate([anchor, *candidates])]}
        for candidate in candidates:
            self.state['reservations'][candidate['reference']] = candidate
            self.history_entry(None, candidate, 'created', candidate['created_at'])
        self.state['series'][sid] = series
        self.state['restaurant_revisions'][anchor['restaurant_id']] += 1
        return self.series_response(series)

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
        policy_route = re.fullmatch(r'/restaurants/([^/]+)/policies', path)
        if method == 'GET' and policy_route:
            restaurant = self.restaurant(policy_route[1])
            return 200, {'policies': copy.deepcopy(self.state['policies'][restaurant['id']])}
        private = re.fullmatch(r'/reservations/([^/]+)/(history|decision)', path)
        series_route = re.fullmatch(r'/series/([^/]+)', path)
        if method == 'GET' and (private or series_route):
            try:
                uid = self.user(headers)
            except APIError:
                fail(404, 'not_found')
            if private:
                reservation = self.owned(private[1], uid)
                if private[2] == 'history':
                    return 200, {'reference': reservation['reference'], 'entries': copy.deepcopy(self.state['histories'][reservation['reference']])}
                return 200, {k: copy.deepcopy(reservation[k]) for k in ('reference', 'revision', 'accepted_terms')}
            sid = series_route[1]
            if not 1 <= len(sid) <= 64:
                fail()
            series = self.state['series'].get(sid)
            if not series or series['user_id'] != uid:
                fail(404, 'not_found')
            return 200, self.series_response(series)
        if method == 'GET' and path == '/availability':
            if 'explain' in query and query['explain'] != ['true']:
                fail()
            if any(k not in query or not query[k][0] for k in ('restaurant_id', 'date', 'party_size')):
                fail()
            day = local_date(query['date'][0])
            party_string = query['party_size'][0]
            if not re.fullmatch(r'[0-9]+', party_string) or len(party_string) > 100:
                fail()
            party = integer(int(party_string))
            restaurant = self.restaurant(query['restaurant_id'][0])
            terms = self.selected_terms(restaurant['id'], day.isoformat())
            slots, seen = [], set()
            for hours in terms['opening_hours']:
                if hours['weekday'] != DAYS[day.weekday()]:
                    continue
                for minute in range(clock_minutes(hours['opens']), clock_minutes(hours['closes']), terms['slot_minutes']):
                    wall = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)
                    local = wall.isoformat(timespec='minutes')
                    if local in seen:
                        continue
                    zone = ZoneInfo(restaurant['timezone'])
                    try:
                        start = resolve(wall, zone)
                        end = (start.astimezone(UTC) + timedelta(minutes=terms['reservation_duration_minutes'])).astimezone(zone)
                        closing = resolve(datetime.combine(day, datetime.min.time()) + timedelta(minutes=clock_minutes(hours['closes'])), zone)
                        if end.astimezone(UTC) > closing.astimezone(UTC):
                            continue
                    except (APIError, OverflowError, ValueError):
                        continue
                    seen.add(local)
                    candidate = {'restaurant_id': restaurant['id'], 'starts_at': start.isoformat(), 'ends_at': end.isoformat()}
                    tables, options, explanations = [], [], []
                    for table in restaurant['tables']:
                        candidate['table_ids'] = [table['id']]
                        capacity_holds = terms['capacities'][table['id']] >= party
                        overlap_holds = self.available(candidate)
                        explanations.append({'table_id': table['id'], 'policy_version': terms['policy_version'],
                                             'available': capacity_holds and overlap_holds,
                                             'rules': [{'rule': 'capacity', 'holds': capacity_holds},
                                                       {'rule': 'no_overlap', 'holds': overlap_holds}]})
                        if capacity_holds and overlap_holds:
                            tables.append(table['id'])
                            options.append({'table_ids': [table['id']], 'capacity': terms['capacities'][table['id']]})
                    capacities = terms['capacities']
                    for pair in restaurant['combinable']:
                        capacity = sum(capacities[tid] for tid in pair)
                        candidate['table_ids'] = pair
                        if capacity >= party and self.available(candidate):
                            options.append({'table_ids': list(pair), 'capacity': capacity})
                    slot = {'starts_at_local': local, 'starts_at': start.isoformat(timespec='seconds'),
                            'available_table_ids': tables, 'available_options': options}
                    if 'explain' in query:
                        slot['explain'] = explanations
                    slots.append(slot)
            slots.sort(key=lambda s: s['starts_at_local'])
            return 200, {'restaurant_id': restaurant['id'], 'date': day.isoformat(), 'timezone': restaurant['timezone'], 'slots': slots}
        uid = self.user(headers)
        if method == 'POST' and (policy_route or path == '/series'):
            self.object_body(body)
            receipt, serialized_body, replay = self.receipt(method, path, uid, headers, body)
            if replay is not None:
                return 200, replay
            if policy_route:
                restaurant = self.restaurant(policy_route[1])
                if uid not in restaurant['manager_user_ids']:
                    fail(403, 'forbidden')
                policy = self.policy_body(restaurant['id'], body)
                policy['policy_version'] = len(self.state['policies'][restaurant['id']]) + 1
                self.state['policies'][restaurant['id']].append(policy)
                self.state['restaurant_revisions'][restaurant['id']] += 1
                response = copy.deepcopy(policy)
            else:
                response = self.adopt(body, uid)
            self.state['receipts'][receipt] = {'body': serialized_body, 'response': copy.deepcopy(response)}
            return 201, response
        if method == 'POST' and path in ('/reservations', '/reservation-moves'):
            self.object_body(body)
            receipt, serialized_body, replay = self.receipt(method, path, uid, headers, body)
            if replay is not None:
                return 200, replay
            if path == '/reservations':
                candidate = self.new_reservation(string(body, 'restaurant_id', identifier=True), body, uid)
                if not self.available(candidate):
                    fail(409, 'table_unavailable')
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
            self.record_changes(candidates)
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
                    candidate = copy.deepcopy(reservation)
                    candidate['status'] = 'cancelled'
                    candidate['revision'] += 1
                    self.record_changes([candidate], 'cancelled')
                    reservation = candidate
                return 200, self.public(reservation)
            self.object_body(body)
            candidate = self.amended(reservation, body)
            if not self.available(candidate, (reservation['reference'],)):
                fail(409, 'table_unavailable')
            self.record_changes([candidate])
            return 200, self.public(candidate)
        fail(404, 'not_found')

    def fixture(self, fixture, known_users=()):
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
            combinable = supplied.get('combinable', [])
            if not isinstance(combinable, list):
                fail(400, 'malformed_request')
            restaurant['combinable'] = []
            for pair in combinable:
                if not isinstance(pair, list):
                    fail(400, 'malformed_request')
                if len(pair) != 2:
                    fail()
                for tid in pair:
                    string({'id': tid}, 'id', identifier=True)
                if pair[0] == pair[1] or any(tid not in table_ids for tid in pair):
                    fail()
                restaurant['combinable'].append(list(pair))
            managers = supplied.get('manager_user_ids', [])
            if not isinstance(managers, list):
                fail(400, 'malformed_request')
            for manager in managers:
                string({'id': manager}, 'id', identifier=True)
                if manager not in replacement['users'] and manager not in known_users:
                    fail()
            restaurant['manager_user_ids'] = list(managers)
            replacement['restaurants'][rid] = restaurant
            replacement['policies'][rid] = []
            replacement['restaurant_revisions'][rid] = 1
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
            status = seeded.get('status', 'confirmed')
            if not isinstance(status, str):
                fail(400, 'malformed_request')
            if status not in ('confirmed', 'cancelled'):
                fail()
            if status == 'confirmed' and not validator.available(candidate):
                fail()
            candidate.update(reservation_id=res_id, reference=reference, user_id=uid, status=status, revision=1,
                             created_at=datetime.now(UTC).isoformat(timespec='seconds'))
            replacement['reservations'][reference] = candidate
            validator.history_entry(None, candidate, 'created', candidate['created_at'])
            if status == 'cancelled':
                validator.history_entry(candidate, candidate, 'cancelled', candidate['created_at'])
        return replacement

    def checked_terms(self, rid, terms):
        if not isinstance(terms, dict) or type(terms.get('policy_version')) is not int:
            fail()
        version = terms['policy_version']
        if version == 0:
            expected = self.base_terms(rid)
        else:
            policy = next((p for p in self.state['policies'][rid] if p['policy_version'] == version), None)
            if policy is None:
                fail()
            expected = {k: v for k, v in policy.items() if k != 'effective_from'}
        if canonical(terms) != canonical(expected):
            fail()
        return copy.deepcopy(expected)

    def checked_history(self, reservation):
        entries = self.state['histories'].get(reservation['reference'])
        if not isinstance(entries, list) or not entries:
            fail()
        reconstructed, previous_at = None, None
        for seq, entry in enumerate(entries, 1):
            if not isinstance(entry, dict) or type(entry.get('seq')) is not int or entry['seq'] != seq:
                fail()
            at = datetime.fromisoformat(string(entry, 'at'))
            if at.utcoffset() is None or (previous_at and at < previous_at):
                fail()
            previous_at = at
            revision = integer(entry.get('revision'))
            terms = self.checked_terms(reservation['restaurant_id'], entry.get('accepted_terms'))
            changes = entry.get('changes')
            if not isinstance(changes, list):
                fail()
            event = entry.get('event')
            if seq == 1:
                if event != 'created' or revision != 1:
                    fail()
                fields = {}
                for change in changes:
                    if not isinstance(change, dict) or change.get('from') is not None:
                        fail()
                    fields[change['field']] = change['to']
                reconstructed = {**reservation, **self.validated_booking(reservation['restaurant_id'], fields, terms),
                                 'status': 'confirmed', 'revision': revision}
                if canonical(changes) != canonical(self.changes(None, reconstructed)):
                    fail()
            elif event == 'cancelled':
                seeded_cancel = seq == 2 and revision == 1 and reconstructed['revision'] == 1
                if reconstructed['status'] != 'confirmed' or changes or not (revision == reconstructed['revision'] + 1 or seeded_cancel):
                    fail()
                if terms != reconstructed['accepted_terms']:
                    fail()
                reconstructed = {**reconstructed, 'status': 'cancelled', 'revision': revision}
            elif event == 'changed':
                if reconstructed['status'] != 'confirmed' or not changes or revision != reconstructed['revision'] + 1:
                    fail()
                fields = self.booking_fields(reconstructed)
                for change in changes:
                    if not isinstance(change, dict) or change.get('field') not in ('table_id', 'table_ids', 'starts_at_local', 'party_size'):
                        fail()
                    field = change['field']
                    if change.get('from') != reconstructed.get(field):
                        fail()
                    if field == 'table_id':
                        fields.pop('table_ids', None)
                    fields[field] = change['to']
                updated = {**reconstructed, **self.validated_booking(reservation['restaurant_id'], fields, terms), 'revision': revision}
                if len(updated['table_ids']) > 1:
                    updated.pop('table_id', None)
                if canonical(changes) != canonical(self.changes(reconstructed, updated)):
                    fail()
                reconstructed = updated
            else:
                fail()
        for key in ('table_ids', 'starts_at_local', 'party_size', 'starts_at', 'ends_at', 'status', 'revision', 'accepted_terms'):
            if reconstructed[key] != reservation[key]:
                fail()

    def import_state(self, envelope):
        # Validate a detached copy completely before replacing destination state.
        try:
            if envelope.get('track') != 'tablekeeper' or type(envelope.get('format_version')) is not int or envelope['format_version'] != 1:
                fail()
            state = copy.deepcopy(envelope['state'])
            legacy_keys = {'users', 'tokens', 'restaurants', 'reservations', 'receipts'}
            if not isinstance(state, dict) or set(state) not in (legacy_keys, set(empty_state())) or any(not isinstance(v, dict) for v in state.values()):
                fail()
            legacy = set(state) == legacy_keys
            if legacy:
                state.update({key: {} for key in set(empty_state()) - legacy_keys})
            # Upgrade stage-1 configuration and records; original receipt values stay intact.
            for restaurant in state['restaurants'].values():
                if not isinstance(restaurant, dict):
                    fail()
                restaurant.setdefault('combinable', [])
                if legacy:
                    restaurant['manager_user_ids'] = []
                    state['policies'][restaurant['id']] = []
                    state['restaurant_revisions'][restaurant['id']] = 1
            for reservation in state['reservations'].values():
                if not isinstance(reservation, dict):
                    fail()
                if 'table_ids' not in reservation and 'table_id' in reservation:
                    reservation['table_ids'] = [reservation['table_id']]
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
            configuration = self.fixture({'users': [], 'restaurants': list(state['restaurants'].values()), 'reservations': []}, state['users'])
            if configuration['restaurants'] != state['restaurants']:
                fail()
            validator = Service()
            validator.state = state
            if set(state['policies']) != set(state['restaurants']) or set(state['restaurant_revisions']) != set(state['restaurants']):
                fail()
            for rid, policies in state['policies'].items():
                if not isinstance(policies, list):
                    fail()
                integer(state['restaurant_revisions'][rid])
                for version, policy in enumerate(policies, 1):
                    if not isinstance(policy, dict) or type(policy.get('policy_version')) is not int or policy['policy_version'] != version:
                        fail()
                    if {**validator.policy_body(rid, policy), 'policy_version': version} != policy:
                        fail()
            ids = set()
            confirmed = []
            for ref, res in state['reservations'].items():
                if not isinstance(res, dict) or res.get('reference') != ref or not re.fullmatch(r'[A-Z0-9]{6,12}', ref):
                    fail()
                res_id = string(res, 'reservation_id', identifier=True)
                if res_id in ids or res.get('user_id') not in state['users'] or res.get('status') not in ('confirmed', 'cancelled'):
                    fail()
                ids.add(res_id)
                if legacy:
                    res['accepted_terms'] = validator.base_terms(res['restaurant_id'])
                    res['revision'] = 1
                integer(res.get('revision'))
                terms = validator.checked_terms(res['restaurant_id'], res.get('accepted_terms'))
                validated = validator.validated_booking(string(res, 'restaurant_id', identifier=True), self.booking_fields(res), terms)
                if any(res.get(k) != v for k, v in validated.items()):
                    fail()
                if len(res['table_ids']) > 1 and 'table_id' in res:
                    fail()
                created = datetime.fromisoformat(string(res, 'created_at'))
                if created.utcoffset() is None:
                    fail()
                if res['status'] == 'confirmed':
                    if any(self.overlaps(res, other) for other in confirmed):
                        fail()
                    confirmed.append(res)
                if legacy:
                    validator.history_entry(None, res, 'created', res['created_at'])
                    if res['status'] == 'cancelled':
                        validator.history_entry(res, res, 'cancelled', res['created_at'])
                validator.checked_history(res)
            if set(state['histories']) != set(state['reservations']):
                fail()
            adopted = set()
            for sid, series in state['series'].items():
                if not isinstance(series, dict) or sid != string(series, 'series_id', identifier=True) or series.get('user_id') not in state['users'] or series.get('restaurant_id') not in state['restaurants']:
                    fail()
                integer(series.get('revision'))
                if type(series.get('interval_weeks')) is not int or not 1 <= series['interval_weeks'] <= 4:
                    fail()
                occurrences = series.get('occurrences')
                if not isinstance(occurrences, list) or not 2 <= len(occurrences) <= 12:
                    fail()
                for index, item in enumerate(occurrences):
                    if not isinstance(item, dict) or type(item.get('index')) is not int or item['index'] != index or type(item.get('exception')) is not bool or item.get('reference') not in state['reservations'] or item['reference'] in adopted:
                        fail()
                    res = state['reservations'][item['reference']]
                    if res['user_id'] != series['user_id'] or res['restaurant_id'] != series['restaurant_id']:
                        fail()
                    adopted.add(item['reference'])
            for index, receipt in state['receipts'].items():
                scope = parse_json(index)
                if not isinstance(scope, list) or len(scope) != 4 or scope[0] not in state['users'] or scope[1] != 'POST' or not isinstance(scope[2], str) or not (scope[2] in ('/reservations', '/reservation-moves', '/series') or re.fullmatch(r'/restaurants/[^/]+/policies', scope[2])) or not isinstance(scope[3], str) or not 1 <= len(scope[3]) <= 255 or canonical(scope) != index:
                    fail()
                if not isinstance(receipt, dict) or set(receipt) != {'body', 'response'} or not isinstance(receipt['body'], str):
                    fail()
                request = parse_json(receipt['body'])
                if not isinstance(request, dict) or canonical(request) != receipt['body'] or not isinstance(receipt['response'], dict):
                    fail()
                policy_route = re.fullmatch(r'/restaurants/([^/]+)/policies', scope[2])
                if policy_route:
                    policy = receipt['response']
                    if policy_route[1] not in state['restaurants'] or policy not in state['policies'][policy_route[1]]:
                        fail()
                    continue
                if scope[2] == '/series':
                    saved = receipt['response']
                    if saved.get('series_id') not in state['series'] or saved.get('revision') != 1:
                        fail()
                    series = state['series'][saved['series_id']]
                    if series['user_id'] != scope[0] or saved.get('interval_weeks') != series['interval_weeks'] or not isinstance(saved.get('occurrences'), list) or len(saved['occurrences']) != len(series['occurrences']):
                        fail()
                    responses = []
                    for occurrence, current_occurrence in zip(saved['occurrences'], series['occurrences']):
                        if not isinstance(occurrence, dict) or occurrence.get('index') != current_occurrence['index'] or occurrence.get('reference') != current_occurrence['reference'] or occurrence.get('exception') is not False:
                            fail()
                        responses.append(occurrence.get('reservation'))
                else:
                    responses = [receipt['response']] if scope[2] == '/reservations' else receipt['response'].get('reservations')
                if not isinstance(responses, list) or not responses:
                    fail()
                for response in responses:
                    if not isinstance(response, dict) or response.get('reference') not in state['reservations']:
                        fail()
                    current = state['reservations'][response['reference']]
                    if current['user_id'] != scope[0] or any(response.get(k) != current[k] for k in ('reference', 'reservation_id', 'restaurant_id', 'created_at')):
                        fail()
                    if 'revision' in response and integer(response['revision']) > current['revision']:
                        fail()
                    terms = validator.checked_terms(current['restaurant_id'], response.get('accepted_terms', validator.base_terms(current['restaurant_id'])))
                    valid = validator.validated_booking(current['restaurant_id'], self.booking_fields(response), terms)
                    normalized = {**response, 'table_ids': response.get('table_ids', [response.get('table_id')]), 'accepted_terms': terms}
                    if response.get('status') != 'confirmed' or any(normalized.get(k) != v for k, v in valid.items()):
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
            path = urlsplit(self.path).path
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/signup': ('index.html', 'text/html; charset=utf-8'),
                      '/login': ('index.html', 'text/html; charset=utf-8'),
                      '/lookup': ('index.html', 'text/html; charset=utf-8'),
                      '/assets/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/assets/app.css': ('app.css', 'text/css; charset=utf-8')}
            if self.command in ('GET', 'HEAD') and path in assets:
                filename, content_type = assets[path]
                payload = (Path(__file__).parent / 'static' / filename).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(payload)
                return
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
