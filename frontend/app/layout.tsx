import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Lead Scraper",
  description: "Pakistan local business lead scraper — implementation.md §13",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav className="top">
          <span className="brand">Lead Scraper</span>
          <Link href="/">New run</Link>
          <Link href="/runs">Runs</Link>
          <Link href="/results">Results</Link>
          <span className="spacer" />
          <Link href="/settings">Settings</Link>
        </nav>
        <main>{children}</main>
      </body>
    </html>
  );
}
