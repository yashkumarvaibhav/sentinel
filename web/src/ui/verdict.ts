import {
  CalendarCheck,
  CircleDashed,
  FileCode2,
  Layers,
  ShieldAlert,
  Wrench,
  type LucideIcon,
} from '@/ui/icons';

/**
 * A verdict's icon.
 *
 * The verdict is the loudest status on an incident card, so it is the last
 * place a shape should be missing. These are deliberately *descriptive* rather
 * than severity-graded — the icon says which of the five things the platform
 * concluded, and severity is carried separately. An attack and a fault are not
 * the same kind of bad, and a reader scanning a feed sorts them by shape long
 * before they read either label.
 *
 * `UNCLASSIFIED` gets the same dashed circle as an insufficient metric, because
 * it is the same statement: nothing has been concluded here yet.
 */
export const VERDICT_ICON: Record<string, LucideIcon> = {
  EXPECTED_EVENT: CalendarCheck,
  ATTACK: ShieldAlert,
  OPERATIONAL_FAULT: Wrench,
  CODE_CONFIG_FAULT: FileCode2,
  COMBINATION: Layers,
  UNCLASSIFIED: CircleDashed,
};

export function verdictIcon(verdict: string): LucideIcon {
  return VERDICT_ICON[verdict] ?? CircleDashed;
}
