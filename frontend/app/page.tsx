"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

type User = { id: string; tenant_id: string; email: string; role: string };
type Service = {
  id: string;
  name: string;
  description: string | null;
  duration_minutes: number;
  price_minor: number;
  currency: string;
  is_active: boolean;
};
type Appointment = {
  id: string;
  service_id: string;
  customer_id: string;
  customer_name: string;
  customer_phone: string;
  starts_at: string;
  ends_at: string;
  status: string;
};
type BusinessHour = {
  weekday: number;
  is_closed: boolean;
  opens_at: string | null;
  closes_at: string | null;
};
type Conversation = {
  id: string;
  customer_phone: string;
  status: "active" | "human_required" | "closed";
  last_inbound_at: string;
  is_opted_out: boolean;
  opted_out_at: string | null;
};
type ConversationMessage = {
  id: string;
  direction: "inbound" | "outbound";
  message_type: string;
  body: string | null;
  delivery_status: string;
  occurred_at: string;
};
type Connection = {
  tenant_id: string;
  phone_number_id: string;
  business_account_id: string;
  status: "pending" | "active" | "disabled";
  display_phone_number?: string | null;
  verified_name?: string | null;
  token_expires_at?: string | null;
  onboarding_error_code?: string | null;
} | null;

type Tab = "overview" | "appointments" | "inbox" | "services" | "hours" | "whatsapp";

type FacebookLoginResponse = {
  authResponse?: { code?: string };
  status?: string;
};
type EmbeddedSignupMessage = {
  type?: string;
  event?: string;
  data?: { waba_id?: string; phone_number_id?: string; error_message?: string };
};
declare global {
  interface Window {
    FB?: {
      init: (options: { appId: string; cookie: boolean; xfbml: boolean; version: string }) => void;
      login: (
        callback: (response: FacebookLoginResponse) => void,
        options: Record<string, unknown>,
      ) => void;
    };
  }
}

const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof payload.detail === "string" ? payload.detail : `Request failed (${response.status})`,
    );
  }
  return payload as T;
}

function money(minor: number): string {
  return new Intl.NumberFormat("en-NG", {
    style: "currency",
    currency: "NGN",
    maximumFractionDigits: 2,
  }).format(minor / 100);
}

function dateTime(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Africa/Lagos",
  }).format(new Date(value));
}

function shortDate(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "Africa/Lagos",
  }).format(new Date(value));
}

