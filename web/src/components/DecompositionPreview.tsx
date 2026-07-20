import * as echarts from 'echarts/core';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { LineChart } from 'echarts/charts';
import { SVGRenderer } from 'echarts/renderers';
import { useEffect, useRef } from 'react';

echarts.use([LineChart, GridComponent, TooltipComponent, SVGRenderer]);

/** Illustrative shape only — no telemetry exists yet. Replaced by real frames in Phase 1. */
const SAMPLE = {
  base: [40, 41, 39, 42, 40, 41, 43, 42, 41, 40, 42, 41],
  event: [0, 0, 0, 12, 34, 51, 60, 58, 44, 22, 6, 0],
  residual: [0, 0, 0, 0, 0, 2, 3, 26, 31, 24, 4, 0],
};

const STACK = 'decomposition';

function seriesFor(name: string, data: number[], color: string) {
  return {
    name,
    type: 'line' as const,
    stack: STACK,
    areaStyle: { color, opacity: 0.85 },
    lineStyle: { width: 0 },
    symbol: 'none' as const,
    data,
  };
}

/**
 * A stacked area sketch of the one idea the product is built on: observed load
 * splits into what the baseline explains, what the event explains, and the
 * residual nothing explains.
 */
export function DecompositionPreview({ height = 200 }: { height?: number }) {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    // jsdom and freshly mounted nodes report zero size; give ECharts explicit
    // dimensions so it never has to guess.
    const chart = echarts.init(host, undefined, {
      renderer: 'svg',
      width: host.clientWidth || 640,
      height,
    });

    chart.setOption({
      animation: false,
      grid: { top: 8, right: 8, bottom: 20, left: 32 },
      xAxis: { type: 'category', data: SAMPLE.base.map((_, i) => `${i}`), axisLabel: { show: false } },
      yAxis: { type: 'value', splitLine: { lineStyle: { opacity: 0.12 } } },
      series: [
        seriesFor('explained base', SAMPLE.base, '#8aa0bf'),
        seriesFor('explained by event', SAMPLE.event, '#e0a63c'),
        seriesFor('unexplained residual', SAMPLE.residual, '#e2593f'),
      ],
    });

    const onResize = () => {
      chart.resize({ width: host.clientWidth || 640, height });
    };
    window.addEventListener('resize', onResize);

    return () => {
      window.removeEventListener('resize', onResize);
      chart.dispose();
    };
  }, [height]);

  return <div ref={hostRef} style={{ height }} aria-hidden="true" />;
}
