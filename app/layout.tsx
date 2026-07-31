import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL(
    process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000",
  ),
  title: "意向宝｜让客户声音照进产品规划",
  description:
    "从会议证据出发，形成技术、客户、产品平台与客户职能的证据化意向地图。",
  openGraph: {
    title: "意向宝｜让客户声音照进产品规划",
    description:
      "技术 × 客户 × 产品平台 × 客户职能的证据化意向地图。",
    type: "website",
    images: [{ url: "/og.png", width: 1672, height: 941 }],
  },
  twitter: {
    card: "summary_large_image",
    title: "意向宝｜让客户声音照进产品规划",
    description:
      "技术 × 客户 × 产品平台 × 客户职能的证据化意向地图。",
    images: ["/og.png"],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
