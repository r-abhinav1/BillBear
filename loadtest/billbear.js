// k6 load test: whole tables splitting a bill, the way phones really use BillBear.
//
// Each k6 virtual user is one table: the host opens a room from a saved scan
// (no photo, so no paid Gemini call), the guests join, every phone polls the
// room every 3 seconds while people pick, everyone sends picks, the host
// splits, and everyone opens the results and downloads the PDF.
//
// The server must have LOAD_TEST_TOKEN set (32+ characters); requests carry it
// in X-Load-Test so the bot check and per-network limits don't stop a test run
// from one machine. Scans keep all their limits. See loadtest/README.md.
//
//   k6 run -e BASE_URL=http://localhost:5055 -e LOAD_TEST_TOKEN=... loadtest/billbear.js
//
// Knobs (-e NAME=value): TABLES (peak tables at once, 10), PHONES (per table, 6),
// PICK_SECONDS (polling time while picking, 60), RAMP (1m), HOLD (3m),
// PDF (1 = everyone downloads it, 0 = nobody). Against anything but localhost
// also pass -e CONFIRM_REMOTE=yes: the run uses real CPU on that server.
import http from 'k6/http';
import { check, sleep, fail } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE = (__ENV.BASE_URL || 'http://localhost:5055').replace(/\/$/, '');
const TOKEN = __ENV.LOAD_TEST_TOKEN || '';
const TABLES = parseInt(__ENV.TABLES || '10', 10);
const PHONES = Math.max(2, parseInt(__ENV.PHONES || '6', 10));
const PICK_SECONDS = parseInt(__ENV.PICK_SECONDS || '60', 10);
const POLL_SECONDS = 3;
const PDF = (__ENV.PDF || '1') !== '0';

if (TOKEN.length < 32) fail('Set -e LOAD_TEST_TOKEN=<the same 32+ character secret the server has>.');
if (!/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(BASE) && __ENV.CONFIRM_REMOTE !== 'yes') {
    fail(`${BASE} is not local. Pass -e CONFIRM_REMOTE=yes if you mean it (it uses that server's CPU budget).`);
}

