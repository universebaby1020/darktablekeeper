'use strict';

const app = document.getElementById('app');
const storageKey = 'tablekeeper-session';
let session = null;
try { session = JSON.parse(localStorage.getItem(storageKey)); } catch (_) { /* Start signed out. */ }
let searchVersion = 0;
let searchContext = null;
let selection = null;
let lookupVersion = 0;

class Rejection extends Error {
  constructor(status, error) {
    super(error?.message || 'We could not complete this request. Please try again.');
    this.status = status;
    this.code = error?.code;
  }
}

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...options.headers };
  if (session?.token) headers.Authorization = 'Bearer ' + session.token;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 204) return null;
  const value = await response.json();
  if (!response.ok) {
    // A server failure cannot establish whether a submitted booking committed.
    if (response.status >= 500) throw new Error('The connection did not confirm the result.');
    throw new Rejection(response.status, value.error);
  }
  return value;
}

function el(tag, attributes = {}, text = '') {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === 'class') node.className = value;
    else node.setAttribute(name, value);
  }
  if (text) node.textContent = text;
  return node;
}

function removeTest(id, scope = document) {
  scope.querySelectorAll(`[data-testid="${id}"]`).forEach(node => node.remove());
}

function feedback(container, id, text, kind = 'error') {
  removeTest(id, container);
  const node = el('p', { class: 'feedback ' + kind, 'data-testid': id, role: kind === 'error' ? 'alert' : 'status' }, text);
  container.append(node);
  return node;
}

function bringIntoView(node) {
  node.focus({ preventScroll: true });
  node.scrollIntoView({ behavior: 'auto', block: 'nearest' });
}

function localDescription(local) {
  const [day, time] = local.split('T');
  const readable = new Intl.DateTimeFormat('en', { month: 'long', day: 'numeric', year: 'numeric' }).format(new Date(day + 'T12:00:00'));
  return `${readable} at ${time}`;
}

function tableLabels(restaurant, ids) {
  return ids.map(id => restaurant.tables.find(table => table.id === id)?.label || 'Table').join(' & ');
}

function seatingName(restaurant, ids) {
  return (ids.length === 1 ? 'Table ' : 'Tables ') + tableLabels(restaurant, ids);
}

function requestKey() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
}

function renderAccount() {
  const nav = document.getElementById('account-nav');
  nav.replaceChildren();
  if (session?.token) {
    nav.append(el('span', { class: 'current-user', 'data-testid': 'current-user' }, session.display_name));
    const logout = el('button', { type: 'button', class: 'quiet-button', 'data-testid': 'logout-button' }, 'Log out');
    logout.onclick = () => {
      session = null;
      localStorage.removeItem(storageKey);
      selection = null;
      document.getElementById('booking-column')?.replaceChildren();
      removeTest('reservation-detail');
      renderAccount();
    };
    nav.append(logout);
  } else {
    nav.append(el('a', { href: '/login' }, 'Log in'), el('a', { href: '/signup' }, 'Sign up'));
  }
  document.querySelectorAll('nav a').forEach(link => {
    if (link.getAttribute('href') === location.pathname) link.setAttribute('aria-current', 'page');
  });
}

