import http from 'k6/http';
import { check, randomSeed } from 'k6';

// The endpoint is intentionally not configurable. Scenario inputs can vary
// bounded rates and timing, but cannot redirect this runner outside our mesh.
const TARGET = 'http://frontend-proxy:8080';
const runId = __ENV.SENTINEL_RUN_ID;
const journey = __ENV.SENTINEL_JOURNEY || 'browse';
const userAgent = __ENV.SENTINEL_USER_AGENT;
const schedule = journey === 'checkout'
  ? {
      target: 'astronomy-shop/frontend-proxy',
      request_mix_seed: 1,
      phases: [{
        name: 'checkout',
        rate_rps: Number(__ENV.SENTINEL_RATE_RPS),
        duration_seconds: Number(__ENV.SENTINEL_DURATION_SECONDS),
        start_offset_seconds: 0,
      }],
    }
  : journey === 'path_attack'
    ? {
        target: 'astronomy-shop/frontend-proxy',
        request_mix_seed: 1,
        phases: [{
          name: 'path_attack',
          rate_rps: Number(__ENV.SENTINEL_RATE_RPS),
          duration_seconds: Number(__ENV.SENTINEL_DURATION_SECONDS),
          start_offset_seconds: 0,
        }],
      }
    : JSON.parse(__ENV.SENTINEL_SCHEDULE);

if (schedule.target !== 'astronomy-shop/frontend-proxy') {
  throw new Error(`unsupported contained target: ${schedule.target}`);
}
if (!['browse', 'checkout', 'path_attack'].includes(journey)) {
  throw new Error(`unsupported contained journey: ${journey}`);
}

const scenarios = {};
for (const phase of schedule.phases) {
  if (phase.rate_rps < 1 || phase.rate_rps > 50) {
    throw new Error(`phase rate outside 1..50 rps: ${phase.rate_rps}`);
  }
  // Checkout performs three sequential requests and successful downstream
  // calls can take fifteen seconds while the deliberately pressured service
  // recovers. Preallocate for twenty seconds of concurrency so k6
  // delivers the requested arrival rate without relaxing the
  // zero-dropped-iterations honesty gate. Browse traffic keeps its tighter
  // budget.
  const preAllocatedVUs = journey === 'checkout'
    ? Math.min(Math.max(phase.rate_rps * 20, 10), 50)
    : Math.min(Math.max(phase.rate_rps, 2), 20);
  scenarios[phase.name] = {
    executor: 'constant-arrival-rate',
    rate: phase.rate_rps,
    timeUnit: '1s',
    duration: `${phase.duration_seconds}s`,
    startTime: `${phase.start_offset_seconds}s`,
    preAllocatedVUs,
    maxVUs: 50,
    gracefulStop: '0s',
  };
}

export const options = {
  scenarios,
  thresholds: journey === 'checkout'
    ? { dropped_iterations: ['count==0'] }
    : { checks: ['rate>0.99'], dropped_iterations: ['count==0'] },
};

const paths = ['/', '/api/products'];
randomSeed(schedule.request_mix_seed);

export function setup() {
  if (journey === 'checkout') {
    return;
  }
  // The collector may sample an individual trace. A tiny separate burst makes
  // the phase origin observable without putting marker traffic in detector
  // input; the runner anchors on the last observed marker.
  for (let index = 0; index < 10; index += 1) {
    http.get(`${TARGET}/`, {
      headers: { 'User-Agent': `sentinel-score-anchor/${runId}` },
    });
  }
}

export default function () {
  if (journey === 'checkout') {
    checkout();
    return;
  }
  if (journey === 'path_attack') {
    pathAttack();
    return;
  }
  const path = paths[Math.floor(Math.random() * paths.length)];
  const response = http.get(`${TARGET}${path}`, {
    headers: { 'User-Agent': `sentinel-score/${runId}` },
  });
  check(response, { 'testbed response below 500': (result) => result.status < 500 });
}

function pathAttack() {
  const path = __ENV.SENTINEL_ATTACK_PATH;
  if (path !== '/' || !userAgent || !userAgent.startsWith('sentinel-score/')) {
    throw new Error('invalid contained path-attack input');
  }
  const response = http.get(`${TARGET}${path}`, {
    headers: { 'User-Agent': userAgent },
  });
  check(response, { 'path attack response below 500': (result) => result.status < 500 });
}

function checkout() {
  const userId = `${runId}-${__VU}-${__ITER}`;
  const headers = {
    'Content-Type': 'application/json',
    'User-Agent': `sentinel-stimulus/${runId}`,
  };
  const productId = '0PUK6V6EV0';
  const product = http.get(`${TARGET}/api/products/${productId}`, { headers });
  const cart = http.post(
    `${TARGET}/api/cart`,
    JSON.stringify({ item: { productId, quantity: 1 }, userId }),
    { headers },
  );
  const order = http.post(
    `${TARGET}/api/checkout`,
    JSON.stringify({
      userId,
      email: 'sentinel-checkout@example.com',
      address: {
        streetAddress: '1600 Amphitheatre Parkway',
        zipCode: '94043',
        city: 'Mountain View',
        state: 'CA',
        country: 'United States',
      },
      userCurrency: 'USD',
      creditCard: {
        creditCardNumber: '4432-8015-6152-0454',
        creditCardExpirationMonth: 1,
        creditCardExpirationYear: 2039,
        creditCardCvv: 672,
      },
    }),
    { headers },
  );
  check(product, { 'checkout product request completed': (result) => result.status > 0 });
  check(cart, { 'checkout cart request completed': (result) => result.status > 0 });
  check(order, { 'checkout order request completed': (result) => result.status > 0 });
}
