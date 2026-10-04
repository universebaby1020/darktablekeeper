"""Combined occupancy, migration and local-asset HTTP requirements."""
import copy
import importlib.util
import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import service
import test_service as base


def combination_fixture():
    result = base.fixture()
    restaurant = result['restaurants'][0]
    restaurant['tables'].append({'id': 't3', 'label': 'Window', 'capacity': 2})
    restaurant['combinable'] = [['t2', 't1'], ['t1', 't3']]
    return result


class CombinedTests(unittest.TestCase):
    def setUp(self):
        self.api = service.Service()
        self.assertEqual(self.api.request('POST', '/_test/reset', body=combination_fixture())[0], 204)
        _, login = self.api.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})
        self.headers = {'Authorization': 'Bearer ' + login['token']}
        self.body = {'restaurant_id': 'r1', 'table_ids': ['t1', 't2'], 'starts_at_local': '2090-01-05T19:00', 'party_size': 7}

    def book(self, body=None, key='combo'):
        return self.api.request('POST', '/reservations', {**self.headers, 'Idempotency-Key': key}, body or self.body)

    def error(self, result, status, code):
        self.assertEqual(result[0], status)
        self.assertEqual(result[1]['error']['code'], code)

    def test_combination_occupies_both_members_and_cancel_frees_both(self):
        status, booking = self.book()
        self.assertEqual(status, 201)
        self.assertEqual(booking['table_ids'], ['t2', 't1'])
        self.assertNotIn('table_id', booking)
        for tid in ('t1', 't2'):
            self.error(self.book({**self.body, 'table_ids': [tid], 'party_size': 2}, tid), 409, 'table_unavailable')
        self.assertEqual(self.book({**self.body, 'starts_at_local': '2090-01-05T20:30'}, 'adjacent')[0], 201)
        self.assertEqual(self.api.request('POST', '/reservations/' + booking['reference'] + '/cancel', self.headers)[0], 200)
        for tid in ('t1', 't2'):
            result = self.book({**self.body, 'table_ids': [tid], 'party_size': 2}, tid)
            self.assertEqual(result[0], 201)
            self.assertEqual(result[1]['table_id'], tid)
            self.assertEqual(result[1]['table_ids'], [tid])

    def test_options_order_capacity_and_pair_nontransitivity(self):
        slots = self.api.request('GET', '/availability?restaurant_id=r1&date=2090-01-05&party_size=2')[1]['slots']
        self.assertEqual(slots[0]['available_options'], [
            {'table_ids': ['t1'], 'capacity': 4}, {'table_ids': ['t2'], 'capacity': 4}, {'table_ids': ['t3'], 'capacity': 2},
            {'table_ids': ['t2', 't1'], 'capacity': 8}, {'table_ids': ['t1', 't3'], 'capacity': 6}])
        large = self.api.request('GET', '/availability?restaurant_id=r1&date=2090-01-05&party_size=6')[1]['slots'][0]
        self.assertEqual(large['available_table_ids'], [])
        self.assertEqual([o['table_ids'] for o in large['available_options']], [['t2', 't1'], ['t1', 't3']])
        self.error(self.book({**self.body, 'table_ids': ['t2', 't3'], 'party_size': 2}), 422, 'combination_not_allowed')
        self.error(self.book({**self.body, 'table_ids': ['t1', 't2', 't3']}), 422, 'combination_not_allowed')
        self.error(self.book({**self.body, 'party_size': 9}), 422, 'party_exceeds_capacity')

    def test_invalid_selection_shape_and_id_limits_leave_state_unchanged(self):
        before = copy.deepcopy(self.api.state)
        for value, status, code in (([], 422, 'validation_failed'), (['t1', 't1'], 422, 'validation_failed'),
                                     ('t1', 400, 'malformed_request'), ([True], 400, 'malformed_request'),
                                     (['x' * 65], 422, 'validation_failed'), (['absent'], 404, 'not_found')):
            self.error(self.book({**self.body, 'table_ids': value}), status, code)
            self.assertTrue(self.api.state == before)
        self.error(self.book({**self.body, 'table_id': 't1'}), 422, 'validation_failed')
        self.assertTrue(self.api.state == before)
        self.assertEqual(self.book()[0], 201)

    def test_amendment_switches_sets_and_failure_preserves_occupancy(self):
        booking = self.book()[1]
        path = '/reservations/' + booking['reference']
        before = copy.deepcopy(self.api.state)
        self.error(self.api.request('PATCH', path, self.headers, {'table_ids': ['t2', 't3']}), 422, 'combination_not_allowed')
        self.assertTrue(self.api.state == before)
        status, single = self.api.request('PATCH', path, self.headers, {'table_id': 't3', 'party_size': 2})
        self.assertEqual(status, 200)
        self.assertEqual(single['table_ids'], ['t3'])
        self.assertEqual(single['table_id'], 't3')
        status, pair = self.api.request('PATCH', path, self.headers, {'table_ids': ['t3', 't1'], 'party_size': 6})
        self.assertEqual(status, 200)
        self.assertEqual(pair['table_ids'], ['t1', 't3'])
        self.assertNotIn('table_id', pair)
        for key in ('reference', 'reservation_id', 'created_at'):
            self.assertEqual(pair[key], booking[key])

    def test_simultaneous_pair_and_single_requests_serialize_on_shared_member(self):
        def attempt(index):
            return self.book(self.body if index % 2 else {**self.body, 'table_ids': ['t1'], 'party_size': 2}, str(index))
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(attempt, range(50)))
        self.assertEqual(sum(r[0] == 201 for r in results), 1)
        self.assertEqual(sum(r[0] == 409 and r[1]['error']['code'] == 'table_unavailable' for r in results), 49)

    def test_atomic_moves_between_single_and_pair_sets(self):
        first = self.book({**self.body, 'table_ids': ['t1'], 'party_size': 2}, 'first')[1]
        second = self.book({**self.body, 'table_ids': ['t3'], 'party_size': 2}, 'second')[1]
        headers = {**self.headers, 'Idempotency-Key': 'move'}
        valid = {'moves': [{'reference': first['reference'], 'table_ids': ['t1', 't3']}, {'reference': second['reference'], 'table_id': 't2'}]}
        status, response = self.api.request('POST', '/reservation-moves', headers, valid)
        self.assertEqual(status, 201)
        self.assertEqual(response['reservations'][0]['table_ids'], ['t1', 't3'])
        self.assertEqual(response['reservations'][1]['table_id'], 't2')
        invalid = {'moves': [{'reference': first['reference'], 'table_ids': ['t1', 't2']}, {'reference': second['reference'], 'table_id': 't2'}]}
        before = copy.deepcopy(self.api.state)
        self.error(self.api.request('POST', '/reservation-moves', {**headers, 'Idempotency-Key': 'invalid'}, invalid), 409, 'table_unavailable')
        self.assertTrue(self.api.state == before)
        exported = self.api.request('GET', '/_test/export')[1]
        self.assertEqual(self.api.request('POST', '/_test/import', body=exported), (204, None))
        self.assertEqual(self.api.request('POST', '/reservation-moves', headers, valid), (200, response))

    def test_cancelled_combination_seeds_do_not_occupy_members(self):
        f = combination_fixture()
        f['reservations'] = [{**self.body, 'id': 'seed1', 'reference': 'SEED01', 'user_id': 'ada', 'status': 'cancelled'},
                             {**self.body, 'id': 'seed2', 'reference': 'SEED02', 'user_id': 'bob'}]
        self.assertEqual(self.api.request('POST', '/_test/reset', body=f), (204, None))
        self.assertEqual(self.api.state['reservations']['SEED01']['status'], 'cancelled')
        self.assertEqual(self.api.state['reservations']['SEED02']['status'], 'confirmed')

    def test_stage1_export_upgrade_preserves_session_records_and_original_receipts(self):
        spec = importlib.util.spec_from_file_location('accepted_stage1', Path(__file__).parent.parent / 'stage-1' / 'service.py')
        old_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old_module)
        old = old_module.Service()
        old.request('POST', '/_test/reset', body=base.fixture())
        _, account = old.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})
        headers = {'Authorization': 'Bearer ' + account['token'], 'Idempotency-Key': 'before-upgrade'}
        body = {'restaurant_id': 'r1', 'table_id': 't1', 'starts_at_local': '2090-01-05T19:00', 'party_size': 2}
        status, original = old.request('POST', '/reservations', headers, body)
        self.assertEqual(status, 201)
        moves = {'moves': [{'reference': original['reference'], 'party_size': 3}]}
        move_headers = {**headers, 'Idempotency-Key': 'old-move'}
        original_move = old.request('POST', '/reservation-moves', move_headers, moves)[1]
        exported = old.request('GET', '/_test/export')[1]
        self.assertEqual(self.api.request('POST', '/_test/import', body=exported), (204, None))
        self.assertEqual(self.api.request('POST', '/reservations', headers, body), (200, original))
        self.assertEqual(self.api.request('POST', '/reservation-moves', move_headers, moves), (200, original_move))
        detail = self.api.request('GET', '/reservations/' + original['reference'], headers)[1]
        self.assertEqual(detail['table_ids'], ['t1'])
        self.assertEqual(detail['party_size'], 3)
        self.assertEqual(detail['created_at'], original['created_at'])
        self.assertEqual(self.api.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})[0], 200)
        upgraded = self.api.request('GET', '/_test/export')[1]
        self.assertEqual(self.api.request('POST', '/_test/import', body=upgraded), (204, None))
        self.assertEqual(self.api.request('POST', '/reservations', headers, body), (200, original))


class StaticHTTPTests(unittest.TestCase):
    def test_required_screen_and_local_asset_routes(self):
        for path, content_type in (('/', 'text/html'), ('/signup', 'text/html'), ('/login', 'text/html'), ('/lookup?reference=EXAMPLE', 'text/html'),
                                   ('/assets/app.js', 'text/javascript'), ('/assets/app.css', 'text/css')):
            request = ('GET ' + path + ' HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n').encode()
            connection = base.MemoryConnection(request)
            service.Handler(connection, ('127.0.0.1', 1234), None)
            head, body = connection.writer.getvalue().split(b'\r\n\r\n', 1)
            self.assertIn(b'200 OK', head)
            self.assertIn(content_type.encode(), head)
            self.assertTrue(body)
            if content_type == 'text/html':
                self.assertIn(b'name="viewport"', body)
                self.assertNotIn(b'https://', body)


if __name__ == '__main__':
    unittest.main()
