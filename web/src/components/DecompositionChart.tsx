import { LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import * as echarts from 'echarts/core';
import { SVGRenderer } from 'echarts/renderers';
import { useEffect, useMemo, useRef } from 'react';

import { isCoherent } from '@/api/decomposition';
import type { DecompFrame } from '@/contracts/types';

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, SVGRenderer]);

const STACK = 'decomposition';

// Read off the domain tokens rather than hard-coded, so the chart and the
// legend beside it can never drift apart. These three do not change with the
// theme, on purpose: "this part of the surge is unexplained" must be the same
// red in a dark room and a lit one.
function token(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function band(name: string, data: number[], color: string) {
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

export interface DecompositionChartProps {
  frames: DecompFrame[];
  height?: number;
}

/**
 * The hero chart: observed load split into what the baseline explains, what the
 * event explains, and the residual nothing explains.
 *
 * The three bands are stacked and the observed line is drawn **on top of them
 * from its own field**, not from their sum. Drawing it from the sum would make
 * the chart agree with itself by construction — and the whole reason the API
 * sends the parts rather than a summary is so this one relationship stays
 * checkable rather than assumed.
 */
export function DecompositionChart({ frames, height = 260 }: DecompositionChartProps) {
  const hostRef = useRef<HTMLDivElement>(null);

  const series = useMemo(
    () => ({
      times: frames.map((frame) => frame.ts),
      base: frames.map((frame) => frame.explained_base),
      event: frames.map((frame) => frame.explained_event),
      residual: frames.map((frame) => frame.residual),
      observed: frames.map((frame) => frame.observed),
    }),
    [frames],
  );

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

    const muted = token('--muted', '#666666');

    chart.setOption({
      // Motion is opt-in here: a chart that animates on every poll is noise on
      // a wall display, and prefers-reduced-motion is honoured globally anyway.
      animation: false,
      grid: { top: 8, right: 8, bottom: 24, left: 44 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: series.times,
        axisLabel: {
          color: muted,
          formatter: (value: string) => new Date(value).toISOString().slice(11, 19),
        },
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: muted },
        splitLine: { lineStyle: { opacity: 0.12 } },
      },
      series: [
        band('explained base', series.base, token('--color-base', '#8aa0bf')),
        band('explained by event', series.event, token('--color-event', '#e0a63c')),
        band('unexplained residual', series.residual, token('--color-residual', '#e2593f')),
        {
          name: 'observed',
          type: 'line' as const,
          data: series.observed,
          symbol: 'none' as const,
          lineStyle: { width: 1.5, color: muted, type: 'dashed' as const },
          z: 5,
        },
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
  }, [series, height]);

  const incoherent = frames.filter((frame) => !isCoherent(frame)).length;

  return (
    <div className="flex flex-col gap-2">
      <div
        ref={hostRef}
        style={{ height }}
        role="img"
        aria-label={`Decomposition of ${frames.length} frames into explained base, explained event and unexplained residual`}
      />
      {incoherent > 0 && (
        <p className="text-bad text-xs" role="alert">
          {incoherent} frame{incoherent === 1 ? '' : 's'} did not satisfy observed = base + event +
          residual and {incoherent === 1 ? 'is' : 'are'} drawn as received. This is an upstream
          defect, not a rounding artefact.
        </p>
      )}
    </div>
  );
}
