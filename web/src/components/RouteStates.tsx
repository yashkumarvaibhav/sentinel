import { Link } from 'react-router';

export function LoadingPage() {
  return (
    <p className="text-muted text-sm" role="status">
      Loading screen…
    </p>
  );
}

export function NotFoundPage() {
  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-2xl font-semibold">Screen not found</h1>
      <p className="text-muted text-sm">This route is not part of the current Sentinel build.</p>
      <Link className="text-accent text-sm underline" to="/command">
        Open the command center
      </Link>
    </div>
  );
}
