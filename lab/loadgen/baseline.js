import http from 'k6/http';
import { check, group } from 'k6';

// Baseline browse traffic on top of what the demo's own load generator already
// produces. Deliberately modest: this is a shared host, and the point is a
// measurable, reproducible shift in request rate — not a stress test.
//
// The target is fixed to the in-cluster service. There is no URL parameter,
// because a load generator that can be pointed anywhere is one misconfiguration
// away from being aimed at something that is not ours.
const TARGET = 'http://frontend-proxy:8080';

export const options = {
  scenarios: {
    browse: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE || 5), // requests per second — capped below
      timeUnit: '1s',
      duration: __ENV.DURATION || '2m',
      preAllocatedVUs: 5,
      maxVUs: 20,
    },
  },
  thresholds: {
    // A baseline run that is already failing tells us nothing about the change
    // we are trying to measure.
    http_req_failed: ['rate<0.10'],
  },
};

export default function () {
  group('browse', () => {
    const home = http.get(`${TARGET}/`);
    check(home, { 'home 200': (r) => r.status === 200 });

    const products = http.get(`${TARGET}/api/products`);
    check(products, { 'products 200': (r) => r.status === 200 });
  });
}
