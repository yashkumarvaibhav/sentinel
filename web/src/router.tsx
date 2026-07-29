import { createBrowserRouter } from 'react-router';

import { LoadingPage, NotFoundPage } from '@/components/RouteStates';
import { CommandShell } from '@/shell/CommandShell';

async function commandRoute() {
  const module = await import('@/App');
  return { Component: module.App };
}

async function incidentDetailRoute() {
  const module = await import('@/components/IncidentDetailPage');
  return { Component: module.IncidentDetailPage };
}

async function securityRoute() {
  const module = await import('@/components/SecurityPage');
  return { Component: module.SecurityPage };
}

/**
 * One browser router, created outside React state as the package requires.
 *
 * Both evidence-heavy screens are lazy boundaries. This keeps a direct proof
 * link from paying for the command center's chart code before it is needed and
 * gives Vite a real route-level split instead of one ever-growing entry chunk.
 */
export const router = createBrowserRouter([
  {
    Component: CommandShell,
    HydrateFallback: LoadingPage,
    children: [
      { index: true, lazy: commandRoute },
      { path: 'command', lazy: commandRoute },
      { path: 'security', lazy: securityRoute },
      { path: 'incidents/:incidentId', lazy: incidentDetailRoute },
      { path: '*', Component: NotFoundPage },
    ],
  },
]);
