import http from 'k6/http';
import { check, randomSeed } from 'k6';

// The endpoint is intentionally not configurable. Scenario inputs can vary
// bounded rates and timing, but cannot redirect this runner outside our mesh.
const TARGET = 'http://frontend-proxy:8080';
const schedule = JSON.parse(__ENV.SENTINEL_SCHEDULE);
const runId = __ENV.SENTINEL_RUN_ID;

if (schedule.target !== 'astronomy-shop/frontend-proxy') {
  throw new Error(`unsupported contained target: ${schedule.target}`);
}

const scenarios = {};
for (const phase of schedule.phases) {
  if (phase.rate_rps < 1 || phase.rate_rps > 50) {
    throw new Error(`phase rate outside 1..50 rps: ${phase.rate_rps}`);
  }
  scenarios[phase.name] = {
    executor: 'constant-arrival-rate',
    rate: phase.rate_rps,
    timeUnit: '1s',
    duration: `${phase.duration_seconds}s`,
    startTime: `${phase.start_offset_seconds}s`,
    preAllocatedVUs: Math.min(Math.max(phase.rate_rps, 2), 20),
    maxVUs: 50,
    gracefulStop: '0s',
  };
}

export const options = {
  scenarios,
  thresholds: {
    checks: ['rate>0.99'],
    dropped_iterations: ['count==0'],
  },
};

const paths = ['/', '/api/products'];
randomSeed(schedule.request_mix_seed);

export function setup() {
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
  const path = paths[Math.floor(Math.random() * paths.length)];
  const response = http.get(`${TARGET}${path}`, {
    headers: { 'User-Agent': `sentinel-score/${runId}` },
  });
  check(response, { 'testbed response below 500': (result) => result.status < 500 });
}
