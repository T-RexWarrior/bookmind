import type { Metadata } from 'next'
import './globals.css'
import './bookmind.css'
import { BookMindProvider } from '@/components/bookmind/BookMindProvider'
import ThemeScript from '@/components/ThemeScript'
import ToastViewport from '@/components/common/ToastViewport'
import MotionProvider from '@/components/common/MotionProvider'
import SettingsReturnTracker from '@/components/settings/SettingsReturnTracker'
import { WorkspaceNavigation } from '@/components/workspaces/WorkspaceNavigation'
import { AppShellProvider } from '@/context/AppShellContext'
import { I18nClientBridge } from '@/i18n/I18nClientBridge'

// Geist matches the public site (deeptutor.info) and stays crisp at the
// small UI sizes the composer/toolbars use, unlike the rounder Jakarta.
export const metadata: Metadata = {
  title: 'BookMind · 学迹',
  description: '围绕同一教材和知识点协作的阅读、练习与笔记助手',
  icons: {
    icon: [{ url: '/favicon.svg', type: 'image/svg+xml' }],
    apple: '/apple-touch-icon.png',
  },
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning data-scroll-behavior="smooth">
      <head>
        <ThemeScript />
      </head>
      <body
        className="font-sans bg-[var(--background)] text-[var(--foreground)]"
        suppressHydrationWarning
      >
        <AppShellProvider>
          <SettingsReturnTracker />
          <WorkspaceNavigation />
          <MotionProvider>
            <I18nClientBridge>
              <BookMindProvider>{children}</BookMindProvider>
            </I18nClientBridge>
          </MotionProvider>
          <ToastViewport />
        </AppShellProvider>
      </body>
    </html>
  )
}
