import { render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';

import { SecurityPage } from '@/components/SecurityPage';
import { OperatorCredentialProvider } from '@/shell/OperatorCredential';
import { SnapshotStreamProvider } from '@/shell/SnapshotStream';
import { SECURITY_READY_FIXTURE } from '@/test/securityFixture';

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

function renderSecurity() {
  return render(
    <MemoryRouter initialEntries={['/security']}>
      <OperatorCredentialProvider>
        <SnapshotStreamProvider>
          <Routes>
            <Route path="/security" element={<SecurityPage />} />
          </Routes>
        </SnapshotStreamProvider>
      </OperatorCredentialProvider>
    </MemoryRouter>,
  );
}

it('renders decomposition, explicit cohort attribution, and honest insufficiency', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(SECURITY_READY_FIXTURE), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    ),
  );

  renderSecurity();

  await waitFor(() =>
    expect(screen.getByRole('heading', { level: 1, name: 'Security evidence' })).toBeVisible(),
  );
  expect(screen.getByRole('heading', { name: 'Residual decomposition' })).toBeVisible();
  expect(
    screen.getByRole('img', {
      name: /decomposition of 1 frames into explained base, explained event and unexplained residual/i,
    }),
  ).toBeVisible();
  expect(screen.getByText(/84 observed = 5 explained base \+ 60 explained event \+ 19/i)).toBeVisible();
  expect(screen.getByRole('heading', { name: 'Attack timeline' })).toBeVisible();
  expect(screen.getByText('public-client-group-17')).toBeVisible();
  expect(screen.getByRole('columnheader', { name: 'Machine timing' })).toBeVisible();
  expect(screen.getAllByText('Insufficient').length).toBeGreaterThan(0);
  expect(
    screen.getAllByText('No evidence window or numeric value exists.').length,
  ).toBeGreaterThan(0);

  const integrityHeadings = screen.getAllByRole('heading', {
    name: 'Protected cohort integrity',
  });
  const protectedCard = integrityHeadings.at(-1)!.closest('article');
  expect(protectedCard).not.toBeNull();
  expect(within(protectedCard!).getByText('Insufficient')).toBeVisible();
  expect(within(protectedCard!).queryByText(/^0(?:\.0+)?%?$/)).not.toBeInTheDocument();
  expect(
    screen.getByText(
      (_, element) =>
        element?.tagName === 'FOOTER' &&
        element.textContent?.includes('REAL telemetry provenance') === true,
    ),
  ).toBeVisible();
});

it('states that an empty current snapshot is not proof of safety', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            status: 'empty',
            snapshot: null,
            detail: 'No current incident has an evidence-backed security snapshot.',
          }),
          { status: 200 },
        ),
      ),
    ),
  );

  renderSecurity();

  await waitFor(() =>
    expect(screen.getByRole('heading', { name: 'No current security snapshot' })).toBeVisible(),
  );
  expect(screen.getByText(/not evidence that the system is safe or quiet/i)).toBeVisible();
});

it('keeps the same evidence in exec mode while hiding technical provenance rows', async () => {
  window.localStorage.setItem('sentinel.audience', 'exec');
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify(SECURITY_READY_FIXTURE), { status: 200 })),
    ),
  );

  renderSecurity();

  await waitFor(() =>
    expect(screen.getByRole('heading', { level: 1, name: 'Security evidence' })).toBeVisible(),
  );
  expect(screen.getByText('public-client-group-17')).toBeVisible();
  expect(screen.getByText('84%')).toBeVisible();
  expect(screen.queryByText(/Measured \/ baseline:/)).not.toBeInTheDocument();
  expect(screen.queryByText(/refs symptom-auth-ratio/)).not.toBeInTheDocument();
});

it('announces a degraded snapshot as unavailable rather than empty', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            status: 'degraded',
            snapshot: null,
            detail: 'The durable security reader is unavailable.',
          }),
          { status: 503 },
        ),
      ),
    ),
  );

  renderSecurity();

  await waitFor(() =>
    expect(screen.getByRole('alert')).toHaveTextContent('Security evidence unavailable'),
  );
  expect(screen.queryByRole('heading', { name: 'No current security snapshot' })).not.toBeInTheDocument();
});

it('fails closed behind the memory-only operator credential prompt', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(new Response(null, { status: 401 }))),
  );

  renderSecurity();

  await waitFor(() =>
    expect(screen.getByText('Protected security evidence')).toBeVisible(),
  );
  expect(screen.getByRole('button', { name: 'Unlock security evidence' })).toBeVisible();
  expect(screen.getByLabelText('Operator credential')).toHaveAttribute('type', 'password');
});
