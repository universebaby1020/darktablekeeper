"""Specification-driven state and HTTP checks; no product fixtures or shipped tests."""
import copy
import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import service


def fixture(zone='Europe/Berlin', date='2090-01-05', opening='18:00', closing='23:00', duration=90):
    weekday = service.DAYS[datetime.fromisoformat(date).weekday()]
    return {'users': [{'id': 'ada', 'email': 'ada@example.com', 'password': 'correct horse', 'display_name': 'Ada'},
                      {'id': 'bob', 'email': 'bob@example.com', 'password': 'correct horse', 'display_name': 'Bob'}],
            'restaurants': [{'id': 'r1', 'name': 'Anker', 'timezone': zone, 'slot_minutes': 30,
                             'reservation_duration_minutes': duration, 'cancellation_cutoff_minutes': 120,
                             'opening_hours': [{'weekday': weekday, 'opens': opening, 'closes': closing}],
                             'tables': [{'id': 't1', 'label': '1', 'capacity': 4}, {'id': 't2', 'label': '2', 'capacity': 4}]}],
            'reservations': []}


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.api = service.Service()
        self.assertEqual(self.api.request('POST', '/_test/reset', body=fixture())[0], 204)
        _, login = self.api.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})
        self.headers = {'Authorization': 'Bearer ' + login['token']}
        self.body = {'restaurant_id': 'r1', 'table_id': 't1', 'starts_at_local': '2090-01-05T19:00', 'party_size': 4}

    def book(self, body=None, key='booking'):
        return self.api.request('POST', '/reservations', {**self.headers, 'Idempotency-Key': key}, body or self.body)

    def error(self, result, status, code):
        self.assertEqual(result[0], status)
        self.assertEqual(result[1]['error']['code'], code)

    def http_write(self, path, raw, key):
        request = ('POST ' + path + ' HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                   'Authorization: ' + self.headers['Authorization'] + '\r\nIdempotency-Key: ' + key +
                   '\r\nContent-Length: ' + str(len(raw)) + '\r\n\r\n').encode() + raw
        connection = MemoryConnection(request)
        with patch.object(service, 'SERVICE', self.api):
            service.Handler(connection, ('127.0.0.1', 1234), None)
        head, body = connection.writer.getvalue().split(b'\r\n\r\n', 1)
        return int(head.split(b' ')[1]), json.loads(body)

    def test_large_json_number_in_unknown_create_field_commits_with_receipt(self):
        raw = json.dumps(self.body).encode()[:-1] + b', "extra": 1e309}'
        status, original = self.http_write('/reservations', raw, 'large')
        self.assertEqual(status, 201)
        self.assertEqual(len(self.api.state['reservations']), 1)
        self.assertEqual(len(self.api.state['receipts']), 1)
        self.assertEqual(self.http_write('/reservations', raw.replace(b'1e309', b'10e308'), 'large'), (200, original))
        before = copy.deepcopy(self.api.state)
        self.error(self.http_write('/reservations', raw.replace(b'1e309', b'1e310'), 'large'), 409, 'idempotency_key_reuse')
        self.assertTrue(self.api.state == before)
        exported = self.api.request('GET', '/_test/export')[1]
        self.assertEqual(self.api.request('POST', '/_test/import', body=exported), (204, None))
        self.assertEqual(self.http_write('/reservations', raw, 'large'), (200, original))

    def test_large_json_number_in_unknown_move_field_commits_with_receipt(self):
        original = self.book()[1]
        raw = json.dumps({'moves': [{'reference': original['reference'], 'table_id': 't2'}]}).encode()[:-1] + b', "extra": 1e309}'
        status, moved = self.http_write('/reservation-moves', raw, 'large-move')
        self.assertEqual(status, 201)
        self.assertEqual(moved['reservations'][0]['table_id'], 't2')
        self.assertEqual(len(self.api.state['receipts']), 2)
        self.assertEqual(self.http_write('/reservation-moves', raw, 'large-move'), (200, moved))
        exported = self.api.request('GET', '/_test/export')[1]
        self.assertEqual(self.api.request('POST', '/_test/import', body=exported), (204, None))
        self.assertEqual(self.http_write('/reservation-moves', raw, 'large-move'), (200, moved))

    def test_invalid_json_numeric_constants_and_invalid_changes_leave_state_unchanged(self):
        for constant in (b'NaN', b'Infinity', b'-Infinity', b'1e999999999999999999999999'):
            raw = json.dumps(self.body).encode()[:-1] + b', "extra": ' + constant + b'}'
            before = copy.deepcopy(self.api.state)
            self.error(self.http_write('/reservations', raw, 'invalid'), 400, 'malformed_request')
            self.assertTrue(self.api.state == before)
        original = self.book(key='invalid')[1]
        invalid = json.dumps({'moves': [{'reference': original['reference'], 'party_size': False}]}).encode()[:-1] + b', "extra": 1e309}'
        before = copy.deepcopy(self.api.state)
        self.error(self.http_write('/reservation-moves', invalid, 'invalid-move'), 422, 'validation_failed')
        self.assertTrue(self.api.state == before)
        for constant in (b'NaN', b'Infinity', b'-Infinity', b'1e999999999999999999999999'):
            self.error(self.http_write('/reservation-moves', invalid.replace(b'1e309', constant), 'invalid-move'), 400, 'malformed_request')
            self.assertTrue(self.api.state == before)
        valid = json.dumps({'moves': [{'reference': original['reference'], 'table_id': 't2'}]}).encode()
        self.assertEqual(self.http_write('/reservation-moves', valid, 'invalid-move')[0], 201)

    def test_json_numeric_value_identity_handles_precision_underflow_and_boolean_types(self):
        raw = json.dumps(self.body).encode()[:-1] + b', "extra": {"small": 1e-400, "precise": 1.0000000000000000001}}'
        status, original = self.http_write('/reservations', raw, 'precise')
        self.assertEqual(status, 201)
        equivalent = raw.replace(b'1e-400', b'10e-401').replace(b'1.0000000000000000001', b'1.00000000000000000010')
        self.assertEqual(self.http_write('/reservations', equivalent, 'precise'), (200, original))
        before = copy.deepcopy(self.api.state)
        for inconsistent in (raw.replace(b'1e-400', b'0'), raw.replace(b'1.0000000000000000001', b'1.0000000000000000002'), raw.replace(b'1e-400', b'true')):
            self.error(self.http_write('/reservations', inconsistent, 'precise'), 409, 'idempotency_key_reuse')
            self.assertTrue(self.api.state == before)

    def test_public_browsing_authentication_and_account_hashing(self):
        self.assertEqual(self.api.request('GET', '/health'), (200, {'status': 'ok'}))
        self.assertEqual(self.api.request('GET', '/restaurants')[0], 200)
        self.assertEqual(self.api.request('GET', '/restaurants/r1')[0], 200)
        slots = self.api.request('GET', '/availability?restaurant_id=r1&date=2090-01-05&party_size=4')[1]['slots']
        self.assertEqual(len(slots), 8)
        self.assertEqual(slots[0]['available_table_ids'], ['t1', 't2'])
        self.error(self.api.request('GET', '/reservations'), 401, 'unauthenticated')
        result = self.api.request('POST', '/auth/signup', body={'email': 'new@example.com', 'password': 'long password', 'display_name': 'New', 'ignored': True})
        self.assertEqual(result[0], 201)
        self.assertEqual(self.api.request('POST', '/auth/login', body={'email': 'new@example.com', 'password': 'long password'})[0], 200)
        self.error(self.api.request('POST', '/auth/signup', body={'email': 'new@example.com', 'password': 'long password', 'display_name': 'New'}), 409, 'email_taken')
        self.error(self.api.request('POST', '/auth/login', body={'email': 'new@example.com', 'password': 'wrong password'}), 401, 'unauthenticated')
        for u in self.api.state['users'].values():
            self.assertNotIn('password', u)
            self.assertTrue(u['password_hash'].startswith('pbkdf2_sha256$'))

    def test_concurrent_identical_requests_create_once_and_replay_original(self):
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda _: self.book(), range(50)))
        self.assertEqual(sum(r[0] == 201 for r in results), 1)
        self.assertEqual(sum(r[0] == 200 for r in results), 49)
        self.assertTrue(all(r[1] == results[0][1] for r in results))
        self.assertEqual(len(self.api.state['reservations']), 1)
        ref = results[0][1]['reference']
        self.assertEqual(self.api.request('POST', '/reservations/' + ref + '/cancel', self.headers)[0], 200)
        self.assertEqual(self.book(), (200, results[0][1]))

    def test_concurrent_distinct_keys_prevent_overlap_half_open_boundary(self):
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda n: self.book(key=str(n)), range(50)))
        self.assertEqual(sum(r[0] == 201 for r in results), 1)
        self.assertEqual(sum(r[0] == 409 and r[1]['error']['code'] == 'table_unavailable' for r in results), 49)
        self.assertEqual(self.book({**self.body, 'starts_at_local': '2090-01-05T20:30'}, 'adjacent')[0], 201)
        self.assertEqual(len(self.api.state['reservations']), 2)

    def test_validation_and_retry_precedence_failed_keys_are_reusable(self):
        self.error(self.api.request('POST', '/reservations', self.headers, self.body), 400, 'missing_idempotency_key')
        self.error(self.book(key='x' * 256), 422, 'validation_failed')
        for party in (True, '4', 0, -1, 1.5):
            self.error(self.book({**self.body, 'party_size': party}), 422, 'validation_failed')
        self.error(self.book({**self.body, 'starts_at_local': '2090-01-05T19:00Z'}), 422, 'validation_failed')
        self.error(self.book({**self.body, 'starts_at_local': '2090-01-05T19:00:00'}), 422, 'validation_failed')
        self.error(self.book({**self.body, 'table_id': 1}), 400, 'malformed_request')
        self.error(self.book({**self.body, 'starts_at_local': '2090-01-05T19:15'}), 422, 'not_on_slot_grid')
        self.error(self.book({**self.body, 'starts_at_local': '2090-01-05T22:00'}), 422, 'outside_opening_hours')
        self.error(self.book({**self.body, 'party_size': 5}), 422, 'party_exceeds_capacity')
        self.error(self.book({**self.body, 'table_id': 'missing'}), 404, 'not_found')
        self.assertEqual(self.book()[0], 201)
        self.error(self.book({'party_size': False}), 409, 'idempotency_key_reuse')
        reversed_body = dict(reversed(list(self.body.items())))
        self.assertEqual(self.book(reversed_body)[0], 200)
        for party in ('+4', '4.0', '1e9', '-1', '0'):
            self.error(self.api.request('GET', '/availability?restaurant_id=r1&date=2090-01-05&party_size=' + party), 422, 'validation_failed')
        self.error(self.api.request('GET', '/availability?restaurant_id=r1&date=2090-02-30&party_size=4'), 422, 'validation_failed')

    def test_cancel_and_amend_preserve_identity_and_failed_occupancy(self):
        first = self.book()[1]
        second = self.book({**self.body, 'table_id': 't2'}, 'second')[1]
        before = copy.deepcopy(self.api.state)
        self.error(self.api.request('PATCH', '/reservations/' + first['reference'], self.headers, {'table_id': 't2'}), 409, 'table_unavailable')
        self.assertEqual(self.api.state, before)
        status, changed = self.api.request('PATCH', '/reservations/' + first['reference'], self.headers, {'party_size': 2, 'starts_at_local': '2090-01-05T20:30'})
        self.assertEqual(status, 200)
        for k in ('reference', 'reservation_id', 'created_at'):
            self.assertEqual(changed[k], first[k])
        self.assertEqual(self.api.request('POST', '/reservations/' + first['reference'] + '/cancel', self.headers)[0], 200)
        self.assertEqual(self.api.request('POST', '/reservations/' + first['reference'] + '/cancel', self.headers)[0], 200)
        self.error(self.api.request('PATCH', '/reservations/' + first['reference'], self.headers, {}), 409, 'reservation_cancelled')
        listing = self.api.request('GET', '/reservations', self.headers)[1]['reservations']
        self.assertEqual([r['reference'] for r in listing], [first['reference'], second['reference']])
        _, bob = self.api.request('POST', '/auth/login', body={'email': 'bob@example.com', 'password': 'correct horse'})
        bob_headers = {'Authorization': 'Bearer ' + bob['token']}
        self.error(self.api.request('GET', '/reservations/' + first['reference'], bob_headers), 404, 'not_found')
        self.assertEqual(self.api.request('POST', '/reservations', {**bob_headers, 'Idempotency-Key': 'booking'}, self.body)[0], 201)

    def test_batch_swap_atomicity_validation_order_and_noop(self):
        a = self.book()[1]
        b = self.book({**self.body, 'table_id': 't2'}, 'second')[1]
        headers = {**self.headers, 'Idempotency-Key': 'booking'}
        invalid = {'moves': [{'reference': a['reference'], 'table_id': 't2'}, {'reference': b['reference']}]}
        before = copy.deepcopy(self.api.state)
        self.error(self.api.request('POST', '/reservation-moves', headers, invalid), 409, 'table_unavailable')
        self.assertEqual(self.api.state, before)
        valid = {'moves': [{'reference': a['reference'], 'table_id': 't2'}, {'reference': b['reference'], 'table_id': 't1'}]}
        status, swapped = self.api.request('POST', '/reservation-moves', headers, valid)
        self.assertEqual(status, 201)
        self.assertEqual([r['table_id'] for r in swapped['reservations']], ['t2', 't1'])
        self.assertEqual(self.api.request('POST', '/reservation-moves', headers, valid), (200, swapped))
        self.assertEqual(self.book(), (200, a))
        noop = {'moves': [{'reference': a['reference']}, {'reference': b['reference']}]}
        self.assertEqual(self.api.request('POST', '/reservation-moves', {**self.headers, 'Idempotency-Key': 'noop'}, noop), (201, swapped))
        for moves in ([], [{}], [{'reference': a['reference']}] * 2, ['not an object'], 'not a list'):
            self.error(self.api.request('POST', '/reservation-moves', {**self.headers, 'Idempotency-Key': 'invalid'}, {'moves': moves}), 422, 'validation_failed')
        self.error(self.api.request('POST', '/reservation-moves', {**self.headers, 'Idempotency-Key': 'invalid'}, {'moves': [{'reference': a['reference'], 'table_id': 'missing'}, {'reference': 'ABSENT'}]}), 404, 'not_found')

    def test_cutoff_and_past_creation(self):
        day = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
        f = fixture('UTC', day)
        self.assertEqual(self.api.request('POST', '/_test/reset', body=f)[0], 204)
        _, login = self.api.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})
        self.headers = {'Authorization': 'Bearer ' + login['token']}
        status, res = self.book({**self.body, 'starts_at_local': day + 'T19:00'})
        self.assertEqual(status, 201)
        self.error(self.api.request('POST', '/reservations/' + res['reference'] + '/cancel', self.headers), 409, 'cutoff_passed')
        self.error(self.api.request('PATCH', '/reservations/' + res['reference'], self.headers, {'table_id': 3}), 409, 'cutoff_passed')
        self.error(self.api.request('POST', '/reservation-moves', {**self.headers, 'Idempotency-Key': 'past'}, {'moves': [{'reference': res['reference'], 'party_size': False}]}), 409, 'cutoff_passed')

    def test_dst_skipped_times_first_occurrence_and_absolute_duration(self):
        for zone, spring, autumn, repeated, expected_start, expected_end in (
            ('Europe/Berlin', '2026-03-29', '2026-10-25', '02:30', '+02:00', '03:00:00+01:00'),
            ('America/New_York', '2026-03-08', '2026-11-01', '01:30', '-04:00', '02:00:00-05:00'),
        ):
            self.assertEqual(self.api.request('POST', '/_test/reset', body=fixture(zone, spring, '00:00', '05:00'))[0], 204)
            _, login = self.api.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})
            self.headers = {'Authorization': 'Bearer ' + login['token']}
            self.error(self.book({**self.body, 'starts_at_local': spring + 'T02:30'}), 422, 'invalid_local_time')
            slots = self.api.request('GET', '/availability?restaurant_id=r1&date=' + spring + '&party_size=4')[1]['slots']
            self.assertFalse(any(s['starts_at_local'][11:13] == '02' for s in slots))
            self.assertEqual(self.api.request('POST', '/_test/reset', body=fixture(zone, autumn, '00:00', '05:00'))[0], 204)
            _, login = self.api.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})
            self.headers = {'Authorization': 'Bearer ' + login['token']}
            status, booking = self.book({**self.body, 'starts_at_local': autumn + 'T' + repeated})
            self.assertEqual(status, 201)
            self.assertTrue(booking['starts_at'].endswith(expected_start))
            self.assertTrue(booking['ends_at'].endswith(expected_end))
            self.assertEqual((datetime.fromisoformat(booking['ends_at']) - datetime.fromisoformat(booking['starts_at'])).total_seconds(), 5400)
            slots = self.api.request('GET', '/availability?restaurant_id=r1&date=' + autumn + '&party_size=4')[1]['slots']
            self.assertEqual(sum(s['starts_at_local'] == autumn + 'T' + repeated for s in slots), 1)

    def test_export_import_replacement_tokens_hashes_and_historical_receipts(self):
        a = self.book()[1]
        b = self.book({**self.body, 'table_id': 't2'}, 'second')[1]
        batch = {'moves': [{'reference': a['reference'], 'table_id': 't2'}, {'reference': b['reference'], 'table_id': 't1'}]}
        batch_headers = {**self.headers, 'Idempotency-Key': 'batch'}
        _, receipt = self.api.request('POST', '/reservation-moves', batch_headers, batch)
        self.api.request('POST', '/reservations/' + a['reference'] + '/cancel', self.headers)
        _, exported = self.api.request('GET', '/_test/export')
        destination = service.Service()
        destination.request('POST', '/_test/reset', body=fixture())
        _, previous_login = destination.request('POST', '/auth/login', body={'email': 'bob@example.com', 'password': 'correct horse'})
        self.assertEqual(destination.request('POST', '/_test/import', body=exported), (204, None))
        self.assertEqual(destination.state, exported['state'])
        self.error(destination.request('GET', '/reservations', {'Authorization': 'Bearer ' + previous_login['token']}), 401, 'unauthenticated')
        self.assertEqual(destination.request('POST', '/reservations', {**self.headers, 'Idempotency-Key': 'booking'}, self.body), (200, a))
        self.assertEqual(destination.request('POST', '/reservation-moves', batch_headers, batch), (200, receipt))
        self.assertEqual(destination.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})[0], 200)
        self.assertEqual(destination.request('POST', '/_test/import', body=exported), (204, None))
        self.assertEqual(destination.state, exported['state'])
        for changed in ({}, {**exported, 'track': 'other'}, {**exported, 'format_version': True}, {**exported, 'state': []}):
            before = copy.deepcopy(destination.state)
            self.error(destination.request('POST', '/_test/import', body=changed), 422, 'validation_failed')
            self.assertEqual(destination.state, before)
        broken = copy.deepcopy(exported)
        broken['state']['reservations'][a['reference']]['ends_at'] = 'invalid'
        before = copy.deepcopy(destination.state)
        self.error(destination.request('POST', '/_test/import', body=broken), 422, 'validation_failed')
        self.assertEqual(destination.state, before)
        self.api.request('POST', '/reservations/' + b['reference'] + '/cancel', self.headers)
        self.assertNotEqual(self.api.state, exported['state'])
        self.assertEqual(destination.request('POST', '/_test/reset', body=fixture()), (204, None))
        self.error(destination.request('GET', '/reservations', self.headers), 401, 'unauthenticated')

    def test_seed_reservations_and_id_length_reset_is_atomic(self):
        f = fixture()
        f['reservations'] = [{**self.body, 'id': 'seed1', 'reference': 'SEED01', 'user_id': 'ada'}]
        self.assertEqual(self.api.request('POST', '/_test/reset', body=f), (204, None))
        slots = self.api.request('GET', '/availability?restaurant_id=r1&date=2090-01-05&party_size=4')[1]['slots']
        occupied = next(s for s in slots if s['starts_at_local'].endswith('T19:00'))
        self.assertEqual(occupied['available_table_ids'], ['t2'])
        f['users'][0]['id'] = 'x' * 65
        before = copy.deepcopy(self.api.state)
        self.error(self.api.request('POST', '/_test/reset', body=f), 422, 'validation_failed')
        self.assertEqual(self.api.state, before)


