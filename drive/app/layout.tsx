import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Drive",
  description: "Our own drive — captured UI, served unchanged",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-white text-neutral-900 antialiased">
        {children}
      </body>
    </html>
  );
}