export default function HomePage() {
  const [user, setUser] = useState<User | null>(null);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [authForm, setAuthForm] = useState({
    business_name: "",
    tenant_slug: "",
    email: "",
    password: "",
    business_timezone: "Africa/Lagos",
  });
  const [services, setServices] = useState<Service[]>([]);
  const [appointments, setAppointments] = useState<Appointment[]>([]);
  const [hoursConfigured, setHoursConfigured] = useState(false);
  const [hours, setHours] = useState<BusinessHour[]>(
    weekdays.map((_, weekday) => ({
      weekday,
      is_closed: weekday >= 5,
      opens_at: weekday >= 5 ? null : "09:00:00",
      closes_at: weekday >= 5 ? null : "17:00:00",
    })),
  );
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [connection, setConnection] = useState<Connection>(null);
  const [signupPin, setSignupPin] = useState("");
  const signupPinRef = useRef("");
  const signupPartsRef = useRef<{
    code?: string;
    waba_id?: string;
    phone_number_id?: string;
  }>({});
  const signupSubmittingRef = useRef(false);
  const [metaSdkReady, setMetaSdkReady] = useState(false);
  const [signupStatus, setSignupStatus] = useState("");
  const [activeConversation, setActiveConversation] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [serviceForm, setServiceForm] = useState({
    name: "",
    description: "",
    duration_minutes: "60",
    price_naira: "",
  });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const completeSignup = useCallback(async () => {
    const signup = signupPartsRef.current;
    if (
      !signup.code ||
      !signup.waba_id ||
      !signup.phone_number_id ||
      signupSubmittingRef.current
    ) {
      return;
    }
    if (!/^\d{6}$/.test(signupPinRef.current)) {
      setError("Enter a six-digit WhatsApp two-step verification PIN before connecting.");
      return;
    }
    signupSubmittingRef.current = true;
    setBusy(true);
    setError("");
    setSignupStatus("Completing secure WhatsApp connection…");
    try {
      const result = await api<Connection>("whatsapp/embedded-signup", {
        method: "POST",
        body: JSON.stringify({
          code: signup.code,
          waba_id: signup.waba_id,
          phone_number_id: signup.phone_number_id,
          two_step_pin: signupPinRef.current,
        }),
      });
      setConnection(result);
      setSignupStatus("WhatsApp is connected and ready for inbound messages.");
      setNotice("WhatsApp Business account connected.");
    } catch (reason) {
      const pending = await api<Connection>("whatsapp/connection").catch(() => null);
      setConnection(pending);
      setError(reason instanceof Error ? reason.message : "WhatsApp onboarding failed");
      setSignupStatus(
        pending?.status === "pending"
          ? "Meta authorized the number. Complete the remaining registration step with Retry connection."
          : "Connection was not completed. Restart Embedded Signup and try again.",
      );
    } finally {
      signupSubmittingRef.current = false;
      signupPartsRef.current = {};
      setBusy(false);
    }
  }, []);

  function launchEmbeddedSignup() {
    const appId = process.env.NEXT_PUBLIC_META_APP_ID;
    const configId = process.env.NEXT_PUBLIC_META_EMBEDDED_SIGNUP_CONFIG_ID;
    const graphVersion = process.env.NEXT_PUBLIC_META_GRAPH_API_VERSION;
    if (!appId || !configId || !graphVersion || !window.FB || !metaSdkReady) {
      setError("Meta Embedded Signup is not configured. Check the frontend Meta environment variables.");
      return;
    }
    if (!/^\d{6}$/.test(signupPin)) {
      setError("Choose a six-digit WhatsApp two-step verification PIN first.");
      return;
    }
    signupPartsRef.current = {};
    setError("");
    setSignupStatus("Waiting for you to complete Meta's secure signup window…");
    const solutionId = process.env.NEXT_PUBLIC_META_SOLUTION_ID;
    const extras = solutionId ? { setup: { solutionID: solutionId } } : {};
    window.FB.login(
      (response) => {
        const code = response.authResponse?.code;
        if (!code) {
          setSignupStatus("Meta signup did not return an authorization code. Start again.");
          return;
        }
        signupPartsRef.current = { ...signupPartsRef.current, code };
        void completeSignup();
      },
      {
        config_id: configId,
        auth_type: "rerequest",
        response_type: "code",
        override_default_response_type: true,
        extras,
      },
    );
  }

  async function disableWhatsAppConnection() {
    if (!window.confirm("Disable SmeOps messaging for this WhatsApp number?")) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<Connection>("whatsapp/connection/disconnect", {
        method: "POST",
        body: "{}",
      });
      setConnection(result);
      setNotice("SmeOps messaging has been disabled for this number.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not disable WhatsApp connection");
    } finally {
      setBusy(false);
    }
  }

  async function retryWhatsAppConnection() {
    setBusy(true);
    setError("");
    try {
      const result = await api<Connection>("whatsapp/connection/complete", {
        method: "POST",
        body: "{}",
      });
      setConnection(result);
      setSignupStatus("WhatsApp is connected and ready for inbound messages.");
      setNotice("WhatsApp connection completed.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connection retry failed");
      const pending = await api<Connection>("whatsapp/connection").catch(() => null);
      setConnection(pending);
    } finally {
      setBusy(false);
    }
  }

  const loadDashboard = useCallback(async () => {
    setError("");
    const [serviceData, appointmentData, hourData, conversationData, connectionData] =
      await Promise.all([
        api<Service[]>("services"),
        api<Appointment[]>("appointments?limit=100"),
        api<BusinessHour[]>("business-hours"),
        api<Conversation[]>("whatsapp/conversations?limit=100"),
        api<Connection>("whatsapp/connection"),
      ]);
    setServices(serviceData);
    setAppointments(appointmentData);
    setHoursConfigured(hourData.length === 7);
    setHours(
      hourData.length === 7
        ? hourData
        : weekdays.map((_, weekday) => ({
            weekday,
            is_closed: weekday >= 5,
            opens_at: weekday >= 5 ? null : "09:00:00",
            closes_at: weekday >= 5 ? null : "17:00:00",
          })),
    );
    setConversations(conversationData);
    setConnection(connectionData);
    if (conversationData.length > 0) {
      const selected =
        activeConversation && conversationData.some((item) => item.id === activeConversation)
          ? activeConversation
          : conversationData[0].id;
      setActiveConversation(selected);
      const thread = await api<ConversationMessage[]>(
        `whatsapp/conversations/${selected}/messages?limit=100`,
      );
      setMessages(thread);
    } else {
      setActiveConversation(null);
      setMessages([]);
    }
  }, [activeConversation]);

  useEffect(() => {
    const appId = process.env.NEXT_PUBLIC_META_APP_ID;
    const graphVersion = process.env.NEXT_PUBLIC_META_GRAPH_API_VERSION;
    if (!appId || !graphVersion) return;

    const initializeSdk = () => {
      if (!window.FB) return;
      window.FB.init({
        appId,
        cookie: true,
        xfbml: false,
        version: graphVersion,
      });
      setMetaSdkReady(true);
    };

    if (window.FB) {
      initializeSdk();
      return;
    }

    const existing = document.getElementById("facebook-jssdk") as HTMLScriptElement | null;
    if (existing) {
      existing.addEventListener("load", initializeSdk, { once: true });
      return () => existing.removeEventListener("load", initializeSdk);
    }
    const script = document.createElement("script");
    script.id = "facebook-jssdk";
    script.src = "https://connect.facebook.net/en_US/sdk.js";
    script.async = true;
    script.defer = true;
    script.onload = initializeSdk;
    script.onerror = () => setError("Meta JavaScript SDK could not be loaded.");
    document.body.appendChild(script);
    return () => {
      script.onload = null;
    };
  }, []);

  useEffect(() => {
    const handleMetaMessage = (event: MessageEvent) => {
      let hostname = "";
      try {
        hostname = new URL(event.origin).hostname;
      } catch {
        return;
      }
      if (!(hostname === "facebook.com" || hostname.endsWith(".facebook.com"))) return;
      if (typeof event.data !== "string") return;

      let payload: EmbeddedSignupMessage;
      try {
        payload = JSON.parse(event.data) as EmbeddedSignupMessage;
      } catch {
        return;
      }
      if (payload.type !== "WA_EMBEDDED_SIGNUP") return;

      if (payload.event === "FINISH") {
        const wabaId = payload.data?.waba_id;
        const phoneNumberId = payload.data?.phone_number_id;
        if (!wabaId || !phoneNumberId) {
          setError("Meta completed signup without returning the WABA and phone number IDs.");
          return;
        }
        signupPartsRef.current = {
          ...signupPartsRef.current,
          waba_id: wabaId,
          phone_number_id: phoneNumberId,
        };
        void completeSignup();
      } else if (payload.event === "CANCEL") {
        setSignupStatus("Meta signup was cancelled. You can start again when ready.");
      } else if (payload.event === "ERROR") {
        setError(payload.data?.error_message || "Meta reported an Embedded Signup error.");
      } else if (payload.event === "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING") {
        setError("WhatsApp Business App coexistence onboarding is not enabled for this pilot.");
      }
    };

    window.addEventListener("message", handleMetaMessage);
    return () => window.removeEventListener("message", handleMetaMessage);
  }, [completeSignup]);

  useEffect(() => {
    let mounted = true;
    api<User>("auth/me")
      .then((identity) => {
        if (mounted) setUser(identity);
      })
      .catch(() => {
        if (mounted) setUser(null);
      })
      .finally(() => {
        if (mounted) setLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    if (!user) return;
    let mounted = true;
    loadDashboard()
      .catch((reason: unknown) => {
        if (mounted) setError(reason instanceof Error ? reason.message : "Unable to load dashboard");
      });
    return () => {
      mounted = false;
    };
  }, [user, loadDashboard]);

  const upcomingAppointments = useMemo(
    () =>
      appointments
        .filter((item) => item.status === "confirmed" && new Date(item.starts_at) >= new Date())
        .sort((a, b) => a.starts_at.localeCompare(b.starts_at)),
    [appointments],
  );

  const openConversations = useMemo(
    () => conversations.filter((item) => item.status === "human_required").length,
    [conversations],
  );

  async function submitAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api(authMode === "login" ? "auth/login" : "auth/register", {
        method: "POST",
        body: JSON.stringify(
          authMode === "login"
            ? {
                tenant_slug: authForm.tenant_slug,
                email: authForm.email,
                password: authForm.password,
              }
            : authForm,
        ),
      });
      const identity = await api<User>("auth/me");
      setUser(identity);
      setNotice(authMode === "register" ? "Business workspace created." : "Signed in.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Authentication failed");
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    await api("auth/logout", { method: "POST" }).catch(() => undefined);
    setUser(null);
    setServices([]);
    setAppointments([]);
    setConversations([]);
    setMessages([]);
    setConnection(null);
  }

  async function refresh() {
    setBusy(true);
    try {
      await loadDashboard();
      setNotice("Workspace data refreshed.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Refresh failed");
    } finally {
      setBusy(false);
    }
  }

  async function createService(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api<Service>("services", {
        method: "POST",
        body: JSON.stringify({
          name: serviceForm.name,
          description: serviceForm.description || null,
          duration_minutes: Number(serviceForm.duration_minutes),
          price_minor: Math.round(Number(serviceForm.price_naira) * 100),
        }),
      });
      setServiceForm({ name: "", description: "", duration_minutes: "60", price_naira: "" });
      await loadDashboard();
      setNotice("Service added.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not add service");
    } finally {
      setBusy(false);
    }
  }

  async function saveHours(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api<BusinessHour[]>("business-hours", {
        method: "PUT",
        body: JSON.stringify({ days: hours }),
      });
      setNotice("Opening hours saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save opening hours");
    } finally {
      setBusy(false);
    }
  }

  async function cancelAppointment(id: string) {
    if (!window.confirm("Cancel this appointment?")) return;
    setBusy(true);
    try {
      await api(`appointments/${id}/cancel`, { method: "POST", body: "{}" });
      await loadDashboard();
      setNotice("Appointment cancelled.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not cancel appointment");
    } finally {
      setBusy(false);
    }
  }

  async function selectConversation(id: string) {
    setActiveConversation(id);
    setError("");
    try {
      const thread = await api<ConversationMessage[]>(
        `whatsapp/conversations/${id}/messages?limit=100`,
      );
      setMessages(thread);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load conversation");
    }
  }

  async function resolveConversation(id: string) {
    setBusy(true);
    try {
      await api(`whatsapp/conversations/${id}/resolve`, { method: "POST", body: "{}" });
      await loadDashboard();
      setNotice("Conversation returned to the active queue.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not resolve conversation");
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return <main className="loading-screen"><span className="brand-mark">S</span><p>Loading SmeOps…</p></main>;
  }

  if (!user) {
    return (
      <main className="auth-shell">
        <section className="auth-aside">
          <div className="brand-lockup"><span className="brand-mark">S</span><span>SmeOps</span></div>
          <div className="auth-copy">
            <span className="eyebrow">BUSINESS OPERATIONS, SIMPLIFIED</span>
            <h1>Your business, <em>in rhythm.</em></h1>
            <p>Manage appointments, customer conversations and day-to-day operations from one workspace.</p>
          </div>
          <div className="auth-footnote">Built for service businesses in Nigeria.</div>
        </section>
        <section className="auth-panel">
          <form className="auth-card" onSubmit={submitAuth}>
            <span className="eyebrow">{authMode === "login" ? "WELCOME BACK" : "GET STARTED"}</span>
            <h2>{authMode === "login" ? "Sign in to your workspace" : "Create your business workspace"}</h2>
            <p className="muted">{authMode === "login" ? "Enter your business account details." : "Set up your salon or service business."}</p>
            {authMode === "register" && (
              <label>Business name<input required minLength={2} value={authForm.business_name} onChange={(e) => setAuthForm({ ...authForm, business_name: e.target.value })} placeholder="e.g. Studio Ada" /></label>
            )}
            <label>Business ID<input required value={authForm.tenant_slug} onChange={(e) => setAuthForm({ ...authForm, tenant_slug: e.target.value.toLowerCase().replace(/\s+/g, "-") })} placeholder="studio-ada" /></label>
            <label>Email address<input required type="email" value={authForm.email} onChange={(e) => setAuthForm({ ...authForm, email: e.target.value })} placeholder="you@example.com" /></label>
            <label>Password<input required type="password" minLength={authMode === "register" ? 12 : 1} value={authForm.password} onChange={(e) => setAuthForm({ ...authForm, password: e.target.value })} placeholder="Enter your password" /></label>
            {authMode === "register" && (
              <label>Business timezone<select value={authForm.business_timezone} onChange={(e) => setAuthForm({ ...authForm, business_timezone: e.target.value })}><option value="Africa/Lagos">Africa/Lagos (WAT)</option><option value="Africa/Accra">Africa/Accra (GMT)</option></select></label>
            )}
            {error && <p className="form-error">{error}</p>}
            <button className="button button-primary full-width" disabled={busy}>{busy ? "Please wait…" : authMode === "login" ? "Sign in" : "Create workspace"}</button>
            <p className="auth-switch">
              {authMode === "login" ? "New to SmeOps?" : "Already have a workspace?"}{" "}
              <button type="button" className="text-button" onClick={() => { setAuthMode(authMode === "login" ? "register" : "login"); setError(""); }}>
                {authMode === "login" ? "Create an account" : "Sign in"}
              </button>
            </p>
          </form>
        </section>
      </main>
    );
  }

  const nav: { id: Tab; label: string; icon: string }[] = [
    { id: "overview", label: "Overview", icon: "◫" },
    { id: "appointments", label: "Appointments", icon: "▦" },
    { id: "inbox", label: "WhatsApp inbox", icon: "◌" },
    { id: "services", label: "Services", icon: "✳" },
    { id: "hours", label: "Opening hours", icon: "◷" },
    { id: "whatsapp", label: "WhatsApp setup", icon: "↗" },
  ];

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup"><span className="brand-mark">S</span><span>SmeOps</span></div>
        <div className="workspace-label">WORKSPACE</div>
        <div className="workspace-card"><span className="workspace-avatar">B</span><span><strong>Business workspace</strong><small>{user.role} account</small></span><span className="chevron">⌄</span></div>
        <nav className="side-nav" aria-label="Main navigation">
          {nav.map((item) => (
            <button key={item.id} className={tab === item.id ? "nav-item active" : "nav-item"} onClick={() => setTab(item.id)}>
              <span className="nav-icon">{item.icon}</span>{item.label}
              {item.id === "inbox" && openConversations > 0 && <span className="nav-count">{openConversations}</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="connection-mini"><span className={connection?.status === "active" ? "status-dot online" : "status-dot"} /><span>{connection?.status === "active" ? "WhatsApp connected" : "WhatsApp not connected"}</span></div>
          <div className="profile-row"><span className="profile-avatar">{user.email.slice(0, 1).toUpperCase()}</span><span className="profile-email">{user.email}</span><button className="icon-button" onClick={logout} aria-label="Sign out" title="Sign out">↪</button></div>
        </div>
      </aside>

      <section className="main-area">
        <header className="topbar">
          <div><span className="breadcrumb">Workspace</span><span className="breadcrumb-separator">/</span><strong>{nav.find((item) => item.id === tab)?.label}</strong></div>
          <div className="topbar-actions"><span className="timezone-chip">WAT · Africa/Lagos</span><button className="button button-quiet" onClick={refresh} disabled={busy}>↻ Refresh</button><button className="profile-avatar top-avatar" onClick={logout} title="Sign out">{user.email.slice(0, 1).toUpperCase()}</button></div>
        </header>

        <div className="page-content">
          {error && <div className="alert alert-error"><span>{error}</span><button onClick={() => setError("")}>×</button></div>}
          {notice && <div className="alert alert-success"><span>{notice}</span><button onClick={() => setNotice("")}>×</button></div>}

          {tab === "overview" && (
            <>
              <div className="page-heading"><div><span className="eyebrow">YOUR BUSINESS AT A GLANCE</span><h1>Good day</h1><p>Here is what is happening in your workspace.</p></div><button className="button button-primary" onClick={() => setTab("appointments")}>View appointments <span>→</span></button></div>
              <div className="stats-grid">
                <article className="stat-card"><span className="stat-label">Upcoming appointments</span><strong>{upcomingAppointments.length}</strong><small className="stat-foot">Confirmed bookings ahead</small><span className="stat-symbol">▦</span></article>
                <article className="stat-card"><span className="stat-label">Active services</span><strong>{services.length}</strong><small className="stat-foot">Available to customers</small><span className="stat-symbol">✳</span></article>
                <article className="stat-card"><span className="stat-label">Conversations</span><strong>{conversations.length}</strong><small className="stat-foot">WhatsApp customer threads</small><span className="stat-symbol">◌</span></article>
                <article className="stat-card"><span className="stat-label">Needs attention</span><strong>{openConversations}</strong><small className="stat-foot">Waiting for staff follow-up</small><span className="stat-symbol">!</span></article>
              </div>
              <div className="content-grid">
                <section className="panel"><div className="panel-heading"><div><h2>Next appointments</h2><p>Your upcoming confirmed bookings</p></div><button className="text-button" onClick={() => setTab("appointments")}>See all →</button></div>
                  {upcomingAppointments.slice(0, 5).length === 0 ? <EmptyState title="No upcoming appointments" body="Your confirmed bookings will appear here." /> : (
                    <div className="appointment-list">{upcomingAppointments.slice(0, 5).map((item) => <AppointmentRow key={item.id} item={item} onCancel={cancelAppointment} />)}</div>
                  )}
                </section>
                <section className="panel setup-panel"><div className="panel-heading"><div><h2>Workspace setup</h2><p>Complete these steps to go live</p></div></div>
                  <SetupStep done={services.length > 0} title="Add your services" detail="Set service names, duration and prices" onClick={() => setTab("services")} />
                  <SetupStep done={hoursConfigured} title="Set opening hours" detail="Tell customers when you are available" onClick={() => setTab("hours")} />
                  <SetupStep done={connection?.status === "active"} title="Connect WhatsApp" detail="Link your business number" onClick={() => setTab("whatsapp")} />
                </section>
              </div>
            </>
          )}

          {tab === "appointments" && (
            <>
              <div className="page-heading"><div><span className="eyebrow">SCHEDULE</span><h1>Appointments</h1><p>View and manage customer bookings.</p></div><span className="count-chip">{appointments.length} total</span></div>
              <section className="panel">
                {appointments.length === 0 ? <EmptyState title="No appointments yet" body="Bookings created through WhatsApp will appear here." /> : (
                  <div className="table-wrap"><table><thead><tr><th>Customer</th><th>Service</th><th>Date & time</th><th>Contact</th><th>Status</th><th /></tr></thead><tbody>
                    {appointments.map((item) => {
                      const service = services.find((entry) => entry.id === item.service_id);
                      return <tr key={item.id}><td><strong>{item.customer_name}</strong><small className="cell-sub">{item.id.slice(0, 8).toUpperCase()}</small></td><td>{service?.name ?? "Service"}</td><td>{dateTime(item.starts_at)}</td><td>{item.customer_phone}</td><td><span className={item.status === "confirmed" ? "pill pill-green" : "pill"}>{item.status}</span></td><td>{item.status === "confirmed" && <button className="button button-danger-quiet" onClick={() => cancelAppointment(item.id)} disabled={busy}>Cancel</button>}</td></tr>;
                    })}
                  </tbody></table></div>
                )}
              </section>
            </>
          )}

          {tab === "inbox" && (
            <>
              <div className="page-heading"><div><span className="eyebrow">CUSTOMER SUPPORT</span><h1>WhatsApp inbox</h1><p>Customer conversations and handoffs.</p></div><span className="count-chip">{conversations.length} conversations</span></div>
              <div className="inbox-layout">
                <section className="panel inbox-list">
                  {conversations.length === 0 ? <EmptyState title="Inbox is empty" body="New WhatsApp customer messages will appear here." /> : conversations.map((item) => (
                    <button key={item.id} className={activeConversation === item.id ? "conversation-item selected" : "conversation-item"} onClick={() => selectConversation(item.id)}>
                      <span className="conversation-avatar">{item.customer_phone.slice(-2)}</span><span className="conversation-copy"><strong>{item.customer_phone}</strong><small>{shortDate(item.last_inbound_at)}</small></span>
                      {item.status === "human_required" && <span className="pill pill-amber">Staff</span>}
                      {item.is_opted_out && <span className="pill">Opted out</span>}
                    </button>
                  ))}
                </section>
                <section className="panel thread-panel">
                  {activeConversation ? (
                    <>
                      <div className="thread-header"><div><strong>{conversations.find((item) => item.id === activeConversation)?.customer_phone}</strong><small>WhatsApp conversation</small></div>
                        {conversations.find((item) => item.id === activeConversation)?.status === "human_required" && <button className="button button-primary" onClick={() => resolveConversation(activeConversation)} disabled={busy}>Resolve handoff</button>}
                      </div>
                      <div className="message-list">
                        {[...messages].reverse().map((message) => <div key={message.id} className={message.direction === "outbound" ? "message-row outgoing" : "message-row incoming"}><div className="message-bubble"><p>{message.body ?? `[${message.message_type} message]`}</p><small>{dateTime(message.occurred_at)} · {message.direction === "outbound" ? message.delivery_status : "received"}</small></div></div>)}
                        {messages.length === 0 && <EmptyState title="No messages" body="This conversation has no message history." />}
                      </div>
                    </>
                  ) : <EmptyState title="Select a conversation" body="Choose a customer thread to read the message history." />}
                </section>
              </div>
            </>
          )}

          {tab === "services" && (
            <>
              <div className="page-heading"><div><span className="eyebrow">YOUR CATALOGUE</span><h1>Services</h1><p>Manage the services customers can book.</p></div></div>
              <div className="content-grid service-grid">
                <section className="panel"><div className="panel-heading"><div><h2>Active services</h2><p>{services.length} services available</p></div></div>
                  {services.length === 0 ? <EmptyState title="No services configured" body="Add your first service to enable bookings." /> : <div className="service-list">{services.map((service) => <article className="service-card" key={service.id}><div className="service-icon">✳</div><div className="service-details"><strong>{service.name}</strong><p>{service.description || "No description"}</p><small>{service.duration_minutes} minutes</small></div><strong className="service-price">{money(service.price_minor)}</strong></article>)}</div>}
                </section>
                <section className="panel"><div className="panel-heading"><div><h2>Add a service</h2><p>Prices are entered in naira.</p></div></div>
                  <form className="stack-form" onSubmit={createService}>
                    <label>Service name<input required minLength={2} maxLength={120} value={serviceForm.name} onChange={(e) => setServiceForm({ ...serviceForm, name: e.target.value })} placeholder="e.g. Haircut" /></label>
                    <label>Description <span className="optional">Optional</span><textarea maxLength={1000} rows={3} value={serviceForm.description} onChange={(e) => setServiceForm({ ...serviceForm, description: e.target.value })} placeholder="Briefly describe the service" /></label>
                    <div className="form-row"><label>Duration (minutes)<input type="number" min="5" max="480" step="5" required value={serviceForm.duration_minutes} onChange={(e) => setServiceForm({ ...serviceForm, duration_minutes: e.target.value })} /></label><label>Price (₦)<input type="number" min="0" step="0.01" required value={serviceForm.price_naira} onChange={(e) => setServiceForm({ ...serviceForm, price_naira: e.target.value })} placeholder="15000" /></label></div>
                    <button className="button button-primary full-width" disabled={busy}>Add service</button>
                  </form>
                </section>
              </div>
            </>
          )}

          {tab === "hours" && (
            <>
              <div className="page-heading"><div><span className="eyebrow">AVAILABILITY</span><h1>Opening hours</h1><p>Set the hours used by the WhatsApp booking assistant.</p></div></div>
              <section className="panel hours-panel"><form onSubmit={saveHours}>
                {hours.map((day) => <div className="hours-row" key={day.weekday}><strong>{weekdays[day.weekday]}</strong><label className="toggle-label"><input type="checkbox" checked={!day.is_closed} onChange={(e) => setHours((current) => current.map((item) => item.weekday === day.weekday ? { ...item, is_closed: !e.target.checked, opens_at: e.target.checked ? item.opens_at || "09:00:00" : null, closes_at: e.target.checked ? item.closes_at || "17:00:00" : null } : item))} /><span>Open</span></label>
                  {day.is_closed ? <span className="closed-label">Closed</span> : <div className="time-inputs"><input type="time" required value={(day.opens_at ?? "09:00:00").slice(0, 5)} onChange={(e) => setHours((current) => current.map((item) => item.weekday === day.weekday ? { ...item, opens_at: `${e.target.value}:00` } : item))} /><span>to</span><input type="time" required value={(day.closes_at ?? "17:00:00").slice(0, 5)} onChange={(e) => setHours((current) => current.map((item) => item.weekday === day.weekday ? { ...item, closes_at: `${e.target.value}:00` } : item))} /></div>}
                </div>)}
                <div className="hours-footer"><span className="muted">Times are interpreted in Africa/Lagos unless your workspace timezone is changed.</span><button className="button button-primary" disabled={busy}>Save opening hours</button></div>
              </form></section>
            </>
          )}

          {tab === "whatsapp" && (
            <>
              <div className="page-heading"><div><span className="eyebrow">CHANNEL CONNECTION</span><h1>WhatsApp setup</h1><p>Connect the number customers will use to contact your business.</p></div></div>
              <section className="panel whatsapp-panel">
                <div className="whatsapp-illustration">◌</div>
                <div className="whatsapp-copy"><span className={connection?.status === "active" ? "pill pill-green" : "pill pill-amber"}>{connection?.status === "active" ? "Connected" : "Not connected"}</span>
                  <h2>{connection?.status === "active" ? "Your WhatsApp channel is connected" : "Connect your WhatsApp Business account"}</h2>
                  <p>{connection?.status === "active" ? "Inbound messages can be received and processed by your SmeOps assistant." : "Complete Meta Embedded Signup to connect your WhatsApp Business number. Your access token will be exchanged and stored securely by the backend."}</p>
                  {connection?.status === "active" ? (
                    <div className="connection-details">
                      <span>Business number</span><strong>{connection.display_phone_number ?? connection.phone_number_id}</strong>
                      <span>Verified name</span><strong>{connection.verified_name ?? "Not returned by Meta"}</strong>
                      <span>WhatsApp Business Account</span><strong>{connection.business_account_id}</strong>
                      {connection.token_expires_at && <><span>Token expiry</span><strong>{dateTime(connection.token_expires_at)}</strong></>}
                      <button className="button button-danger-quiet disconnect-button" onClick={disableWhatsAppConnection} disabled={busy}>Disable connection</button>
                    </div>
                  ) : connection?.status === "pending" ? (
                    <div className="stack-form embedded-form">
                      <p className="muted">Your Meta authorization is saved. Complete the remaining registration step.</p>
                      {connection.onboarding_error_code && <p className="form-error">Meta error reference: {connection.onboarding_error_code}</p>}
                      <button className="button button-primary" onClick={retryWhatsAppConnection} disabled={busy}>Retry connection</button>
                      <label>Use a different six-digit PIN to restart onboarding
                        <input
                          inputMode="numeric"
                          pattern="[0-9]{6}"
                          maxLength={6}
                          type="password"
                          autoComplete="off"
                          value={signupPin}
                          onChange={(event) => {
                            const nextPin = event.target.value.replace(/\D/g, "").slice(0, 6);
                            signupPinRef.current = nextPin;
                            setSignupPin(nextPin);
                          }}
                          placeholder="New 6-digit PIN"
                        />
                      </label>
                      <button className="button button-quiet" onClick={launchEmbeddedSignup} disabled={busy || !metaSdkReady || signupPin.length !== 6}>Restart Embedded Signup</button>
                    </div>
                  ) : (
                    <div className="stack-form embedded-form">
                      <label>Choose a six-digit WhatsApp PIN
                        <input
                          inputMode="numeric"
                          pattern="[0-9]{6}"
                          maxLength={6}
                          type="password"
                          autoComplete="off"
                          value={signupPin}
                          onChange={(event) => {
                            const nextPin = event.target.value.replace(/\D/g, "").slice(0, 6);
                            signupPinRef.current = nextPin;
                            setSignupPin(nextPin);
                          }}
                          placeholder="6-digit PIN"
                        />
                      </label>
                      <p className="muted">This PIN enables WhatsApp two-step verification. It is encrypted by the backend and never returned to the browser after submission.</p>
                      <button className="button button-primary" onClick={launchEmbeddedSignup} disabled={busy || !metaSdkReady || signupPin.length !== 6}>
                        {metaSdkReady ? "Continue with Meta" : "Loading Meta signup…"}
                      </button>
                    </div>
                  )}
                  {signupStatus && <p className="signup-status">{signupStatus}</p>}
                </div>
              </section>
              <div className="notice-panel"><strong>Before connecting</strong><p>Use a business-owned number and complete the Meta Business verification and app review requirements. Never paste an access token into this dashboard.</p></div>
            </>
          )}
        </div>
      </section>
    </main>
  );
}

function AppointmentRow({ item, onCancel }: { item: Appointment; onCancel: (id: string) => void }) {
  return <div className="appointment-row"><div className="appointment-date"><strong>{new Date(item.starts_at).toLocaleDateString("en-NG", { day: "2-digit", timeZone: "Africa/Lagos" })}</strong><small>{new Date(item.starts_at).toLocaleDateString("en-NG", { month: "short", timeZone: "Africa/Lagos" })}</small></div><div className="appointment-customer"><strong>{item.customer_name}</strong><small>{item.customer_phone}</small></div><div className="appointment-time">{dateTime(item.starts_at)}</div><span className="pill pill-green">Confirmed</span><button className="button button-danger-quiet" onClick={() => onCancel(item.id)}>Cancel</button></div>;
}

function SetupStep({ done, title, detail, onClick }: { done: boolean; title: string; detail: string; onClick: () => void }) {
  return <button className="setup-step" onClick={onClick}><span className={done ? "step-check done" : "step-check"}>{done ? "✓" : ""}</span><span><strong>{title}</strong><small>{detail}</small></span><span className="setup-arrow">→</span></button>;
}

function EmptyState({ title, body }: { title: string; body: string }) {
  return <div className="empty-state"><span className="empty-symbol">◌</span><strong>{title}</strong><p>{body}</p></div>;
}
