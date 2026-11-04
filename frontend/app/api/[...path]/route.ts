import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";

export const maxDuration = 15;

export const dynamic = "force-dynamic";

const SESSION_COOKIE = "smeops_session";
const SESSION_MAX_AGE_SECONDS = 30 * 60;

function isAllowedPath(path: string): boolean {
  return (
    path === "auth/login" ||
    path === "auth/register" ||
    path === "auth/me" ||
    path === "services" ||
    path === "business-hours" ||
    path === "appointments" ||
    /^appointments\/[0-9a-f-]+\/cancel$/i.test(path) ||
    path === "whatsapp/connection" ||
    path === "whatsapp/conversations" ||
    /^whatsapp\/conversations\/[0-9a-f-]+\/messages$/i.test(path) ||
    /^whatsapp\/conversations\/[0-9a-f-]+\/resolve$/i.test(path) ||
    path === "whatsapp/messages" ||
    path === "whatsapp/embedded-signup"
  );
}

function backendUrl(path: string, search: string): string {
  const base = process.env.SMEOPS_API_BASE_URL?.replace(/\/+$/, "");
  if (!base) {
    throw new Error("SMEOPS_API_BASE_URL is not configured");
  }
  return `${base}/${path}${search}`;
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path: segments } = await context.params;
  const path = segments.join("/");
  const method = request.method.toUpperCase();
  const jar = await cookies();

  if (path === "auth/logout" && method === "POST") {
    jar.delete(SESSION_COOKIE);
    return NextResponse.json({ authenticated: false });
  }

  if (method !== "GET" && method !== "HEAD") {
    const origin = request.headers.get("origin");
    if (origin && origin !== request.nextUrl.origin) {
      return NextResponse.json({ detail: "Cross-origin request rejected" }, { status: 403 });
    }
  }

  if (!isAllowedPath(path)) {
    return NextResponse.json({ detail: "Not found" }, { status: 404 });
  }

  let url: string;
  try {
    url = backendUrl(path, request.nextUrl.search);
  } catch {
    return NextResponse.json({ detail: "API service is not configured" }, { status: 503 });
  }

  const isAuthExchange = path === "auth/login" || path === "auth/register";
  const token = jar.get(SESSION_COOKIE)?.value;
  if (!isAuthExchange && !token) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const headers = new Headers({ Accept: "application/json" });
  if (token && !isAuthExchange) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  let body: string | undefined;
  if (method !== "GET" && method !== "HEAD") {
    headers.set("Content-Type", "application/json");
    body = await request.text();
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method,
      headers,
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
  } catch {
    return NextResponse.json({ detail: "API service is unavailable" }, { status: 502 });
  }

  const responseBody = await upstream.text();
  if (isAuthExchange && upstream.ok) {
    let authPayload: { access_token?: string };
    try {
      authPayload = JSON.parse(responseBody) as { access_token?: string };
    } catch {
      return NextResponse.json({ detail: "Invalid authentication response" }, { status: 502 });
    }
    if (!authPayload.access_token) {
      return NextResponse.json({ detail: "Authentication response omitted token" }, { status: 502 });
    }
    jar.set(SESSION_COOKIE, authPayload.access_token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "strict",
      path: "/",
      maxAge: SESSION_MAX_AGE_SECONDS,
    });
    return NextResponse.json({ authenticated: true }, { status: upstream.status });
  }

  const contentType = upstream.headers.get("content-type") ?? "application/json";
  return new Response(responseBody, {
    status: upstream.status,
    headers: {
      "Content-Type": contentType,
      "Cache-Control": "no-store",
    },
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
