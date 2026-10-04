"""Closures, optimal assignments and collective recurring transactions."""
import copy
import importlib.util
import itertools
import random
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import service
import test_stage3


class Stage4Tests(unittest.TestCase):
    setUp = test_stage3.Stage3Tests.setUp
    login = test_stage3.Stage3Tests.login
    error = test_stage3.Stage3Tests.error
    book = test_stage3.Stage3Tests.book
    history = test_stage3.Stage3Tests.history
    adopt = test_stage3.Stage3Tests.adopt
    policy = test_stage3.Stage3Tests.policy
    publish = test_stage3.Stage3Tests.publish

    def preview(self, tid='t1', start='2090-01-05T18:00:00+00:00', end='2090-01-05T23:00:00+00:00', key='preview', headers=None, rid='r1'):
        return self.api.request('POST', '/restaurants/' + rid + '/replans',
                                {**(headers or self.headers), 'Idempotency-Key': key},
                                {'table_id': tid, 'from': start, 'to': end})

    def apply(self, plan, key='apply', rid='r1', headers=None):
        return self.api.request('POST', '/restaurants/' + rid + '/replans/' + plan['plan_id'] + '/apply',
                                {**(headers or self.headers), 'Idempotency-Key': key}, {})

    def amend(self, sid, revision=1, index=0, clock='20:00', key='amend', headers=None):
        return self.api.request('POST', '/series/' + sid + '/amend',
                                {**(headers or self.headers), 'Idempotency-Key': key},
                                {'expected_revision': revision, 'from_index': index, 'local_time': clock})

    def test_preview_is_read_only_apply_preserves_terms_times_and_replay(self):
        self.assertEqual(self.api.state['restaurant_revisions']['r1'], 0)
        old = self.book()[1]
        self.publish(self.policy(capacities={'t1': 1, 't2': 1, 't3': 1}))
        before = copy.deepcopy(self.api.state)
        status, plan = self.preview()
        self.assertEqual(status, 201)
        self.assertEqual(plan['moved_count'], 1)
        self.assertEqual(plan['assignments'][0]['table_ids'], ['t3'])
        for field in ('reservations', 'histories', 'series', 'restaurant_revisions', 'closures'):
            self.assertEqual(self.api.state[field], before[field])
        self.assertEqual(self.preview(), (200, plan))
        status, result = self.apply(plan)
        self.assertEqual(status, 201)
        new = result['reservations'][0]
        for field in ('reference', 'reservation_id', 'party_size', 'starts_at_local', 'starts_at', 'ends_at', 'accepted_terms', 'created_at'):
            self.assertEqual(new[field], old[field])
        self.assertEqual(new['revision'], 2)
        history = self.history(old['reference'])[-1]
        self.assertEqual(history['event'], 'reassigned')
        self.assertEqual(history['plan_id'], plan['plan_id'])
        self.assertEqual(history['changes'], [{'field': 'table_ids', 'from': ['t1'], 'to': ['t3']}])
        self.assertEqual(result['restaurant_revision'], plan['restaurant_revision'] + 1)
        self.assertEqual(self.apply(plan), (200, result))
        self.error(self.apply(plan, key='different'), 409, 'plan_already_applied')
        destination = service.Service()
        self.assertEqual(destination.request('POST', '/_test/import', body=self.api.request('GET', '/_test/export')[1])[0], 204)
        self.assertEqual(destination.state, self.api.state)
        self.assertEqual(destination.request('POST', '/restaurants/r1/replans/' + plan['plan_id'] + '/apply',
                                            {**self.headers, 'Idempotency-Key': 'apply'}, {}), (200, result))

    def test_closure_half_open_boundaries_options_explanations_and_amendment(self):
        plan = self.preview(start='2090-01-05T19:30:00+01:00', end='2090-01-05T20:30:00+01:00')[1]
        self.assertEqual(plan['assignments'], [])
        self.assertEqual(self.apply(plan)[0], 201)
        def at(local):
            return self.book({**self.body, 'starts_at_local': '2090-01-05T' + local}, key=local)
        self.assertEqual(at('18:00')[0], 201)  # Ends exactly at the closure start.
        self.assertEqual(at('20:30')[0], 201)  # Starts exactly at its end.
        before = copy.deepcopy(self.api.state)
        self.error(at('19:00'), 409, 'table_unavailable')
        self.assertEqual(self.api.state, before)
        self.error(self.book({**self.body, 'table_ids': ['t2', 't1'], 'table_id': 't1'}, 'both'), 422, 'validation_failed')
        combined = {k: v for k, v in self.body.items() if k != 'table_id'}
        self.error(self.book({**combined, 'table_ids': ['t2', 't1'], 'party_size': 6}, 'pair'), 409, 'table_unavailable')
        slots = self.api.request('GET', '/availability?restaurant_id=r1&date=2090-01-05&party_size=2&explain=true')[1]['slots']
        slot = next(s for s in slots if s['starts_at_local'].endswith('19:30'))
        self.assertNotIn('t1', slot['available_table_ids'])
        self.assertFalse(slot['explain'][0]['rules'][1]['holds'])
        self.assertTrue(all('t1' not in option['table_ids'] for option in slot['available_options']))
        old = self.book({**self.body, 'table_id': 't2'}, 'other')[1]
        before = copy.deepcopy(self.api.state)
        self.error(self.api.request('PATCH', '/reservations/' + old['reference'], self.headers, {'table_id': 't1'}), 409, 'table_unavailable')
        self.assertEqual(self.api.state, before)

    def test_manager_permissions_interval_validation_and_failed_key_reuse(self):
        self.error(self.preview(headers=self.bob), 403, 'forbidden')
        self.error(self.api.request('POST', '/restaurants/r1/replans', body={}), 401, 'unauthenticated')
        self.error(self.preview(tid='missing'), 404, 'not_found')
        for start, end in [('2090-01-05T18:00:00', '2090-01-05T19:00:00Z'),
                           ('2090-02-30T18:00:00Z', '2090-03-01T19:00:00Z'),
                           ('2090-01-05T19:00:00Z', '2090-01-05T18:00:00Z'),
                           ('2090-01-05T18:00:00Z', '2090-01-05T19:00:00+25:00'),
                           (False, '2090-01-05T19:00:00Z')]:
            before = copy.deepcopy(self.api.state)
            self.error(self.preview(start=start, end=end), 422, 'validation_failed')
            self.assertEqual(self.api.state, before)
        status, plan = self.preview()
        self.assertEqual(status, 201)
        self.error(self.preview(end='bad'), 409, 'idempotency_key_reuse')
        self.error(self.apply(plan, headers=self.bob), 403, 'forbidden')
        self.assertEqual(self.apply(plan)[0], 201)

    def test_stale_plan_and_other_restaurant_writes(self):
        fixture = test_stage3.fixture()
        other = copy.deepcopy(fixture['restaurants'][0]); other['id'] = 'r2'
        fixture['restaurants'].append(other)
        self.api.request('POST', '/_test/reset', body=fixture)
        self.headers = self.login('ada')
        original = self.book()[1]
        plan = self.preview()[1]
        # No-op and a different restaurant's closure preserve this plan's fence.
        self.api.request('PATCH', '/reservations/' + original['reference'], self.headers, {'party_size': 2})
        other_plan = self.preview(rid='r2', key='other-preview')[1]
        self.apply(other_plan, rid='r2', key='other-apply')
        self.assertEqual(self.apply(plan)[0], 201)
        new_plan = self.preview(tid='t2', key='second')[1]
        self.book({**self.body, 'starts_at_local': '2090-01-06T19:00'}, 'new')
        before = copy.deepcopy(self.api.state)
        self.error(self.apply(new_plan, 'stale'), 409, 'stale_plan')
        self.assertEqual(self.api.state, before)
        self.error(self.apply(new_plan, 'wrong-restaurant', rid='r2'), 404, 'not_found')

    def test_optimizer_reference_rank_ties_and_all_considered_bookings(self):
        fixture = test_stage3.fixture()
        r = fixture['restaurants'][0]
        r['tables'] = [{'id': 't' + str(i), 'label': str(i), 'capacity': 2} for i in range(4)]
        r['combinable'] = []
        fixture['reservations'] = [dict(self.body, table_id='t0', id='a', reference='AAAAAA', user_id='ada'),
                                   dict(self.body, table_id='t1', id='b', reference='BBBBBB', user_id='bob')]
        self.assertEqual(self.api.request('POST', '/_test/reset', body=fixture)[0], 204)
        self.headers = self.login('ada')
        plan = self.preview(tid='t0')[1]
        self.assertEqual(plan['assignments'], [{'reference': 'AAAAAA', 'table_ids': ['t2'], 'changed': True},
                                               {'reference': 'BBBBBB', 'table_ids': ['t1'], 'changed': False}])
        # Moving just the affected booking takes precedence over the lower rank vector
        # attainable by moving the other diner too.
        self.assertEqual(plan['moved_count'], 1)
        self.assertEqual(self.apply(plan)[0], 201)
        self.assertEqual(self.api.state['reservations']['BBBBBB']['revision'], 1)
        destination = service.Service()
        self.assertEqual(destination.request('POST', '/_test/import', body=self.api.request('GET', '/_test/export')[1])[0], 204)

    def test_no_feasible_plan_planning_limits_and_fixed_assignments(self):
        fixture = test_stage3.fixture(); r = fixture['restaurants'][0]
        r['tables'] = [{'id': 't1', 'label': '1', 'capacity': 2}]; r['combinable'] = []
        self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada'); self.book()
        before = copy.deepcopy(self.api.state)
        self.error(self.preview(), 409, 'no_feasible_plan')
        self.assertEqual(self.api.state, before)
        for count in (6, 7):
            fixture['reservations'] = []
            r['tables'] = [{'id': 't' + str(i), 'label': str(i), 'capacity': 2} for i in range(count)]
            self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada')
            result = self.preview(tid='t0')
            if count == 6:
                self.assertEqual(result[0], 201)
            else:
                self.error(result, 422, 'planning_limit')
        fixture = test_stage3.fixture()
        # The fixed booking doesn't overlap the closure, but overlaps a considered booking.
        fixture['reservations'] = [dict(self.body, id='a', reference='AAAAAA', user_id='ada'),
                                   dict(self.body, table_id='t3', starts_at_local='2090-01-05T20:30', id='b', reference='BBBBBB', user_id='bob')]
        self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada')
        plan = self.preview(start='2090-01-05T19:00:00+01:00', end='2090-01-05T19:30:00+01:00')[1]
        self.assertEqual(len(plan['assignments']), 1)
        self.assertEqual(plan['assignments'][0]['table_ids'], ['t3'])  # Adjacent to fixed booking.
        fixture['reservations'][1]['starts_at_local'] = '2090-01-05T20:00'
        self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada')
        plan = self.preview(start='2090-01-05T19:00:00+01:00', end='2090-01-05T19:30:00+01:00')[1]
        self.assertEqual(plan['assignments'][0]['table_ids'], ['t2'])  # Fixed occupancy excludes t3.

    def test_series_amend_exclusions_policies_history_counters_and_original_replay(self):
        anchor = self.book()[1]
        adopted = self.adopt(anchor['reference'], count=4)[1]; sid = adopted['series_id']
        refs = [o['reference'] for o in adopted['occurrences']]
        self.api.request('PATCH', '/reservations/' + refs[1], self.headers, {'starts_at_local': '2090-01-13T19:00'})
        self.api.request('POST', '/reservations/' + refs[2] + '/cancel', self.headers)
        self.publish(self.policy('2090-01-20', reservation_duration_minutes=60))
        before = copy.deepcopy(self.api.state)
        status, result = self.amend(sid, revision=3)
        self.assertEqual(status, 201)
        self.assertEqual(result['revision'], 4)
        self.assertEqual(self.api.state['restaurant_revisions']['r1'], before['restaurant_revisions']['r1'] + 1)
        for i, o in enumerate(result['occurrences']):
            res = o['reservation']
            if i in (0, 3):
                self.assertTrue(res['starts_at_local'].endswith('20:00'))
                self.assertEqual(res['revision'], 2)
                self.assertFalse(o['exception'])
                self.assertEqual(self.history(refs[i])[-1]['event'], 'changed')
            else:
                self.assertEqual(res, self.api.public(before['reservations'][refs[i]]))
        self.assertEqual(result['occurrences'][3]['reservation']['accepted_terms']['policy_version'], 1)
        self.assertEqual(self.amend(sid, revision=3), (200, result))
        self.api.request('POST', '/reservations/' + refs[3] + '/cancel', self.headers)
        self.assertEqual(self.adopt(anchor['reference'], count=4), (200, adopted))
        self.assertEqual(self.amend(sid, revision=3), (200, result))
        exported = self.api.request('GET', '/_test/export')[1]
        destination = service.Service()
        self.assertEqual(destination.request('POST', '/_test/import', body=exported)[0], 204)
        self.assertEqual(destination.state, self.api.state)

    def test_series_noop_empty_sets_stale_and_invalid_inputs_leave_state_unchanged(self):
        original = self.adopt(self.book()[1]['reference'])[1]; sid = original['series_id']
        for changes in ({'expected_revision': True}, {'expected_revision': 0}, {'from_index': -1},
                        {'from_index': 3}, {'from_index': False}, {'local_time': '4:00'}, {'local_time': '24:00'}, {'local_time': False}):
            body = {'expected_revision': 1, 'from_index': 0, 'local_time': '20:00', **changes}
            before = copy.deepcopy(self.api.state)
            self.error(self.api.request('POST', '/series/' + sid + '/amend', {**self.headers, 'Idempotency-Key': 'invalid'}, body), 422, 'validation_failed')
            self.assertEqual(self.api.state, before)
        self.error(self.amend(sid, revision=2), 409, 'stale_revision')
        before = copy.deepcopy(self.api.state)
        self.assertEqual(self.amend(sid, clock='19:00')[1], original)
        for field in ('reservations', 'histories', 'series', 'restaurant_revisions'):
            self.assertEqual(self.api.state[field], before[field])
        for occurrence in original['occurrences']:
            self.api.request('POST', '/reservations/' + occurrence['reference'] + '/cancel', self.headers)
        revision = self.api.state['series'][sid]['revision']
        self.assertEqual(self.amend(sid, revision=revision, key='empty')[1]['revision'], revision)
        self.error(self.amend(sid, revision=revision, headers=self.bob, key='owner'), 404, 'not_found')
        self.error(self.api.request('POST', '/series/' + sid + '/amend', body={}), 401, 'unauthenticated')

    def test_series_nonoccupancy_precedence_and_collective_rollback(self):
        original = self.adopt(self.book()[1]['reference'])[1]; sid = original['series_id']
        self.book({**self.body, 'starts_at_local': '2090-01-05T20:30'}, key='conflict')
        self.publish(self.policy('2090-01-12', opening_hours=[]))
        before = copy.deepcopy(self.api.state)
        self.error(self.amend(sid), 422, 'outside_opening_hours')
        self.assertEqual(self.api.state, before)
        self.publish(self.policy('2090-01-12'), key='restore')
        before = copy.deepcopy(self.api.state)
        self.error(self.amend(sid), 409, 'table_unavailable')
        self.assertEqual(self.api.state, before)
        self.assertEqual(self.amend(sid, clock='18:00')[0], 201)  # Failed key remains reusable.

    def test_repair_moves_series_once_preserves_exceptions_and_scheduled_dates(self):
        adopted = self.adopt(self.book()[1]['reference'])[1]; sid = adopted['series_id']
        ref = adopted['occurrences'][0]['reference']
        self.api.request('PATCH', '/reservations/' + ref, self.headers, {'party_size': 3})
        before = copy.deepcopy(self.api.state['series'][sid])
        plan = self.preview(end='2090-02-01T23:00:00Z')[1]
        self.assertEqual(self.apply(plan)[0], 201)
        current = self.api.state['series'][sid]
        self.assertEqual(current['revision'], before['revision'] + 1)
        self.assertEqual(current['scheduled_dates'], before['scheduled_dates'])
        self.assertEqual(current['occurrences'], before['occurrences'])
        self.assertEqual(self.amend(sid, revision=current['revision'])[0], 201)
        self.assertTrue(self.api.state['reservations'][ref]['starts_at_local'].endswith('19:00'))

    def test_concurrent_applications_and_series_expected_revision_are_serial(self):
        self.book(); plan = self.preview()[1]
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: self.apply(plan), range(50)))
        self.assertEqual([r[0] for r in results].count(201), 1)
        self.assertEqual([r[0] for r in results].count(200), 49)
        self.assertTrue(all(r[1] == results[0][1] for r in results))
        anchor = self.book({**self.body, 'starts_at_local': '2090-02-05T19:00'}, 'anchor')[1]
        adopted = self.adopt(anchor['reference'], key='adoption')[1]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: self.amend(adopted['series_id'], clock=item[0], key=item[1]),
                                    [('18:00', 'early'), ('20:00', 'late')]))
        self.assertEqual(sorted(r[0] for r in results), [201, 409])
        self.assertEqual(next(r[1]['error']['code'] for r in results if r[0] == 409), 'stale_revision')

    def test_actual_stage3_import_recovers_schedule_after_anchor_changed_before_adoption(self):
        spec = importlib.util.spec_from_file_location('stage3_upgrade_source', Path(__file__).resolve().parent.parent / 'stage-3/service.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        old = module.Service(); old.request('POST', '/_test/reset', body=test_stage3.fixture())
        token = old.request('POST', '/auth/login', body={'email': 'ada@example.com', 'password': 'correct horse'})[1]['token']
        headers = {'Authorization': 'Bearer ' + token}
        original = old.request('POST', '/reservations', {**headers, 'Idempotency-Key': 'original'}, self.body)[1]
        ref = original['reference']
        old.request('PATCH', '/reservations/' + ref, headers, {'starts_at_local': '2090-01-06T19:00'})
        adoption = old.request('POST', '/series', {**headers, 'Idempotency-Key': 'series'}, {'anchor_reference': ref, 'count': 4, 'interval_weeks': 1})[1]
        refs = [o['reference'] for o in adoption['occurrences']]
        old.request('PATCH', '/reservations/' + refs[1], headers, {'starts_at_local': '2090-01-14T19:00'})
        old.request('POST', '/reservations/' + refs[2] + '/cancel', headers)
        self.assertEqual(self.api.request('POST', '/_test/import', body=old.request('GET', '/_test/export')[1])[0], 204)
        self.headers = headers
        result = self.amend(adoption['series_id'], revision=3)[1]
        self.assertEqual(result['occurrences'][0]['reservation']['starts_at_local'], '2090-01-06T20:00')
        self.assertEqual(result['occurrences'][3]['reservation']['starts_at_local'], '2090-01-27T20:00')
        self.assertTrue(result['occurrences'][1]['exception'])
        self.assertEqual(self.book(key='original'), (200, original))
        self.assertEqual(self.api.request('POST', '/series', {**headers, 'Idempotency-Key': 'series'},
                                          {'anchor_reference': ref, 'count': 4, 'interval_weeks': 1}), (200, adoption))

    def test_optimizer_matches_exhaustive_oracle_for_pairs_capacities_and_rank_vector(self):
        rng = random.Random(4167)
        for attempt in range(25):
            fixture = test_stage3.fixture(); r = fixture['restaurants'][0]
            r['tables'] = [{'id': 't' + str(i), 'label': str(i), 'capacity': rng.randint(2, 8)} for i in range(5)]
            r['combinable'] = [['t0', 't1'], ['t2', 't3'], ['t1', 't4']]
            capacities = {t['id']: t['capacity'] for t in r['tables']}
            fixture['reservations'] = []
            for i in range(3):
                fixture['reservations'].append(dict(self.body, table_id='t' + str(i), party_size=rng.randint(1, capacities['t' + str(i)]),
                                                    id='res' + str(i), reference='REF00' + str(i), user_id='ada'))
            self.assertEqual(self.api.request('POST', '/_test/reset', body=fixture)[0], 204)
            self.headers = self.login('ada')
            options = [[t['id']] for t in r['tables']] + r['combinable']
            closed = 't' + str(rng.randrange(5))
            feasible = []
            for ranks in itertools.product(range(len(options)), repeat=3):
                selections = [options[rank] for rank in ranks]
                if any(closed in ids for ids in selections):
                    continue
                if any(set(selections[i]) & set(selections[j]) for i in range(3) for j in range(i)):
                    continue
                unused = [sum(capacities[t] for t in ids) - old['party_size'] for ids, old in zip(selections, fixture['reservations'])]
                if min(unused) < 0:
                    continue
                changed = sum(set(ids) != {old['table_id']} for ids, old in zip(selections, fixture['reservations']))
                feasible.append(((changed, sum(unused), ranks), selections))
            result = self.preview(tid=closed, key='oracle-' + str(attempt))
            if not feasible:
                self.error(result, 409, 'no_feasible_plan')
            else:
                score, selections = min(feasible)
                self.assertEqual(result[0], 201)
                plan = result[1]
                self.assertEqual([a['table_ids'] for a in plan['assignments']], selections)
                self.assertEqual((plan['moved_count'], plan['unused_seats']), score[:2])

    def test_prior_closures_and_combination_repair_preserve_accepted_capacity(self):
        first = self.preview(tid='t3')[1]; self.apply(first)
        old = self.book()[1]
        second = self.preview(tid='t1', key='second')[1]
        self.assertEqual(second['assignments'][0]['table_ids'], ['t2'])
        self.apply(second, key='second')
        # A pair is required when no single has sufficient accepted capacity.
        fixture = test_stage3.fixture()
        fixture['restaurants'][0]['combinable'].append(['t2', 't3'])
        self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada')
        body = {k: v for k, v in self.body.items() if k != 'table_id'}
        old = self.book({**body, 'table_ids': ['t2', 't1'], 'party_size': 6})[1]
        plan = self.preview()[1]
        self.assertEqual(plan['assignments'][0]['table_ids'], ['t2', 't3'])
        new = self.apply(plan)[1]['reservations'][0]
        self.assertNotIn('table_id', new)
        self.assertEqual(new['accepted_terms'], old['accepted_terms'])

    def test_supported_six_booking_plan_and_larger_booking_or_pair_counts(self):
        fixture = test_stage3.fixture(); r = fixture['restaurants'][0]
        r['tables'] = [{'id': 't' + str(i), 'label': str(i), 'capacity': 4} for i in range(6)]
        r['combinable'] = [['t0', 't1'], ['t1', 't2'], ['t2', 't3'], ['t4', 't5']]
        r['opening_hours'] = [{'weekday': day, 'opens': '00:00', 'closes': '23:59'} for day in service.DAYS]
        fixture['reservations'] = [dict(self.body, id='r' + str(i), reference='REF00' + str(i), user_id='ada',
                                       table_id='t0', starts_at_local='2090-01-05T' + str(i * 2).zfill(2) + ':00') for i in range(6)]
        self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada')
        started = time.monotonic()
        result = self.preview(tid='t0', start='2090-01-05T00:00:00+01:00', end='2090-01-05T23:59:00+01:00')
        self.assertEqual(result[0], 201)
        self.assertEqual(len(result[1]['assignments']), 6)
        self.assertLess(time.monotonic() - started, 5)
        fixture['reservations'].append(dict(self.body, id='r6', reference='REF006', user_id='ada', table_id='t0', starts_at_local='2090-01-05T12:00'))
        self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada')
        self.error(self.preview(tid='t0', start='2090-01-05T00:00:00+01:00', end='2090-01-05T23:59:00+01:00'), 422, 'planning_limit')
        fixture['reservations'] = []; r['combinable'].append(['t0', 't5'])
        self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada')
        self.error(self.preview(tid='t0'), 422, 'planning_limit')

    def test_series_dst_rollback_cutoff_and_operator_repair_exception(self):
        fixture = test_stage3.fixture()
        r = fixture['restaurants'][0]
        r['opening_hours'] = [{'weekday': day, 'opens': '00:00', 'closes': '05:00'} for day in service.DAYS]
        self.api.request('POST', '/_test/reset', body=fixture); self.headers = self.login('ada')
        class BeforeSpring(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 3, 1, tzinfo=timezone.utc)
        with patch('service.datetime', BeforeSpring):
            original = self.book({**self.body, 'starts_at_local': '2026-03-22T01:30'})[1]
            adopted = self.adopt(original['reference'], count=2)[1]
            before = copy.deepcopy(self.api.state)
            self.error(self.amend(adopted['series_id'], clock='02:30'), 422, 'invalid_local_time')
            self.assertEqual(self.api.state, before)
        # Current time is after these bookings: a real amendment is refused,
        # while a no-op collective amendment and operator repair remain supported.
        before = copy.deepcopy(self.api.state)
        self.error(self.amend(adopted['series_id'], clock='03:30'), 409, 'cutoff_passed')
        self.assertEqual(self.api.state, before)
        self.assertEqual(self.amend(adopted['series_id'], clock='01:30')[0], 201)
        plan = self.preview(start='2026-03-22T00:00:00+01:00', end='2026-03-30T05:00:00+02:00')[1]
        self.assertEqual(self.apply(plan)[0], 201)

    def test_invalid_plan_and_schedule_import_rejected_atomically(self):
        adopted = self.adopt(self.book()[1]['reference'])[1]
        plan = self.preview()[1]; self.apply(plan)
        export = self.api.request('GET', '/_test/export')[1]
        destination = service.Service()
        self.assertEqual(destination.request('POST', '/_test/import', body=export)[0], 204)
        for section, change in [('plans', lambda v: v[plan['plan_id']].update(moved_count=999)),
                                ('closures', lambda v: v[plan['plan_id']].update(to='bad')),
                                ('series', lambda v: v[adopted['series_id']]['scheduled_dates'].__setitem__(1, '2090-01-05'))]:
            invalid = copy.deepcopy(export); change(invalid['state'][section])
            before = copy.deepcopy(destination.state)
            self.error(destination.request('POST', '/_test/import', body=invalid), 422, 'validation_failed')
            self.assertEqual(destination.state, before)


if __name__ == '__main__':
    unittest.main()
