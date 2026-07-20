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
