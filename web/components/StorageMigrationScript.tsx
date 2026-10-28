import { STORAGE_MIGRATION_SCRIPT } from '@/lib/brand-migration'

/** Execute before theme selection and client provider hydration. */
export default function StorageMigrationScript() {
  return <script dangerouslySetInnerHTML={{ __html: STORAGE_MIGRATION_SCRIPT }} suppressHydrationWarning />
}