function renderAuth(signup) {
  const prefix = signup ? 'signup' : 'login';
  app.innerHTML = `<div class="auth-layout"><section class="auth-copy"><p class="eyebrow">A seat for you</p>
    <h1>${signup ? 'Make yourself<br>at home.' : 'Welcome back<br>to the table.'}</h1>
    <p>${signup ? 'Create your account to book a table and keep every reservation close at hand.' : 'Log in to reserve your next meal or find the details of an upcoming visit.'}</p></section>
    <section class="panel"><form id="auth-form" class="auth-form">
    ${signup ? '<div class="field"><label for="auth-name">Your name</label><input id="auth-name" data-testid="signup-display-name" autocomplete="name" required></div>' : ''}
    <div class="field"><label for="auth-email">Email address</label><input id="auth-email" data-testid="${prefix}-email" type="email" autocomplete="email" required></div>
    <div class="field"><label for="auth-password">Password</label><input id="auth-password" data-testid="${prefix}-password" type="password" autocomplete="${signup ? 'new-password' : 'current-password'}" ${signup ? 'minlength="8"' : ''} required></div>
    <button class="primary-button" data-testid="${prefix}-submit" type="submit">${signup ? 'Create account' : 'Log in'}</button></form>
    <p class="auth-switch">${signup ? 'Already have an account? <a href="/login">Log in</a>' : 'New here? <a href="/signup">Create an account</a>'}</p></section></div>`;
  const form = document.getElementById('auth-form');
  form.onsubmit = async event => {
    event.preventDefault();
    removeTest('auth-error', form);
    const button = form.querySelector('button');
    button.disabled = true;
    const original = button.textContent;
    button.textContent = 'One moment…';
    const body = { email: document.getElementById('auth-email').value, password: document.getElementById('auth-password').value };
    if (signup) body.display_name = document.getElementById('auth-name').value;
    try {
      session = await api('/auth/' + prefix, { method: 'POST', body: JSON.stringify(body) });
      localStorage.setItem(storageKey, JSON.stringify(session));
      renderAccount();
      location.assign('/');
    } catch (error) {
      feedback(form, 'auth-error', error instanceof Rejection ? error.message : 'We could not connect. Please try again.');
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  };
}

function renderSearch() {
  app.innerHTML = `<section class="hero"><div class="hero-copy"><p class="eyebrow">A place at the table</p>
    <h1>A good meal begins<br>with a little together.</h1><p>Find a welcoming spot, choose your time, and make room for the people you love.</p></div>
    <div class="table-art" aria-hidden="true"><span>pull up<br>a chair.</span></div></section>
    <section class="panel"><form id="search-form" class="search-form">
    <div class="field"><label for="restaurant">Restaurant</label><select id="restaurant" data-testid="restaurant-select" required><option value="">Loading restaurants…</option></select></div>
    <div class="field"><label for="date">Date</label><input id="date" data-testid="date-input" type="date" required></div>
    <div class="field"><label for="party-size">Guests</label><input id="party-size" data-testid="party-size-input" type="number" value="2" min="1" step="1" required></div>
    <button class="primary-button" data-testid="search-button" type="submit">Find a table</button></form>
    <p class="search-note">All times are local to your chosen restaurant.</p><div id="search-feedback"></div></section>
    <div id="results-heading"></div><div class="flow-layout"><section id="results" aria-live="polite"><div class="empty-state"><h3>Where shall we meet?</h3><p>Choose a restaurant and date to see its tables.</p></div></section><aside id="booking-column" class="booking-column" aria-label="Your booking"></aside></div>`;
  const today = new Date();
  document.getElementById('date').value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  api('/restaurants').then(value => {
    const select = document.getElementById('restaurant');
    select.replaceChildren();
    value.restaurants.forEach(restaurant => select.append(el('option', { value: restaurant.id }, restaurant.name)));
    if (!value.restaurants.length) {
      select.append(el('option', { value: '' }, 'No restaurants available'));
      document.getElementById('results').innerHTML = '<div class="empty-state"><h3>We’re setting the table.</h3><p>No restaurants are available yet. Please check back later.</p></div>';
    }
  }).catch(() => feedback(document.getElementById('search-feedback'), 'search-error', 'Restaurants could not load. Please refresh this page to try again.'));
  document.getElementById('search-form').onsubmit = event => {
    event.preventDefault();
    runSearch({ restaurant_id: document.getElementById('restaurant').value,
      date: document.getElementById('date').value, party_size: Number(document.getElementById('party-size').value) });
  };
  document.getElementById('restaurant').onchange = () => bringIntoView(document.getElementById('date'));
  document.getElementById('date').onchange = () => bringIntoView(document.getElementById('party-size'));
  document.getElementById('party-size').onchange = () => bringIntoView(document.querySelector('[data-testid="search-button"]'));
}

async function runSearch(query, preserveSelection = false) {
  const version = ++searchVersion;
  removeTest('auth-error');
  document.getElementById('search-feedback').replaceChildren();
  document.getElementById('results-heading').replaceChildren();
  if (!preserveSelection) {
    selection = null;
    searchContext = null;
    document.getElementById('booking-column').replaceChildren();
  }
  const results = document.getElementById('results');
  results.replaceChildren(el('p', { class: 'feedback loading', role: 'status' }, 'Finding a place for you…'));
  results.setAttribute('aria-busy', 'true');
  try {
    const params = new URLSearchParams(query);
    const [availability, restaurant] = await Promise.all([api('/availability?' + params), api('/restaurants/' + encodeURIComponent(query.restaurant_id))]);
    if (version !== searchVersion) return;
    searchContext = { query: { ...query }, restaurant, availability };
    renderGrid(searchContext);
    if (!preserveSelection) {
      results.setAttribute('tabindex', '-1');
      bringIntoView(results.querySelector('[data-available="true"]') || results);
    }
  } catch (error) {
    if (version !== searchVersion) return;
    results.replaceChildren();
    feedback(results, 'search-error', error instanceof Rejection ? error.message : 'We could not check the tables. Please search again.');
  } finally {
    if (version === searchVersion) results.setAttribute('aria-busy', 'false');
  }
}

function renderGrid(context) {
  const { availability, restaurant, query } = context;
  const results = document.getElementById('results');
  const heading = document.getElementById('results-heading');
  heading.replaceChildren();
  const top = el('div', { class: 'section-top' });
  const copy = el('div');
  copy.append(el('h2', {}, restaurant.name), el('p', {}, localDescription(query.date + 'T00:00').split(' at ')[0] + ` · ${query.party_size} ${query.party_size === 1 ? 'guest' : 'guests'}`));
  const legend = el('div', { class: 'legend' });
  legend.append(el('span', {}, 'Available'), el('span', {}, 'Unavailable'));
  top.append(copy, legend);
  heading.append(top);
  results.replaceChildren();
  if (!availability.slots.length) {
    const empty = el('div', { class: 'empty-state', 'data-testid': 'no-slots' });
    empty.append(el('h3', {}, 'No tables on this date.'), el('p', {}, 'Try a different day to find your next meal together.'));
    results.append(empty);
    return;
  }
  const grid = el('div', { 'data-testid': 'availability-grid' });
  for (const slot of availability.slots) {
    const card = el('div', { class: 'slot-card' });
    const time = slot.starts_at_local.slice(11);
    const title = el('div', { class: 'slot-heading' });
    title.append(el('p', { class: 'slot-time' }, time), el('p', { class: 'slot-caption' }, 'Local time'));
    const choices = el('div', { class: 'seating-options' });
    const options = restaurant.tables.map(table => ({ table_ids: [table.id], capacity: slot.available_options?.find(option => option.table_ids.length === 1 && option.table_ids[0] === table.id)?.capacity ?? table.capacity,
      available: slot.available_table_ids.includes(table.id) }));
    for (const pair of restaurant.combinable || []) {
      const option = slot.available_options?.find(candidate => candidate.table_ids.length === 2 && candidate.table_ids.every((id, index) => id === pair[index]));
      if (option) options.push({ ...option, available: true });
    }
    for (const option of options) {
      const ids = option.table_ids;
      const button = el('button', { type: 'button', class: 'seat' + (ids.length > 1 ? ' combo' : ''),
        'data-testid': `slot-${ids.join('+')}-${time}`, 'data-available': String(option.available),
        'aria-pressed': String(Boolean(selection && selection.body.starts_at_local === slot.starts_at_local && selection.body.restaurant_id === restaurant.id && ids.join('|') === selection.ids.join('|'))) });
      button.disabled = !option.available;
      button.append(el('strong', {}, seatingName(restaurant, ids)), el('small', {}, option.available ? (ids.length > 1 ? 'Together · ' : '') + `Seats ${option.capacity}` : 'Unavailable'));
      button.onclick = () => {
        if (!session?.token) {
          const error = feedback(document.getElementById('search-feedback'), 'auth-error', 'Please log in or create an account to book your table.');
          const link = el('a', { href: '/login' }, ' Log in');
          error.append(link);
          bringIntoView(link);
          return;
        }
        openBooking(context, slot, ids);
      };
      choices.append(button);
    }
    card.append(title, choices);
    grid.append(card);
  }
  results.append(grid);
}

function openBooking(context, slot, ids) {
  const body = { restaurant_id: context.restaurant.id, starts_at_local: slot.starts_at_local, party_size: context.query.party_size };
  if (ids.length === 1) body.table_id = ids[0];
  else body.table_ids = [...ids];
  selection = { body, ids: [...ids], restaurant: context.restaurant, query: { ...context.query }, pending: null };
  renderGrid(context);
  const column = document.getElementById('booking-column');
  column.innerHTML = `<section class="panel booking-panel" data-testid="booking-form"><p class="eyebrow">Your seat awaits</p><h2>Make it a date.</h2>
    <p class="booking-summary" data-testid="booking-summary"></p><form id="booking-fields"><div class="field"><label for="booking-party">Guests at your table</label>
    <input id="booking-party" data-testid="booking-party-size" type="number" min="1" step="1" required></div>
    <button type="submit" class="primary-button" data-testid="booking-submit">Reserve this table</button></form><p class="fine-print">Your table is reserved when you receive a confirmation reference.</p><div id="booking-feedback"></div><div id="booking-confirmation"></div></section>`;
  column.querySelector('[data-testid="booking-summary"]').textContent = seatingName(context.restaurant, ids) + ' · ' + localDescription(slot.starts_at_local);
  const input = document.getElementById('booking-party');
  input.value = body.party_size;
  input.oninput = () => {
    selection.body.party_size = Number(input.value);
    selection.pending = null;
    document.getElementById('booking-feedback').replaceChildren();
    document.getElementById('booking-confirmation').replaceChildren();
  };
  document.getElementById('booking-fields').onsubmit = submitBooking;
  bringIntoView(column.querySelector('[data-testid="booking-submit"]'));
}

async function submitBooking(event) {
  event.preventDefault();
  const current = selection;
  if (!current) return;
  const input = document.getElementById('booking-party');
  current.body.party_size = Number(input.value);
  const signature = JSON.stringify(current.body);
  if (!current.pending || current.pending.body !== signature) current.pending = { key: requestKey(), body: signature };
  const pending = { ...current.pending };
  const button = document.querySelector('[data-testid="booking-submit"]');
  const messages = document.getElementById('booking-feedback');
  messages.replaceChildren();
  document.getElementById('booking-confirmation').replaceChildren();
  button.disabled = true;
  input.disabled = true;
  button.textContent = 'Reserving your table…';
  try {
    const reservation = await api('/reservations', { method: 'POST', headers: { 'Idempotency-Key': pending.key }, body: pending.body });
    if (selection !== current) return;
    // The write receipt is immutable. A seating repair may have changed the booking
    // since that receipt was recorded, so confirmation uses an authoritative read.
    button.textContent = 'Checking your reservation…';
    try {
      const latest = await api('/reservations/' + encodeURIComponent(reservation.reference));
      if (selection !== current) return;
      if (latest.reference !== reservation.reference || latest.restaurant_id !== current.restaurant.id) {
        throw new Error('Reservation details did not match the confirmation.');
      }
      renderConfirmation(latest, current.restaurant);
      messages.replaceChildren();
    } catch (_) {
      if (selection !== current) return;
      document.getElementById('booking-confirmation').replaceChildren();
      feedback(messages, 'booking-uncertain', 'Your reservation was confirmed, but we could not load its current seating details. Retry without changing the form to safely view the same reservation.', 'uncertain');
    }
  } catch (error) {
    if (selection !== current) return;
    if (error instanceof Rejection) {
      feedback(messages, 'booking-error', error.code === 'table_unavailable' ? 'This table has just been reserved. We’re refreshing the available choices; your details are saved.' : error.message);
      if (error.code === 'table_unavailable' && searchContext?.query.restaurant_id === current.query.restaurant_id && searchContext?.query.date === current.query.date) {
        await runSearch({ ...current.query }, true);
      }
    } else {
      feedback(messages, 'booking-uncertain', 'We haven’t received a confirmation yet. Your request may have arrived. Retry without changing these details to safely check the same reservation.', 'uncertain');
    }
  } finally {
    if (selection === current) {
      input.disabled = false;
      button.disabled = false;
      button.textContent = messages.querySelector('[data-testid="booking-uncertain"]') ? 'Retry this reservation' : 'Reserve this table';
    }
  }
}

function renderConfirmation(reservation, restaurant) {
  const ids = reservation.table_ids || [reservation.table_id];
  const confirmation = el('div', { class: 'confirmation', 'data-testid': 'confirmation', tabindex: '-1' });
  confirmation.append(el('p', { class: 'eyebrow' }, reservation.status === 'cancelled' ? 'This reservation is cancelled' : 'You’re on the guest list'),
    el('div', { class: 'reference', 'data-testid': 'confirmation-reference' }, reservation.reference),
    el('p', { 'data-testid': 'confirmation-details' }, restaurant.name + ' · ' + seatingName(restaurant, ids) + ' · ' + localDescription(reservation.starts_at_local)),
    el('p', { 'data-testid': 'confirmation-tables' }, seatingName(restaurant, ids)),
    el('a', { href: '/lookup?reference=' + encodeURIComponent(reservation.reference) }, 'View or cancel your reservation'));
  document.getElementById('booking-confirmation').replaceChildren(confirmation);
  bringIntoView(confirmation);
}

function renderLookup() {
  app.innerHTML = `<section class="lookup-layout"><p class="eyebrow">Your plans, all in one place</p><h1>We’ve kept your place.</h1>
    <p class="lookup-intro">Enter your confirmation reference to view the details of your visit or cancel a reservation.</p>
    <div class="panel"><form class="lookup-form" id="lookup-form"><div class="field"><label for="lookup-reference">Confirmation reference</label>
    <input id="lookup-reference" data-testid="lookup-reference-input" required maxlength="64" autocomplete="off" placeholder="Your reference"></div>
    <button class="primary-button" type="submit" data-testid="lookup-submit">Find reservation</button></form><div id="lookup-feedback"></div></div><div id="lookup-detail"></div></section>`;
  document.getElementById('lookup-reference').value = new URLSearchParams(location.search).get('reference') || '';
  document.getElementById('lookup-form').onsubmit = async event => {
    event.preventDefault();
    const version = ++lookupVersion;
    const reference = document.getElementById('lookup-reference').value.trim();
    document.getElementById('lookup-feedback').replaceChildren();
    document.getElementById('lookup-detail').replaceChildren();
    feedback(document.getElementById('lookup-feedback'), 'lookup-loading', 'Finding your reservation…', 'loading');
    try {
      const reservation = await api('/reservations/' + encodeURIComponent(reference));
      const restaurant = await api('/restaurants/' + encodeURIComponent(reservation.restaurant_id));
      if (version !== lookupVersion) return;
      document.getElementById('lookup-feedback').replaceChildren();
      renderReservation(reservation, restaurant);
    } catch (error) {
      if (version !== lookupVersion) return;
      document.getElementById('lookup-feedback').replaceChildren();
      feedback(document.getElementById('lookup-feedback'), 'reservation-error', error instanceof Rejection && error.status === 401 ? 'Please log in to view your reservation.' : error instanceof Rejection ? error.message : 'We could not connect. Please try again.');
    }
  };
}

function renderReservation(reservation, restaurant) {
  const ids = reservation.table_ids || [reservation.table_id];
  const detail = el('section', { class: 'panel reservation-detail', 'data-testid': 'reservation-detail', tabindex: '-1' });
  const title = el('div', { class: 'detail-heading' });
  title.append(el('h2', {}, restaurant.name), el('span', { class: 'status ' + reservation.status, 'data-testid': 'reservation-status' }, reservation.status));
  detail.append(title);
  const metadata = el('div', { class: 'detail-meta' });
  metadata.append(el('p', { 'data-testid': 'reservation-tables' }, seatingName(restaurant, ids)),
    el('p', {}, localDescription(reservation.starts_at_local)), el('p', {}, `${reservation.party_size} guests · Reference ${reservation.reference}`));
  detail.append(metadata);
  if (reservation.status === 'confirmed') {
    const cancel = el('button', { type: 'button', class: 'secondary-button', 'data-testid': 'reservation-cancel-button' }, 'Cancel reservation');
    cancel.onclick = async () => {
      removeTest('reservation-error', detail);
      cancel.disabled = true;
      cancel.textContent = 'Cancelling…';
      try {
        const changed = await api('/reservations/' + encodeURIComponent(reservation.reference) + '/cancel', { method: 'POST', body: '{}' });
        renderReservation(changed, restaurant);
      } catch (error) {
        feedback(detail, 'reservation-error', error instanceof Rejection ? error.message : 'We could not confirm the cancellation. Look up your reservation again to check its status.');
        cancel.disabled = false;
        cancel.textContent = 'Cancel reservation';
      }
    };
    detail.append(cancel);
  } else detail.append(el('p', { class: 'fine-print' }, 'This reservation has been cancelled. We hope to welcome you another time.'));
  document.getElementById('lookup-detail').replaceChildren(detail);
  bringIntoView(detail);
}

renderAccount();
if (location.pathname === '/signup') renderAuth(true);
else if (location.pathname === '/login') renderAuth(false);
else if (location.pathname === '/lookup') renderLookup();
else renderSearch();
