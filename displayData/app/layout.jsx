import './globals.css'

export const metadata = {
  title: 'Biolyt · data lake',
  description: 'Browse the eight S3 categories',
}

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