class MemoryConnection:
    def __init__(self, raw):
        self.reader, self.writer = io.BytesIO(raw), io.BytesIO()

    def makefile(self, mode, buffering=None):
        return self.reader

    def sendall(self, data):
        self.writer.write(data)


class HTTPTests(unittest.TestCase):
    def call(self, raw):
        conn = MemoryConnection(raw)
        with patch.object(service, 'SERVICE', service.Service()):
            service.Handler(conn, ('127.0.0.1', 1234), None)
        head, body = conn.writer.getvalue().split(b'\r\n\r\n', 1)
        return head, json.loads(body) if body else None

    def test_health_http_content_type_and_json(self):
        head, body = self.call(b'GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        self.assertIn(b'200 OK', head)
        self.assertIn(b'Content-Type: application/json; charset=utf-8', head)
        self.assertEqual(body, {'status': 'ok'})

    def test_unparseable_json_and_wrong_object_type_rejected(self):
        for raw in (b'{', b'[]', b'{"email":null,"password":"long password"}'):
            request = b'POST /auth/login HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: ' + str(len(raw)).encode() + b'\r\n\r\n' + raw
            head, body = self.call(request)
            self.assertIn(b'400 Bad Request', head)
            self.assertEqual(body['error']['code'], 'malformed_request')

    def test_unsupported_http_method_returns_json_without_server_error(self):
        head, body = self.call(b'UNSUPPORTED /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        self.assertIn(b'404 Not Found', head)
        self.assertEqual(body['error']['code'], 'not_found')


if __name__ == '__main__':
    unittest.main()
