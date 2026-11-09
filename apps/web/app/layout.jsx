import 'maplibre-gl/dist/maplibre-gl.css';
import './globals.css';

export const metadata = {
  title: 'Qualimap — quality of life, cell by cell',
  description: 'Personal quality-of-life map of Switzerland on the A5 grid: weight noise, air, transport, taxes and access to services.',
};

export const viewport = { width: 'device-width', initialScale: 1 };

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
