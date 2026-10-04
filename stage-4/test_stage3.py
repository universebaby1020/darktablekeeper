"""Policy decisions, immutable history, optimistic updates and recurring transactions."""
import copy
import importlib.util
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import service
import test_stage2
import test_service


def fixture():
    value = test_stage2.combination_fixture()
    restaurant = value['restaurants'][0]
    restaurant['manager_user_ids'] = ['ada']
    restaurant['opening_hours'] = [{'weekday': day, 'opens': '18:00', 'closes': '23:00'} for day in service.DAYS]
    return value


class Stage3Tests(unittest.TestCase):
    def setUp(self):
        self.api = service.Service()
        self.assertEqual(self.api.request('POST', '/_test/reset', body=fixture())[0], 204)
        self.headers = self.login('ada')
        self.bob = self.login('bob')
        self.body = {'restaurant_id': 'r1', 'table_id': 't1', 'starts_at_local': '2090-01-05T19:00', 'party_size': 2}

    def login(self, name):
        result = self.api.request('POST', '/auth/login', body={'email': name + '@example.com', 'password': 'correct horse'})
        self.assertEqual(result[0], 200)
        return {'Authorization': 'Bearer ' + result[1]['token']}

    def error(self, result, status, code):
        self.assertEqual(result[0], status)
        self.assertEqual(result[1]['error']['code'], code)

    def book(self, fields=None, key='booking'):
        return self.api.request('POST', '/reservations', {**self.headers, 'Idempotency-Key': key}, fields or self.body)

    def policy(self, effective='2090-01-01', **changes):
        return {'effective_from': effective, 'slot_minutes': 30, 'reservation_duration_minutes': 120,
                'cancellation_cutoff_minutes': 60,
                'opening_hours': [{'weekday': day, 'opens': '18:00', 'closes': '23:00'} for day in service.DAYS],
                'capacities': {'t1': 4, 't2': 4, 't3': 2}, **changes}

    def publish(self, policy=None, key='policy', headers=None):
        return self.api.request('POST', '/restaurants/r1/policies', {**(headers or self.headers), 'Idempotency-Key': key}, policy or self.policy())

    def history(self, reference):
        result = self.api.request('GET', '/reservations/' + reference + '/history', self.headers)
        self.assertEqual(result[0], 200)
        return result[1]['entries']

    def adopt(self, reference, count=3, interval=1, key='series'):
        return self.api.request('POST', '/series', {**self.headers, 'Idempotency-Key': key},
                                {'anchor_reference': reference, 'count': count, 'interval_weeks': interval})

    def test_explanations_report_both_independent_rules_in_fixture_order(self):
        self.book()
        target = '/availability?restaurant_id=r1&date=2090-01-05&party_size=5'
        ordinary = self.api.request('GET', target)[1]['slots']
        self.assertTrue(all('explain' not in slot for slot in ordinary))
        slots = self.api.request('GET', target + '&explain=true')[1]['slots']
        at19 = next(slot for slot in slots if slot['starts_at_local'].endswith('19:00'))
        self.assertEqual([item['table_id'] for item in at19['explain']], ['t1', 't2', 't3'])
        self.assertEqual(at19['explain'][0]['rules'], [{'rule': 'capacity', 'holds': False}, {'rule': 'no_overlap', 'holds': False}])
        self.assertEqual(at19['explain'][1]['rules'], [{'rule': 'capacity', 'holds': False}, {'rule': 'no_overlap', 'holds': True}])
        for slot in slots:
            self.assertEqual(slot['available_table_ids'], [item['table_id'] for item in slot['explain'] if item['available']])
            self.assertTrue(all(item['available'] == all(rule['holds'] for rule in item['rules']) for item in slot['explain']))
        for value in ('false', '1', '', 'True'):
            self.error(self.api.request('GET', target + '&explain=' + value), 422, 'validation_failed')

    def test_history_order_actual_changes_noops_and_private_read_exceptions(self):
        original = self.book()[1]
        reference = original['reference']
        entries = self.history(reference)
        self.assertEqual([change['field'] for change in entries[0]['changes']], ['table_id', 'starts_at_local', 'party_size'])
        self.assertTrue(all(change['from'] is None for change in entries[0]['changes']))
        self.assertEqual(entries[0]['revision'], 1)
        self.assertEqual(entries[0]['accepted_terms'], original['accepted_terms'])
        path = '/reservations/' + reference
        self.assertEqual(self.api.request('PATCH', path, self.headers, {'party_size': 2})[1], original)
        self.assertEqual(len(self.history(reference)), 1)
        changed = self.api.request('PATCH', path, self.headers, {'table_id': 't2', 'starts_at_local': '2090-01-05T20:00', 'party_size': 3})[1]
        self.assertEqual(changed['revision'], 2)
        self.assertEqual([c['field'] for c in self.history(reference)[1]['changes']], ['table_id', 'starts_at_local', 'party_size'])
        self.assertEqual(self.book(), (200, original))
        self.api.request('POST', path + '/cancel', self.headers)
        self.api.request('POST', path + '/cancel', self.headers)
        entries = self.history(reference)
        self.assertEqual([e['seq'] for e in entries], [1, 2, 3])
        self.assertEqual([e['revision'] for e in entries], [1, 2, 3])
        self.assertEqual(entries[-1]['changes'], [])
        self.assertEqual(entries[-1]['event'], 'cancelled')
        self.assertEqual(self.api.request('GET', path + '/decision', self.headers)[1]['revision'], 3)
        for suffix in ('/history', '/decision'):
            self.error(self.api.request('GET', path + suffix), 404, 'not_found')
            self.error(self.api.request('GET', path + suffix, self.bob), 404, 'not_found')

    def test_policy_manager_permissions_complete_validation_and_reusable_failed_keys(self):
        self.error(self.api.request('POST', '/restaurants/r1/policies', body=self.policy()), 401, 'unauthenticated')
        self.error(self.publish(headers=self.bob), 403, 'forbidden')
        self.error(self.api.request('POST', '/restaurants/missing/policies', {**self.headers, 'Idempotency-Key': 'missing'}, self.policy()), 404, 'not_found')
        invalids = [{}, self.policy(slot_minutes=True), self.policy(slot_minutes=0), self.policy(reservation_duration_minutes=1441),
                    self.policy(cancellation_cutoff_minutes=10081), self.policy(capacities={'t1': 4}),
                    self.policy(capacities={'t1': 4, 't2': 4, 't3': False}), self.policy(capacities={'t1': 4, 't2': 4, 't3': 101}),
                    self.policy(effective='2090-02-30'), self.policy(opening_hours=[{'weekday': 'mon', 'opens': '18:00', 'closes': '23:00'}] * 2),
                    self.policy(opening_hours=[{'weekday': 'mon', 'opens': '25:00', 'closes': '23:00'}])]
        for invalid in invalids:
            before = copy.deepcopy(self.api.state)
            result = self.api.request('POST', '/restaurants/r1/policies', {**self.headers, 'Idempotency-Key': 'repairable'}, invalid)
            self.error(result, 422, 'validation_failed')
            self.assertTrue(self.api.state == before)
        status, policy = self.publish(key='repairable')
        self.assertEqual(status, 201)
        self.assertEqual(policy['policy_version'], 1)
        self.assertEqual(self.publish(key='repairable'), (200, policy))
        self.error(self.publish(self.policy(slot_minutes=False), key='repairable'), 409, 'idempotency_key_reuse')
        self.assertEqual(self.api.request('GET', '/restaurants/r1/policies'), (200, {'policies': [policy]}))

    def test_dated_policy_selection_ties_and_immutable_accepted_decisions(self):
        old = self.book()[1]
        self.publish(self.policy('2090-02-01', reservation_duration_minutes=60), 'late-date')
        self.publish(self.policy('2090-01-01', reservation_duration_minutes=120), 'early-date')
        latest = self.publish(self.policy('2090-01-01', reservation_duration_minutes=30, capacities={'t1': 1, 't2': 8, 't3': 2}), 'same-date')[1]
        slots = self.api.request('GET', '/availability?restaurant_id=r1&date=2090-01-05&party_size=4&explain=true')[1]['slots']
        self.assertEqual(slots[0]['explain'][0]['policy_version'], latest['policy_version'])
        self.assertFalse(slots[0]['explain'][0]['rules'][0]['holds'])
        self.assertEqual(slots[0]['available_options'][0], {'table_ids': ['t2'], 'capacity': 8})
        current = self.api.request('GET', '/reservations/' + old['reference'], self.headers)[1]
        self.assertEqual(current, old)
        self.assertEqual(self.history(old['reference'])[0]['accepted_terms']['policy_version'], 0)
        self.assertEqual(self.api.request('GET', '/restaurants/r1')[1]['reservation_duration_minutes'], 90)
        future = self.book({**self.body, 'starts_at_local': '2090-02-02T19:00'}, 'future')[1]
        self.assertEqual(future['accepted_terms']['policy_version'], 1)

    def test_noop_keeps_old_terms_real_change_adopts_policy_and_failed_change_rolls_back(self):
        original = self.book()[1]
        path = '/reservations/' + original['reference']
        self.publish(self.policy(capacities={'t1': 1, 't2': 8, 't3': 2}))
        self.assertEqual(self.api.request('PATCH', path, self.headers, {'party_size': 2}), (200, original))
        before = copy.deepcopy(self.api.state)
        self.error(self.api.request('PATCH', path, self.headers, {'starts_at_local': '2090-01-05T20:00'}), 422, 'party_exceeds_capacity')
        self.assertTrue(self.api.state == before)
        changed = self.api.request('PATCH', path, self.headers, {'table_id': 't2'})[1]
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(changed['accepted_terms']['policy_version'], 1)
        self.assertTrue(changed['ends_at'].endswith('21:00:00+01:00'))
        self.assertEqual(self.history(original['reference'])[0]['accepted_terms'], original['accepted_terms'])

    def test_accepted_cutoff_and_stale_revision_precede_changed_field_validation(self):
        original = self.book()[1]
        path = '/reservations/' + original['reference']
        for expected in (True, 0, -1, '1', 1.5):
            self.error(self.api.request('PATCH', path, self.headers, {'expected_revision': expected}), 422, 'validation_failed')
        self.error(self.api.request('PATCH', path, self.headers, {'expected_revision': 2, 'party_size': False}), 409, 'stale_revision')
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat('2090-01-05T16:30:00+00:00').astimezone(tz)
        self.publish(self.policy(cancellation_cutoff_minutes=0))
        with patch.object(service, 'datetime', Clock):
            self.error(self.api.request('PATCH', path, self.headers, {'party_size': False}), 409, 'cutoff_passed')
            self.error(self.api.request('POST', path + '/cancel', self.headers), 409, 'cutoff_passed')

    def test_two_expected_revision_amendments_only_one_real_change_succeeds(self):
        original = self.book()[1]
        path = '/reservations/' + original['reference']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda party: self.api.request('PATCH', path, self.headers, {'party_size': party, 'expected_revision': 1}), (3, 4)))
        self.assertEqual(sorted(r[0] for r in results), [200, 409])
        self.assertEqual(next(r[1]['error']['code'] for r in results if r[0] == 409), 'stale_revision')
        self.assertEqual(len(self.history(original['reference'])), 2)

    def test_combined_history_canonical_sets_and_noop_reverse_pair(self):
        body = {k: v for k, v in self.body.items() if k != 'table_id'}
        pair = self.book({**body, 'table_ids': ['t1', 't2'], 'party_size': 6})[1]
        path = '/reservations/' + pair['reference']
        self.assertEqual(self.history(pair['reference'])[0]['changes'][0], {'field': 'table_ids', 'from': None, 'to': ['t2', 't1']})
        before = copy.deepcopy(self.api.state)
        self.assertEqual(self.api.request('PATCH', path, self.headers, {'table_ids': ['t1', 't2']}), (200, pair))
        self.assertTrue(self.api.state == before)
        self.api.request('PATCH', path, self.headers, {'table_id': 't3', 'party_size': 2})
        self.assertEqual(self.history(pair['reference'])[1]['changes'][0], {'field': 'table_ids', 'from': ['t2', 't1'], 'to': ['t3']})

    def test_series_anchor_unchanged_generated_policies_and_original_replay(self):
        anchor = self.book()[1]
        anchor_history = self.history(anchor['reference'])
        self.publish(self.policy('2090-01-12', reservation_duration_minutes=60))
        before_counter = self.api.state['restaurant_revisions']['r1']
        status, series = self.adopt(anchor['reference'])
        self.assertEqual(status, 201)
        self.assertEqual(series['occurrences'][0]['reservation'], anchor)
        self.assertEqual(self.history(anchor['reference']), anchor_history)
        self.assertEqual(self.api.state['restaurant_revisions']['r1'], before_counter + 1)
        self.assertEqual([item['index'] for item in series['occurrences']], [0, 1, 2])
        self.assertEqual(len({item['reference'] for item in series['occurrences']}), 3)
        self.assertEqual([item['reservation']['accepted_terms']['policy_version'] for item in series['occurrences']], [0, 1, 1])
        self.assertEqual(self.book(), (200, anchor))
        self.error(self.adopt(anchor['reference'], key='other-adoption'), 409, 'already_in_series')
        for headers in ({}, self.bob):
            self.error(self.api.request('GET', '/series/' + series['series_id'], headers), 404, 'not_found')
        second = series['occurrences'][1]['reference']
        self.api.request('PATCH', '/reservations/' + second, self.headers, {'party_size': 3})
        changed = self.api.request('GET', '/series/' + series['series_id'], self.headers)[1]
        self.assertEqual(changed['revision'], 2)
        self.assertTrue(changed['occurrences'][1]['exception'])
        self.assertEqual(self.adopt(anchor['reference']), (200, series))

    def test_series_failure_rolls_back_later_occurrences_history_counters_and_key(self):
        anchor = self.book()[1]
        self.book({**self.body, 'starts_at_local': '2090-01-19T19:00'}, 'blocking')
        before = copy.deepcopy(self.api.state)
        self.error(self.adopt(anchor['reference']), 409, 'table_unavailable')
        self.assertTrue(self.api.state == before)
        self.assertEqual(self.adopt(anchor['reference'], count=2)[0], 201)

    def test_series_policy_failure_and_count_interval_limits_leave_state_unchanged(self):
        anchor = self.book()[1]
        for count, interval in ((True, 1), (1, 1), (13, 1), (2, True), (2, 0), (2, 5)):
            before = copy.deepcopy(self.api.state)
            self.error(self.adopt(anchor['reference'], count, interval), 422, 'validation_failed')
            self.assertTrue(self.api.state == before)
        self.publish(self.policy('2090-01-19', capacities={'t1': 1, 't2': 4, 't3': 2}))
        before = copy.deepcopy(self.api.state)
        self.error(self.adopt(anchor['reference']), 422, 'party_exceeds_capacity')
        self.assertTrue(self.api.state == before)

    def test_series_cancellation_noops_and_collective_exceptions_revision_once(self):
        anchor = self.book()[1]
        series = self.adopt(anchor['reference'])[1]
        sid = series['series_id']
        references = [item['reference'] for item in series['occurrences']]
        self.api.request('PATCH', '/reservations/' + references[1], self.headers, {'party_size': 2})
        self.assertEqual(self.api.request('GET', '/series/' + sid, self.headers)[1], series)
        moves = {'moves': [{'reference': reference, 'party_size': 3, 'expected_revision': 1} for reference in references[:2]]}
        headers = {**self.headers, 'Idempotency-Key': 'collective'}
        before_counter = self.api.state['restaurant_revisions']['r1']
        status, moved = self.api.request('POST', '/reservation-moves', headers, moves)
        self.assertEqual(status, 201)
        self.assertEqual(self.api.state['restaurant_revisions']['r1'], before_counter + 1)
        current = self.api.request('GET', '/series/' + sid, self.headers)[1]
        self.assertEqual(current['revision'], 2)
        self.assertEqual([item['exception'] for item in current['occurrences']], [True, True, False])
        self.assertEqual(self.api.request('POST', '/reservation-moves', headers, moves), (200, moved))
        self.api.request('POST', '/reservations/' + references[0] + '/cancel', self.headers)
        self.api.request('POST', '/reservations/' + references[0] + '/cancel', self.headers)
        current = self.api.request('GET', '/series/' + sid, self.headers)[1]
        self.assertEqual(current['revision'], 3)
        self.assertEqual([item['reservation']['status'] for item in current['occurrences']], ['cancelled', 'confirmed', 'confirmed'])
        self.assertTrue(current['occurrences'][0]['exception'])

    def test_failed_collective_move_does_not_change_history_or_series_flags(self):
        anchor = self.book()[1]
        series = self.adopt(anchor['reference'])[1]
        first, second = [item['reference'] for item in series['occurrences'][:2]]
        moves = {'moves': [{'reference': first, 'party_size': 3}, {'reference': second, 'party_size': 99}]}
        before = copy.deepcopy(self.api.state)
        self.error(self.api.request('POST', '/reservation-moves', {**self.headers, 'Idempotency-Key': 'failure'}, moves), 422, 'party_exceeds_capacity')
        self.assertTrue(self.api.state == before)

    def test_export_import_preserves_policy_history_series_and_all_receipts(self):
        self.publish()
        anchor = self.book()[1]
        series = self.adopt(anchor['reference'])[1]
        second = series['occurrences'][1]['reference']
        self.api.request('PATCH', '/reservations/' + second, self.headers, {'table_ids': ['t2', 't1'], 'party_size': 6})
        self.api.request('POST', '/reservations/' + anchor['reference'] + '/cancel', self.headers)
        exported = self.api.request('GET', '/_test/export')[1]
        destination = service.Service()
        self.assertEqual(destination.request('POST', '/_test/import', body=exported), (204, None))
        self.assertTrue(destination.state == exported['state'])
        body = {'anchor_reference': anchor['reference'], 'count': 3, 'interval_weeks': 1}
        self.assertEqual(destination.request('POST', '/series', {**self.headers, 'Idempotency-Key': 'series'}, body), (200, series))
        self.assertEqual(destination.request('POST', '/reservations', {**self.headers, 'Idempotency-Key': 'booking'}, self.body), (200, anchor))
        self.assertEqual(destination.request('POST', '/restaurants/r1/policies', {**self.headers, 'Idempotency-Key': 'policy'}, self.policy())[0], 200)
        for changed in ('history', 'policy', 'series', 'terms'):
            invalid = copy.deepcopy(exported)
            if changed == 'history':
                invalid['state']['histories'][second][1]['revision'] = 0
            elif changed == 'policy':
                invalid['state']['policies']['r1'][0]['capacities']['t1'] = 0
            elif changed == 'series':
                invalid['state']['series'][series['series_id']]['occurrences'][1]['reference'] = anchor['reference']
            else:
                invalid['state']['reservations'][second]['accepted_terms']['slot_minutes'] = True
            before = copy.deepcopy(destination.state)
            self.error(destination.request('POST', '/_test/import', body=invalid), 422, 'validation_failed')
            self.assertTrue(destination.state == before)

    def test_actual_earlier_stage_exports_support_adoption_and_original_retry(self):
        for stage in (1, 2):
            spec = importlib.util.spec_from_file_location('earlier_stage_' + str(stage), Path(__file__).parent.parent / ('stage-' + str(stage)) / 'service.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            old = module.Service()
            old.request('POST', '/_test/reset', body=fixture())
            account = old.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})[1]
            headers = {'Authorization': 'Bearer ' + account['token'], 'Idempotency-Key': 'legacy'}
            original = old.request('POST', '/reservations', headers, self.body)[1]
            exported = old.request('GET', '/_test/export')[1]
            self.assertEqual(self.api.request('POST', '/_test/import', body=exported), (204, None))
            self.assertEqual(self.api.request('POST', '/reservations', headers, self.body), (200, original))
            self.headers = headers
            adopted = self.adopt(original['reference'], key='upgraded-series')
            self.assertEqual(adopted[0], 201)
            self.assertEqual(adopted[1]['occurrences'][0]['reservation']['reference'], original['reference'])

    def test_dst_series_skipped_time_rollback_and_first_repeated_occurrence(self):
        class EarlyClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 2, 1, tzinfo=timezone.utc).astimezone(tz)
        for zone, spring_anchor, repeated_anchor, expected_offset in (
            ('Europe/Berlin', '2026-03-22T02:30', '2026-10-18T02:30', '+02:00'),
            ('America/New_York', '2026-03-01T02:30', '2026-10-25T01:30', '-04:00')):
            with patch.object(service, 'datetime', EarlyClock):
                f = test_service.fixture(zone, spring_anchor[:10], '00:00', '05:00')
                self.assertEqual(self.api.request('POST', '/_test/reset', body=f)[0], 204)
                self.headers = self.login('ada')
                anchor = self.book({**self.body, 'starts_at_local': spring_anchor})[1]
                before = copy.deepcopy(self.api.state)
                self.error(self.adopt(anchor['reference'], count=2), 422, 'invalid_local_time')
                self.assertTrue(self.api.state == before)
                self.assertEqual(self.api.request('POST', '/_test/reset', body=test_service.fixture(zone, repeated_anchor[:10], '00:00', '05:00'))[0], 204)
                self.headers = self.login('ada')
                anchor = self.book({**self.body, 'starts_at_local': repeated_anchor})[1]
                status, recurring = self.adopt(anchor['reference'], count=2)
                self.assertEqual(status, 201)
                occurrence = recurring['occurrences'][1]['reservation']
                self.assertTrue(occurrence['starts_at'].endswith(expected_offset))
                self.assertEqual((datetime.fromisoformat(occurrence['ends_at']) - datetime.fromisoformat(occurrence['starts_at'])).total_seconds(), 5400)

    def test_concurrent_policy_and_series_replays_allocate_once(self):
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda _: self.publish(), range(50)))
        self.assertEqual(sum(result[0] == 201 for result in results), 1)
        self.assertEqual(sum(result[0] == 200 for result in results), 49)
        self.assertTrue(all(result[1] == results[0][1] for result in results))
        self.assertEqual(len(self.api.state['policies']['r1']), 1)
        anchor = self.book()[1]
        before_counter = self.api.state['restaurant_revisions']['r1']
        with ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda _: self.adopt(anchor['reference'], count=12, interval=4), range(50)))
        self.assertEqual(sum(result[0] == 201 for result in results), 1)
        self.assertEqual(sum(result[0] == 200 for result in results), 49)
        self.assertTrue(all(result[1] == results[0][1] for result in results))
        self.assertEqual(len(self.api.state['reservations']), 12)
        self.assertEqual(len(self.api.state['series']), 1)
        self.assertEqual(self.api.state['restaurant_revisions']['r1'], before_counter + 1)

    def test_cancelled_seed_history_and_revision_survive_export_import(self):
        seeded = fixture()
        seeded['reservations'] = [{**self.body, 'id': 'seed-cancelled', 'reference': 'SEED001', 'user_id': 'ada', 'status': 'cancelled'}]
        self.assertEqual(self.api.request('POST', '/_test/reset', body=seeded), (204, None))
        self.headers = self.login('ada')
        entries = self.history('SEED001')
        self.assertEqual([entry['event'] for entry in entries], ['created', 'cancelled'])
        self.assertEqual(self.api.request('GET', '/reservations/SEED001', self.headers)[1]['revision'], 1)
        exported = self.api.request('GET', '/_test/export')[1]
        destination = service.Service()
        self.assertEqual(destination.request('POST', '/_test/import', body=exported), (204, None))
        self.assertTrue(destination.state == exported['state'])


if __name__ == '__main__':
    unittest.main()
