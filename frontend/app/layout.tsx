import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SmeOps — Business operations",
  description: "Manage appointments, customers and WhatsApp conversations.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