export const options = {
    scenarios: {
        tables: {
            executor: 'ramping-vus',
            startVUs: 0,
            stages: [
                { duration: __ENV.RAMP || '1m', target: TABLES },
                { duration: __ENV.HOLD || '3m', target: TABLES },
                { duration: '30s', target: 0 },
            ],
            gracefulRampDown: '2m',     // let tables that are mid-split finish
        },
    },
    thresholds: {
        http_req_failed: ['rate<0.01'],
        checks: ['rate>0.99'],
        'http_req_duration{name:status}': ['p(95)<800'],
        'http_req_duration{name:pdf}': ['p(95)<4000'],
    },
    summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

const tablesDone = new Counter('tables_split');
const tableSeconds = new Trend('table_seconds', true);

// A saved scan, like a .billbear.json file from "Save this scan to a file".
const RECEIPT = JSON.stringify({
    version: 2,
    source: 'billbear_edit_items',
    currency: { code: 'INR', confirmed: true },
    extraction_meta: { mode: 'saved_json', source: 'user_saved_json', provider: 'none' },
    items: [
        ['Paneer Tikka', 330, 'veg'], ['Butter Chicken', 420, 'nonveg'], ['Dal Makhani', 280, 'veg'],
        ['Garlic Naan', 60, 'veg', 4], ['Jeera Rice', 180, 'veg'], ['Chicken Biryani', 360, 'nonveg'],
        ['Masala Dosa', 120, 'veg', 3], ['Gulab Jamun', 90, 'veg', 2], ['Kingfisher', 250, 'alcohol', 2],
        ['Fresh Lime Soda', 110, 'veg'], ['Mutton Rogan Josh', 480, 'nonveg'], ['Raita', 70, 'veg'],
    ].map(([name, price, category, quantity]) => ({ name, price, category, quantity: quantity || 1 })),
    subtotal: 2750,
    charges: [
        { label: 'CGST', amount: 68.75, kind: 'charge' },
        { label: 'SGST', amount: 68.75, kind: 'charge' },
        { label: 'Service charge', amount: 137.5, kind: 'charge' },
    ],
    total: 3025,
});

function params(phone, name, extra) {
    return Object.assign({
        jar: phone.jar,
        headers: Object.assign({ 'X-Load-Test': TOKEN, Origin: BASE }, (extra && extra.headers) || {}),
        tags: { name },
    }, extra && extra.redirects !== undefined ? { redirects: extra.redirects } : {});
}

function newPhone(name) {
    return { name, jar: new http.CookieJar() };
}

export default function () {
    const started = Date.now();
    const host = newPhone('Host');

    // The host opens a room from the saved scan and lands on Check the bill.
    let res = http.post(`${BASE}/create`, {
        host_name: 'Host', room_name: `k6 load test ${__VU}-${__ITER}`, num_people: String(PHONES),
        receipt_json: RECEIPT, mode: 'json',
    }, params(host, 'create'));
    const code = (res.url.match(/\/edit\/([A-Z0-9]{6})/) || [])[1];
    if (!check(res, { 'room created': () => res.status === 200 && !!code })) {
        console.error(`create failed: ${res.status} ${res.url} ${String(res.body).slice(0, 200)}`);
        sleep(5);
        return;
    }
    http.get(`${BASE}/room/${code}`, params(host, 'room'));

    // Guests join on their own phones.
    const phones = [host];
    for (let i = 1; i < PHONES; i++) {
        const guest = newPhone(`Guest ${i}`);
        const r = http.post(`${BASE}/join/${code}`, { user_name: guest.name }, params(guest, 'join'));
        if (check(r, { 'guest joined': () => r.status === 200 && r.url.includes('/select-items') })) phones.push(guest);
        sleep(Math.random() * 2);
    }

    // Everyone opens the pick screen; the dish ids come from it.
    let ids = [];
    for (const phone of phones) {
        const r = http.get(`${BASE}/room/${code}/user/${encodeURIComponent(phone.name)}/select-items`, params(phone, 'pick_page'));
        check(r, { 'pick screen': () => r.status === 200 });
        if (!ids.length) ids = [...String(r.body).matchAll(/name="selected_items" value="(\d+)"/g)].map(m => m[1]);
    }

    // Picking: every phone polls the room every 3 seconds, as the page does.
    for (let t = 0; t < PICK_SECONDS; t += POLL_SECONDS) {
        for (const phone of phones) {
            const r = http.get(`${BASE}/room/${code}/status`, params(phone, 'status'));
            check(r, { 'status ok': () => r.status === 200 });
        }
        sleep(POLL_SECONDS);
    }

    // Everyone sends a couple of dishes (the host takes whatever is left).
    phones.forEach((phone, i) => {
        const mine = phone === host ? ids.filter((_, j) => j % phones.length === 0) : [ids[i % ids.length], ids[(i + 5) % ids.length]];
        // Several dishes = the same form field repeated, as the browser sends it.
        const body = mine.map(id => `selected_items=${id}`).concat(`submission_id=k6-${__VU}-${__ITER}-${i}`).join('&');
        const r = http.post(`${BASE}/room/${code}/user/${encodeURIComponent(phone.name)}/select-items`, body,
            params(phone, 'send_picks', { headers: { 'X-Requested-With': 'fetch', 'Content-Type': 'application/x-www-form-urlencoded' } }));
        check(r, { 'picks sent': () => r.status === 200 });
    });

    // The host splits the bill.
    res = http.post(`${BASE}/room/${code}/finalize`, '{}',
        params(host, 'finalize', { headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' } }));
    check(res, { 'split done': () => res.status === 200 });

    // Everyone opens the split, and downloads the PDF.
    for (const phone of phones) {
        const r = http.get(`${BASE}/room/${code}/results`, params(phone, 'results'));
        check(r, { 'results page': () => r.status === 200 && String(r.body).includes('pays') });
        if (PDF) {
            const p = http.get(`${BASE}/room/${code}/download`, params(phone, 'pdf'));
            check(p, { 'pdf': () => p.status === 200 && String(p.headers['Content-Type']).includes('pdf') });
        }
    }
    tablesDone.add(1);
    tableSeconds.add(Date.now() - started);
    sleep(2);
}
