import { Inter, JetBrains_Mono } from "next/font/google";
import AppShell from "@/components/AppShell";
import "./globals.css";

// §8 type system: Inter 400/500/600 for UI text, JetBrains Mono for figures
// in tables/citations. Exposed as --font-sans / --font-mono, the variables
// Tailwind's font-sans / font-mono utilities resolve.
const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-sans",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata = {
  title: "Alpha Detective",
  description:
    "Financial document intelligence — grounded, citation-backed answers from your own documents.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en" className="h-full">
      <body
        className={`${inter.variable} ${jetbrainsMono.variable} min-h-full antialiased`}
      >
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
