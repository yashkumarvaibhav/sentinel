/**
 * The icon vocabulary, declared in one place.
 *
 * Every icon this product draws is named here and imported from here, never
 * from `lucide-react` directly. Two reasons, both practical:
 *
 * - **The set stays auditable.** The house rule is that status is never colour
 *   alone — colour + icon + text label. Checking that a status has an icon is a
 *   one-file read here; spread across thirty components it is a grep and a hope.
 * - **The bundle cost stays a function of this file.** These are the only icons
 *   that can reach the bundle, so the cost is bounded by a list somebody has to
 *   edit deliberately rather than by whatever a component reached for.
 *
 * Re-exports are statically analysable, so the bundler still drops everything
 * not named below (BUILD_STATE decision #140 records the measured cost).
 */
export {
  // Platform / connection state
  Activity,
  RadioTower,
  WifiOff,
  LoaderCircle,
  // Status — the four semantic outcomes, plus the honest "no answer" case
  CircleCheck,
  TriangleAlert,
  CircleX,
  Info,
  CircleDashed,
  // Honesty labels (UIUX_SPEC §1.3)
  ShieldCheck,
  FlaskConical,
  History,
  // Verdicts — the five conclusions the decision plane can reach
  CalendarCheck,
  ShieldAlert,
  Wrench,
  FileCode2,
  Layers,
  // Controls
  Sun,
  Moon,
  Monitor,
  Briefcase,
  Terminal,
} from 'lucide-react';

export type { LucideIcon } from 'lucide-react';
