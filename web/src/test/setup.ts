import '@testing-library/jest-dom/vitest';

// jsdom ships no canvas implementation, and ECharts measures text through a 2d
// context even when it renders SVG. A minimal stub keeps chart tests quiet
// without pulling in the native `canvas` package.
const measuringContext = {
  measureText: (text: string) => ({ width: text.length * 6 }),
  fillText: () => {},
  save: () => {},
  restore: () => {},
  setTransform: () => {},
};

HTMLCanvasElement.prototype.getContext = (() =>
  measuringContext) as unknown as HTMLCanvasElement['getContext'];

// Tests never reach the network. Anything that wants to must say so by
// stubbing fetch itself, so an accidental real request fails loudly instead of
// making the suite depend on a running stack.
globalThis.fetch = () => Promise.reject(new Error('network access is disabled in tests'));
