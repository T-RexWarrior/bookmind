import { BookMindSidebar, BookMindFrame } from '@/components/bookmind/BookMindShell'
import AppShell from '@/components/layout/AppShell'
import { CapabilityAccessProvider } from '@/components/access/CapabilityAccessContext'
import CapabilityGate from '@/components/access/CapabilityGate'

export default function UtilityLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <CapabilityAccessProvider>
      <AppShell sidebar={<BookMindSidebar />}>
        <BookMindFrame>
          <CapabilityGate>{children}</CapabilityGate>
        </BookMindFrame>
      </AppShell>
    </CapabilityAccessProvider>
  )
}
