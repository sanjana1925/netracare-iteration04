import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Bell, Bot, CalendarClock, Clock, CalendarDays, CalendarPlus, Check, ClipboardList, LogOut, MessageCircle, Plane, RefreshCw, Search,
  Send, Settings, ShieldAlert, Sparkles, Stethoscope, User, X } from "lucide-react";
import { api, setToken, hasToken, streamEvents } from "./api.js";
import { CLINIC_PHONE, clinicPhoneHref } from "./config.js";

const addDays = (iso, n) => {
  const d = new Date(iso + "T00:00:00Z"); // UTC throughout, so the local timezone can never shift the date
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};

const CATEGORY = {
  sudden_vision_loss: "Sudden vision loss",
  curtain_or_shadow: "Curtain or shadow",
  floaters_or_flashes: "Floaters or flashes",
  chemical_or_injury: "Chemical or injury",
  severe_pain_headache_haloes: "Severe pain, headache or haloes",
};

/* ---------- design pieces shared by every screen ---------- */
function LogoMark({ className = "logo-mark" }) {
  return (
    <svg className={className} viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <rect width="48" height="48" rx="14" fill="var(--brand)" />
      <path d="M6 24c4.5-8 11-12 18-12s13.5 4 18 12c-4.5 8-11 12-18 12S10.5 32 6 24z" fill="var(--surface)" />
      <circle cx="24" cy="24" r="8.5" fill="var(--accent)" />
      <circle cx="24" cy="24" r="4" fill="var(--brand)" />
      <circle cx="27" cy="21" r="1.7" fill="var(--surface)" />
    </svg>
  );
}

/* Role navigation: an accessible tab list (arrow keys move between tabs). Panels that must keep their state (the chat) stay mounted. */
function Tabs({ tabs, active, onChange, label }) {
  function onKey(e) {
    const i = tabs.findIndex((t) => t.id === active);
    if (e.key === "ArrowRight") { e.preventDefault(); onChange(tabs[(i + 1) % tabs.length].id); }
    if (e.key === "ArrowLeft") { e.preventDefault(); onChange(tabs[(i - 1 + tabs.length) % tabs.length].id); }
  }
  return (
    <div className="nav" role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <button key={t.id} role="tab" id={`tab-${t.id}`} aria-selected={active === t.id} aria-controls={`panel-${t.id}`}
          tabIndex={active === t.id ? 0 : -1} onClick={() => onChange(t.id)}>
          {t.icon}{t.label}{t.count > 0 && <span className="count" aria-label={`${t.count} waiting`}>{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

function Panel({ id, active, keep, children }) {
  if (!keep && active !== id) return null;
  return <div role="tabpanel" id={`panel-${id}`} aria-labelledby={`tab-${id}`} hidden={active !== id} className="panel">{children}</div>;
}

function Stat({ icon, n, label, tone = "" }) {
  return (
    <div className={`stat ${tone}`}>
      <span className="ico">{icon}</span>
      <div><b>{n}</b><span className="lbl">{label}</span></div>
    </div>
  );
}

function Empty({ icon, title, hint }) {
  return (
    <div className="empty-state">
      {icon}<strong>{title}</strong>{hint && <span>{hint}</span>}
    </div>
  );
}

function initials(name) {
  return name.replace(/^Dr\.?\s*/i, "").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
}

function Notice({ notice, onClose }) {
  if (!notice) return null;
  return (
    <>
      {/* A red-flag reason: the emergency response comes FIRST, in red, word for word from the backend. */}
      {notice.emergency && (
        <div className="emergency" role="alert">
          <ShieldAlert className="icon" aria-hidden="true" />
          <strong>{notice.emergency.message}</strong>
        </div>
      )}
      {notice.text && (
        <div className={`notice ${notice.kind}`} role="alert">
          <span>{notice.text}</span>
          <button className="link" onClick={onClose} aria-label="Dismiss"><X className="icon" aria-hidden="true" /></button>
        </div>
      )}
      {!notice.text && <button className="link" onClick={onClose}>Dismiss</button>}
    </>
  );
}

/* ---------- Home: a bento hero of soft pastel tiles (styles: .hero-bento in styles.css) ---------- */
const HERO_SPECIALITY_LABEL = { Paediatric: "Children" };
const NUMBER_WORDS = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"];

function Home({ doctors, onSymptom, onLogin }) {
  // The specialists tile is built from the doctors the API returns (each doctor's main speciality), not typed in.
  const heroSpecialities = [...new Set(doctors.map((d) => d.specialities?.[0]).filter(Boolean))];
  const heroCount = doctors.length;
  const heroLabel = heroCount > 0 && heroCount < NUMBER_WORDS.length ? `${NUMBER_WORDS[heroCount]} specialist${heroCount === 1 ? "" : "s"}` : "Our specialists";
  return (
    <div className="home calm">
      <header className="calm-top">
        <a className="logo" href="/" aria-label="NetraCare home"><LogoMark /><span className="brand-block"><span className="brand">NetraCare</span><span className="sub">Eye clinic</span></span></a>
        <button onClick={onLogin}>Log in</button>
      </header>
      <div className="hero-wrap">
        <section className="hero-bento" aria-label="Welcome to NetraCare">
          <div className="hero-tile hero-tile--headline">
            <div className="hero-eye" aria-hidden="true"><span className="hero-eye__outer" /><span className="hero-eye__mid" /><span className="hero-eye__core" /></div>
            <p className="hero-eyebrow">NetraCare Eye Clinic</p>
            <h1 className="hero-title">Look after the eyes that look after everything else.</h1>
            <p className="hero-sub">Tell us what you notice. We&apos;ll find the right specialist and a time that&apos;s actually free.</p>
            <div className="hero-actions">
              <button type="button" className="hero-btn hero-btn--dark" onClick={onLogin}>Book an appointment</button>
              <button type="button" className="hero-btn hero-btn--light" onClick={onLogin}>Sign in</button>
            </div>
          </div>

          <section className="hero-tile hero-tile--emergency" aria-label="Eye emergency">
            <p className="hero-tile__label"><AlertTriangle size={18} aria-hidden="true" />Eye emergency</p>
            <p className="hero-tile__text">Sudden vision loss or an injury? Don&apos;t book online. Call <a href={clinicPhoneHref()}>{CLINIC_PHONE}</a> now.</p>
          </section>

          <section className="hero-tile hero-tile--chat" aria-label="Chat assistant">
            <p className="hero-tile__label">Chat assistant</p>
            <p className="hero-tile__text">Not sure who to see? Describe what you notice.</p>
            <p className="hero-bubble"><span className="sr-only">Example: </span>My daughter squints when she watches TV.</p>
            {/* a visitor who is not signed in goes to the symptom form: a quick patient sign-in, then the assistant opens with their words */}
            <button type="button" className="hero-btn hero-btn--lavender" onClick={onSymptom}>Ask now</button>
          </section>

          <section className="hero-tile hero-tile--specialists" aria-label="Our specialists">
            <p className="hero-tile__label">{heroLabel}</p>
            {heroSpecialities.length > 0 && (
              <ul className="hero-chips">{heroSpecialities.map((sp) => <li className="hero-chip" key={sp}>{HERO_SPECIALITY_LABEL[sp] || sp}</li>)}</ul>
            )}
          </section>

          <section className="hero-tile hero-tile--opening" aria-label="Opening days">
            <p className="hero-tile__label"><Clock size={18} aria-hidden="true" />Opening</p>
            <p className="hero-days">Mon to Sat</p>
            <p className="hero-tile__text">Morning and afternoon sessions. Closed Sundays and holidays.</p>
          </section>
        </section>
      </div>
      <footer className="home-foot"><span>NetraCare is a fictional clinic. All data is synthetic.</span></footer>
    </div>
  );
}

/* A first-time visitor starts with a symptom. The assistant is only for an identified patient (so it can book safely and the red-flag check
   runs on the right person), so this asks for a mobile number and name, then opens the chat and sends the symptom through the normal checks. */
function StartWithSymptom({ onLoggedIn, onBack }) {
  const [symptom, setSymptom] = useState("");
  const [mobile, setMobile] = useState("");
  const [name, setName] = useState("");
  const [err, setErr] = useState(null);
  async function submit(e) {
    e.preventDefault();
    setErr(null);
    try {
      const r = await api("/login", { method: "POST", body: { role: "patient", mobile, name } });
      setToken(r.token);
      sessionStorage.setItem("nc_first_message", symptom.trim());
      onLoggedIn(r.user);
    } catch (e2) { setErr({ kind: "error", text: e2.message }); }
  }
  return (
    <div className="card login">
      <button className="link backlink" onClick={onBack}>← Back</button>
      <div className="login-hero">
        <LogoMark />
        <h1>What's wrong with your eyes?</h1>
        <p>Describe it in your own words. The assistant will suggest the right specialist and look for a time that is free.</p>
      </div>
      <form onSubmit={submit}>
        <label>In your own words
          <textarea value={symptom} maxLength={500} rows={3} onChange={(e) => setSymptom(e.target.value)} required
            placeholder="For example: my eyes hurt when I read the board at school" />
        </label>
        <label>Mobile number<input value={mobile} onChange={(e) => setMobile(e.target.value)} placeholder="9000000001" inputMode="numeric" required /></label>
        <label>Your name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Asha Verma" required /></label>
        <button className="primary" type="submit">Tell the assistant</button>
      </form>
      <Notice notice={err} onClose={() => setErr(null)} />
      <p className="hint">Sudden loss of vision, a chemical in the eye or an injury? Call {CLINIC_PHONE} or come in now. We ask for your mobile number and name so the assistant can book for you and keep your details private. A new number creates a new patient. Classroom login only: no passwords.</p>
    </div>
  );
}

/* ---------- Login (R3) ---------- */
function Login({ doctors, onLoggedIn, onBack }) {
  const [role, setRole] = useState("patient");
  const [mobile, setMobile] = useState("");
  const [name, setName] = useState("");
  const [doctorId, setDoctorId] = useState("");
  const [err, setErr] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setErr(null);
    try {
      const body = role === "patient" ? { role, mobile, name } : role === "staff" ? { role, name } : { role, doctor_id: doctorId };
      const r = await api("/login", { method: "POST", body });
      setToken(r.token);
      onLoggedIn(r.user);
    } catch (e2) {
      setErr({ kind: "error", text: e2.message });
    }
  }

  return (
    <div className="card login">
      {onBack && <button className="link backlink" onClick={onBack}>← Back</button>}
      <div className="login-hero">
        <LogoMark />
        <h1>Welcome to NetraCare</h1>
        <p>Your eye clinic, in one place. Log in to book, chat and manage visits.</p>
      </div>
      <div className="tabs" role="tablist" aria-label="Log in as">
        {["patient", "staff", "doctor"].map((r) => (
          <button key={r} role="tab" aria-selected={role === r} className={role === r ? "tab on" : "tab"} onClick={() => { setRole(r); setErr(null); }}>
            {r[0].toUpperCase() + r.slice(1)}
          </button>
        ))}
      </div>
      <form onSubmit={submit}>
        {role === "patient" && (
          <>
            <label>Mobile number<input value={mobile} onChange={(e) => setMobile(e.target.value)} placeholder="9000000001" inputMode="numeric" required /></label>
            <label>Your name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Asha Verma" required /></label>
            <p className="hint">A new mobile number creates a new patient.</p>
          </>
        )}
        {role === "staff" && <label>Your name<input value={name} onChange={(e) => setName(e.target.value)} required /></label>}
        {role === "doctor" && (
          <label>Doctor
            <select value={doctorId} onChange={(e) => setDoctorId(e.target.value)} required>
              <option value="">Pick your name…</option>
              {doctors.map((d) => <option key={d.doctor_id} value={d.doctor_id}>{d.name}</option>)}
            </select>
          </label>
        )}
        <button className="primary" type="submit">Log in</button>
      </form>
      <Notice notice={err} onClose={() => setErr(null)} />
      <p className="hint">Classroom login only: no passwords, so this is not real security.</p>
    </div>
  );
}

/* ---------- R5: Fact of the Day ---------- */
function FactCard() {
  const [fact, setFact] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (previous) => {
    setLoading(true);
    try {
      // first load: the fact saved for this patient comes back at once. Refresh: a different fact is written and saved.
      const q = previous ? `?refresh=true&previous=${encodeURIComponent(previous)}` : "";
      setFact(await api(`/fact-of-the-day${q}`));
    } catch {
      // the backend never fails this call; if the server itself is unreachable, hide the card quietly
      setFact(null);
    }
    setLoading(false);
  }, []);
  useEffect(() => { load(); }, [load]);

  if (!fact && !loading) return null;
  return (
    <div className="card fact">
      <div className="fact-head"><h2><Sparkles className="icon" aria-hidden="true" />Fact of the Day</h2>
        <button onClick={() => load(fact?.fact)} disabled={loading}><RefreshCw className="icon" aria-hidden="true" />{loading ? "Loading…" : "Refresh"}</button></div>
      {fact ? <p className="fact-text">{fact.fact}</p> : <div className="fact-text" aria-label="Loading"><div className="skeleton" style={{ width: "92%" }} /><div className="skeleton" style={{ width: "64%", marginTop: 8 }} /></div>}
      {/* The caveat is sent by the backend code, never written by the LLM */}
      {fact && <p className="hint caveat">{fact.caveat}</p>}
    </div>
  );
}

/* ---------- Iteration 3/4: the patient chat, now a booking agent (threads, confirmation card) ---------- */
const newThread = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : String(Date.now()) + Math.random().toString(36).slice(2));

const SUGGESTIONS = [
  "What appointments do I have?",
  "Find me a free time this week",
  "What time do you open on Saturday?",
  "How much does cataract surgery cost?",
];
const ACTION_TITLE = { book: "Book appointment", cancel: "Cancel appointment", reschedule: "Move appointment" };

function ChatPanel({ onChanged }) {
  const [thread, setThread] = useState(newThread);
  const [messages, setMessages] = useState([]);   // {role, content, kind, sources}
  const [pending, setPending] = useState(null);   // {summary, action}: a confirmation card is waiting
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const logRef = React.useRef(null);
  useEffect(() => { const el = logRef.current; if (el) el.scrollTop = el.scrollHeight; }, [messages, pending, busy]);

  async function post(body, userText) {
    setBusy(true);
    if (userText) setMessages((m) => [...m, { role: "user", content: userText }]);
    setPending(null);
    try {
      const r = await api("/chat", { method: "POST", body: { thread_id: thread, ...body } });
      setMessages((m) => [...m, { role: "assistant", content: r.reply, kind: r.kind, sources: r.sources }]);
      setPending(r.pending_action || null);
      if (r.kind !== "pending") onChanged && onChanged(); // an action may have changed the appointments below
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", content: err.message, kind: "error", sources: [] }]);
    }
    setBusy(false);
  }
  function send(e) {
    e.preventDefault();
    const msg = text.trim();
    if (!msg || busy) return;
    setText("");
    post({ message: msg }, msg); // a typed message while a card is waiting counts as No (the server handles it)
  }
  const quick = (t) => { if (!busy) post({ message: t }, t); };
  useEffect(() => {  // a visitor who started with a symptom: send it now, through the normal red-flag check
    const first = sessionStorage.getItem("nc_first_message");
    if (first) { sessionStorage.removeItem("nc_first_message"); post({ message: first }, first); }
  }, []); // eslint-disable-line
  const reset = () => { setThread(newThread()); setMessages([]); setPending(null); setText(""); };

  return (
    <div className="card chat">
      <div className="fact-head"><h2><MessageCircle className="icon" aria-hidden="true" />Ask NetraCare</h2>
        <button onClick={reset}><RefreshCw className="icon" aria-hidden="true" />New chat</button></div>
      <div className="chat-log" aria-live="polite" ref={logRef}>
        {messages.length === 0 && (
          <div className="welcome">
            <LogoMark />
            <h3>How can I help today?</h3>
            <p className="hint">Ask me to find free times, book, cancel or reschedule your appointments, or ask about timings, fees, what to bring and procedures. I always ask you to confirm before I change anything.</p>
            <div className="suggest">{SUGGESTIONS.map((t) => <button key={t} onClick={() => quick(t)} disabled={busy}>{t}</button>)}</div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <span className="av" aria-hidden="true">{m.role === "user" ? <User className="icon" /> : <Bot className="icon" />}</span>
            <div className={`bubble ${m.role} ${m.kind || ""}`}>
              {m.kind === "emergency" ? <strong>{m.content}</strong> : m.content}
              {m.sources?.length > 0 && <div className="sources">Sources: {m.sources.join("; ")}</div>}
            </div>
          </div>
        ))}
        {pending && (
          <div className="confirm-card" role="group" aria-label="Please confirm">
            <div className="confirm-title"><CalendarClock className="icon" aria-hidden="true" />{ACTION_TITLE[pending.action] || "Please confirm"}</div>
            <div className="confirm-summary">{pending.summary}</div>
            <div className="confirm-actions">
              <button className="primary" disabled={busy} onClick={() => post({ confirm: true })}><Check className="icon" aria-hidden="true" />Confirm</button>
              <button disabled={busy} onClick={() => post({ confirm: false })}><X className="icon" aria-hidden="true" />No</button>
            </div>
          </div>
        )}
        {busy && (
          <div className="msg assistant"><span className="av" aria-hidden="true"><Bot className="icon" /></span>
            <div className="bubble assistant typing"><span className="dots" aria-hidden="true"><i /><i /><i /></span>Thinking…</div></div>
        )}
      </div>
      <form className="chat-input" onSubmit={send}>
        <input value={text} maxLength={500} onChange={(e) => setText(e.target.value)} placeholder="Type your message…" aria-label="Your message (500 characters at most)" />
        <button className="primary" type="submit" disabled={busy || !text.trim()}><Send className="icon" aria-hidden="true" />Send</button>
      </form>
    </div>
  );
}

/* ---------- Doctors list ---------- */
function DoctorList({ doctors }) {
  return (
    <div className="card">
      <h2><Stethoscope className="icon" aria-hidden="true" />Our doctors</h2>
      <ul className="doctors">
        {doctors.map((d) => (
          <li key={d.doctor_id}><span className="dot" aria-hidden="true">{initials(d.name)}</span>
            <div className="meta"><b>{d.name}</b><span>{d.specialities.join(", ")}</span></div></li>
        ))}
      </ul>
    </div>
  );
}

/* ---------- Booking / rescheduling panel (R4, R5) ---------- */
function BookPanel({ doctors, today, user, patients, reschedule, onDone, onCancelReschedule, notify }) {
  const [doctorId, setDoctorId] = useState("");
  const [speciality, setSpeciality] = useState("");
  const [visitType, setVisitType] = useState("new");
  const [date, setDate] = useState(addDays(today, 1));
  const [reason, setReason] = useState("");
  const [slots, setSlots] = useState(null);
  const [patientId, setPatientId] = useState("");
  const doctor = doctors.find((d) => d.doctor_id === doctorId);

  useEffect(() => {
    if (reschedule) { setDoctorId(reschedule.doctor_id); setSpeciality(reschedule.speciality); setVisitType(reschedule.visit_type); }
  }, [reschedule]);

  useEffect(() => {
    if (!doctorId || !speciality || !date) { setSlots(null); return; }
    let live = true;
    api(`/slots?doctor_id=${doctorId}&date=${date}&speciality=${encodeURIComponent(speciality)}&visit_type=${visitType}`)
      .then((r) => live && setSlots(r))
      .catch((e) => { if (live) { setSlots(null); notify({ kind: "error", text: e.message }); } });
    return () => { live = false; };
  }, [doctorId, speciality, visitType, date]); // eslint-disable-line

  const refreshSlots = () =>
    api(`/slots?doctor_id=${doctorId}&date=${date}&speciality=${encodeURIComponent(speciality)}&visit_type=${visitType}`).then(setSlots).catch(() => {});

  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState("");  // the start time chosen by clicking; the Book button then books it
  useEffect(() => { setSelected(""); }, [doctorId, speciality, visitType, date]);
  useEffect(() => { if (selected && slots && !slots.slots.includes(selected)) setSelected(""); }, [slots, selected]);

  async function book(start) {
    if (busy) return; // one request at a time: a double-click must not fire two bookings
    const body = { doctor_id: doctorId, speciality, visit_type: visitType, date, start_time: start };
    if (!reschedule) {
      if (!reason.trim()) return notify({ kind: "error", text: "Please tell us the reason for your visit." });
      body.reason = reason.trim();
    }
    setBusy(true);
    try {
      if (reschedule) {
        await api(`/appointments/${reschedule.appointment_id}/reschedule`, { method: "POST", body });
        notify({ kind: "ok", text: `Rescheduled to ${date} at ${start} with ${doctor.name}.` });
      } else {
        if (user.role === "staff") {
          if (!patientId) { setBusy(false); return notify({ kind: "error", text: "Choose the patient this booking is for." }); }
          body.patient_id = patientId;
        }
        const appt = await api("/appointments", { method: "POST", body });
        notify({ kind: "ok", emergency: appt.emergency, text: `Booked ${speciality} with ${doctor.name} on ${date} at ${start}.` });
        setReason("");
      }
      setSelected("");
      refreshSlots(); // the booked slot must disappear from the list straight away
      onDone();
    } catch (e) {
      // A refusal shows the backend's plain-words message, and the slot list is refreshed.
      // A refusal shows the backend's plain-words message. If the reason was a red flag, the emergency response is shown first.
      notify({ kind: "error", emergency: e.emergency, text: e.message + (e.toStaff ? " (Staff can help with this.)" : "") });
      refreshSlots();
      if (e.emergency) onDone(); // the urgent case stands even though the booking was refused
    }
    setBusy(false);
  }

  return (
    <div className="card">
      <h2><CalendarPlus className="icon" aria-hidden="true" />{reschedule ? `Reschedule ${reschedule.appointment_id}` : user.role === "staff" ? "Book for a patient" : "Book an appointment"}</h2>
      {reschedule && <p className="hint">The old appointment is cancelled only if the new one is accepted. <button className="link" onClick={onCancelReschedule}>Stop rescheduling</button></p>}
      <div className="row">
        {user.role === "staff" && !reschedule && (
          <label>Patient
            <select value={patientId} onChange={(e) => setPatientId(e.target.value)}>
              <option value="">Choose patient…</option>
              {patients.map((p) => <option key={p.patient_id} value={p.patient_id}>{p.name} ({p.mobile})</option>)}
            </select>
          </label>
        )}
        <label>Doctor
          <select value={doctorId} onChange={(e) => { setDoctorId(e.target.value); setSpeciality(""); }}>
            <option value="">Choose doctor…</option>
            {doctors.map((d) => <option key={d.doctor_id} value={d.doctor_id}>{d.name}</option>)}
          </select>
        </label>
        <label>Speciality
          <select value={speciality} onChange={(e) => setSpeciality(e.target.value)} disabled={!doctor}>
            <option value="">Choose…</option>
            {(doctor?.specialities || []).map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label>Visit
          <select value={visitType} onChange={(e) => setVisitType(e.target.value)}>
            <option value="new">New consultation (20 min)</option>
            <option value="follow_up">Follow-up (10 min)</option>
          </select>
        </label>
        <label>Date
          <input type="date" value={date} min={today} max={addDays(today, 30)} onChange={(e) => setDate(e.target.value)} />
        </label>
      </div>
      {!reschedule && (
        <label>What is the reason for your visit?
          <textarea value={reason} maxLength={300} rows={2} required onChange={(e) => setReason(e.target.value)}
            placeholder="For example: routine eye test and new glasses" />
          <span className="hint counter">{reason.length}/300</span>
        </label>
      )}
      {slots && (
        slots.slots.length === 0
          ? <p className="empty">No free slots for {doctor?.name} on this date. Try another day.</p>
          : <>
              <p className="hint">{slots.slots.length} free start times ({slots.duration_minutes} min each). Pick one, then press {reschedule ? "Reschedule" : "Book"}:</p>
              <div className="slots">
                {slots.slots.map((t) => (
                  <button key={t} type="button" className={`slot ${selected === t ? "selected" : ""}`} aria-pressed={selected === t}
                    disabled={busy} onClick={() => setSelected(t)}>{t}</button>
                ))}
              </div>
              <div className="row book-row">
                <button type="button" className="primary book-btn" disabled={!selected || busy} onClick={() => book(selected)}>
                  {busy ? "Booking…" : reschedule ? "Reschedule" : "Book"}{selected && !busy ? ` ${selected}` : ""}
                </button>
                {!selected && <span className="hint">Choose a time above first.</span>}
              </div>
            </>
      )}
    </div>
  );
}

/* ---------- Appointments table ---------- */
function ApptTable({ rows, onCancel, onReschedule, onComplete, onOpen, showPatient, showDoctor }) {
  if (!rows.length) return <Empty icon={<CalendarDays className="icon" aria-hidden="true" />} title="No appointments to show" hint="When there is one, it will appear here." />;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Date</th><th>Time</th>{showDoctor && <th>Doctor</th>}{showPatient && <th>Patient</th>}<th>Speciality</th><th>Visit</th><th>Reason</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.appointment_id} className={a.status !== "booked" ? "dim" : a.urgent ? "urgent-row" : ""}>
              <td>{a.date}</td><td className="mono">{a.start_time}–{a.end_time}</td>
              {showDoctor && <td>{a.doctor_name}</td>}
              {showPatient && <td>{a.patient_name}</td>}
              <td>{a.speciality}</td><td>{a.visit_type === "new" ? "New" : "Follow-up"}</td>
              <td className="reason">{a.reason || "—"}{a.moved_note && <div className="hint">{a.moved_note}</div>}</td>
              <td>{a.urgent ? <span className="badge urgent">Urgent</span> : null} <span className={`badge ${a.status}`}>{a.status}</span>
                {a.approval_status && a.status === "booked" && <div className="hint">{a.approval_status === "pending" ? `Waiting for Staff: ${a.approval_action} request` : `Staff rejected the ${a.approval_action} request`}</div>}
                {a.approval_status === "approved" && a.status === "cancelled" && <div className="hint">Cancelled after Staff approval</div>}</td>
              <td className="actions">
                {a.status === "booked" && onOpen && <button onClick={() => onOpen(a)}>Open patient</button>}
                {a.status === "booked" && onCancel && a.approval_status !== "pending" && <button onClick={() => onCancel(a)}>Cancel</button>}
                {a.status === "booked" && onReschedule && <button onClick={() => onReschedule(a)}>Reschedule</button>}
                {a.status === "booked" && onComplete && <button className="primary" onClick={() => onComplete(a)}>Mark completed</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---------- Role dashboards ---------- */
function PatientView({ user, doctors, today, notify }) {
  const [rows, setRows] = useState([]);
  const [resched, setResched] = useState(null);
  const load = useCallback(() => api("/appointments").then(setRows).catch((e) => notify({ kind: "error", text: e.message })), []); // eslint-disable-line
  useEffect(() => { load(); }, [load]);

  async function cancel(a) {
    try {
      await api(`/appointments/${a.appointment_id}/cancel`, { method: "POST" });
      notify({ kind: "ok", text: `Cancelled ${a.speciality} on ${a.date} at ${a.start_time}. The slot is free again.` });
    } catch (e) { notify({ kind: "error", text: e.message }); }
    load();
  }
  const [tab, setTab] = useState("assistant");
  const upcoming = rows.filter((r) => r.status === "booked").length;
  const tabs = [
    { id: "assistant", label: "Assistant", icon: <MessageCircle className="icon" aria-hidden="true" /> },
    { id: "book", label: "Book", icon: <CalendarPlus className="icon" aria-hidden="true" /> },
    { id: "appointments", label: "My appointments", icon: <ClipboardList className="icon" aria-hidden="true" />, count: 0 },
    { id: "doctors", label: "Doctors", icon: <Stethoscope className="icon" aria-hidden="true" /> },
  ];
  return (
    <>
      <Tabs tabs={tabs} active={tab} onChange={setTab} label="Patient sections" />
      <Panel id="assistant" active={tab} keep>
        <FactCard />
        <ChatPanel onChanged={load} />
      </Panel>
      <Panel id="book" active={tab}>
        <BookPanel doctors={doctors} today={today} user={user} reschedule={resched} notify={notify}
          onDone={() => { setResched(null); load(); setTab("appointments"); }} onCancelReschedule={() => setResched(null)} />
      </Panel>
      <Panel id="appointments" active={tab}>
        <div className="card"><h2><ClipboardList className="icon" aria-hidden="true" />My appointments {upcoming > 0 && <span className="badge">{upcoming} upcoming</span>}</h2>
          <ApptTable rows={rows} showDoctor onCancel={cancel} onReschedule={(a) => { setResched(a); setTab("book"); }} /></div>
      </Panel>
      <Panel id="doctors" active={tab}><DoctorList doctors={doctors} /></Panel>
    </>
  );
}

/* ---------- Stretch: week view calendar for a doctor (Staff choose the doctor; a doctor sees their own) ---------- */
function WeekCalendar({ doctors, fixedDoctor, today }) {
  const [doctorId, setDoctorId] = useState(fixedDoctor ? "" : (doctors[0]?.doctor_id || ""));
  const [date, setDate] = useState(today);
  const [week, setWeek] = useState(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (!fixedDoctor && !doctorId) return;
    let live = true;
    const q = new URLSearchParams({ date });
    if (!fixedDoctor) q.set("doctor_id", doctorId);
    api(`/calendar?${q}`).then((w) => { if (live) { setWeek(w); setErr(""); } }).catch((e) => live && setErr(e.message));
    return () => { live = false; };
  }, [doctorId, date, fixedDoctor]);
  return (
    <div className="card"><h2>Week view{week ? `: ${week.doctor}` : ""}</h2>
      <div className="row">
        {!fixedDoctor && (
          <label>Doctor
            <select value={doctorId} onChange={(e) => setDoctorId(e.target.value)}>
              {doctors.map((d) => <option key={d.doctor_id} value={d.doctor_id}>{d.name}</option>)}
            </select>
          </label>
        )}
        <button onClick={() => week && setDate(week.prev_week)}>← Previous week</button>
        <button onClick={() => setDate(today)}>This week</button>
        <button onClick={() => week && setDate(week.next_week)}>Next week →</button>
      </div>
      {err && <p className="notice error">{err}</p>}
      {week && (
        <div className="week">
          {week.days.map((d) => (
            <div className={`day ${d.holiday ? "holiday" : ""}`} key={d.date}>
              <div className="day-head"><b>{d.weekday}</b> <span className="mono">{d.date.slice(5)}</span></div>
              {d.holiday && <div className="hint">Closed: {d.holiday}</div>}
              {["AM", "PM"].map((s) => (
                <div key={s} className={`sess ${d.holiday ? "OFF" : d.sessions[s]}`}>
                  <span>{s}</span><span>{d.holiday ? "closed" : d.sessions[s] === "SURGERY" ? "surgery" : d.sessions[s] === "OFF" ? "off" : "OPD"}</span>
                </div>
              ))}
              {d.appointments.map((a) => (
                <div className={`wk-appt ${a.status}`} key={a.appointment_id} title={`${a.speciality} (${a.visit_type})`}>
                  <span className="mono">{a.start_time}</span> {a.patient_name}
                  <div className="hint">{a.speciality}{a.visit_type === "follow_up" ? " · follow-up" : ""}</div>
                </div>
              ))}
              {!d.holiday && d.appointments.length === 0 && <div className="hint">No appointments</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------- Stretch: Staff approval queue ---------- */
function ApprovalsPanel({ items, onDecide }) {
  return (
    <div className="card"><h2>Approvals waiting ({items.length})</h2>
      {items.length === 0 && <p className="empty">No requests waiting.</p>}
      {items.map((a) => (
        <div className="request" key={a.approval_id}>
          <div>
            <b>{a.patient_name}</b> ({a.patient_mobile}) wants to <b>{a.action}</b> their {a.speciality} appointment with {a.doctor_name} on {a.date} {a.start_time}
            {a.action === "reschedule" && <> → <b>{a.new_doctor_name}, {a.new_date} {a.new_start_time}</b></>}
            <div className="hint">Requested {a.created_at.replace("T", " ")} · it starts in under 2 hours</div>
          </div>
          <div className="actions"><button className="primary" onClick={() => onDecide(a, "approve")}>Approve</button>
            <button onClick={() => onDecide(a, "reject")}>Reject</button></div>
        </div>
      ))}
    </div>
  );
}

/* ---------- Stretch: Staff add holidays and edit a doctor's weekly schedule ---------- */
const DAYS6 = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function ClinicSettings({ doctors, today, notify }) {
  const [holidays, setHolidays] = useState([]);
  const [schedule, setSchedule] = useState([]);
  const [date, setDate] = useState("");
  const [name, setName] = useState("");
  const load = useCallback(() => {
    api("/holidays").then(setHolidays).catch(() => {});
    api("/schedule").then(setSchedule).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  async function addHoliday(e) {
    e.preventDefault();
    try {
      await api("/holidays", { method: "POST", body: { date, name } });
      notify({ kind: "ok", text: `Added holiday ${date} (${name}). No one can book that day now.` });
      setDate(""); setName(""); load();
    } catch (e2) { notify({ kind: "error", text: e2.message }); }
  }
  async function removeHoliday(h) {
    try { await api(`/holidays/${h.date}`, { method: "DELETE" }); notify({ kind: "ok", text: `Removed holiday ${h.date}.` }); load(); }
    catch (e2) { notify({ kind: "error", text: e2.message }); }
  }
  async function setStatus(doctor_id, day, session, status) {
    try {
      await api("/schedule", { method: "PUT", body: { doctor_id, day, session, status } });
      notify({ kind: "ok", text: `${doctors.find((d) => d.doctor_id === doctor_id)?.name}: ${day} ${session} is now ${status}.` });
    } catch (e2) { notify({ kind: "error", text: e2.message }); }
    load();
  }
  const cell = (doctor_id, day, session) => (schedule.find((s) => s.doctor_id === doctor_id && s.day === day && s.session === session) || {}).status || "OFF";
  const upcoming = holidays.filter((h) => h.date >= today);
  return (
    <div className="card"><h2>Clinic settings</h2>
      <h3>Holidays</h3>
      <form className="row" onSubmit={addHoliday}>
        <label>Date<input type="date" value={date} min={today} onChange={(e) => setDate(e.target.value)} required /></label>
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Reason for closing" required /></label>
        <button className="primary" type="submit">Add holiday</button>
      </form>
      <div className="chips">
        {upcoming.length === 0 && <span className="hint">No upcoming holidays.</span>}
        {upcoming.map((h) => <span className="chip" key={h.date}>{h.date} · {h.name} <button className="link" aria-label={`Remove ${h.date}`} onClick={() => removeHoliday(h)}>×</button></span>)}
      </div>
      <h3>Weekly schedule</h3>
      <p className="hint">OPD = takes patients, SURGERY = operating (not bookable), OFF = not at the clinic. Changes apply at once. A change that would leave an existing booking breaking the rules is refused.</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Doctor</th>{DAYS6.map((d) => <th key={d}>{d} AM / PM</th>)}</tr></thead>
          <tbody>
            {doctors.map((doc) => (
              <tr key={doc.doctor_id}>
                <td className="name">{doc.name}</td>
                {DAYS6.map((day) => (
                  <td key={day}><div className="halves">
                    {["AM", "PM"].map((s) => (
                      <select key={s} aria-label={`${doc.name} ${day} ${s}`} className={`sel ${cell(doc.doctor_id, day, s)}`} value={cell(doc.doctor_id, day, s)}
                        onChange={(e) => setStatus(doc.doctor_id, day, s, e.target.value)}>
                        <option value="OPD">OPD</option><option value="SURGERY">SX</option><option value="OFF">off</option>
                      </select>
                    ))}
                  </div></td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ---------- Iteration 2: urgent cases (R4) ---------- */
function UrgentList({ cases, onSeen }) {
  if (!cases.length) return null;
  return (
    <div className="urgent-panel" role="region" aria-label="Urgent cases">
      <h2>Urgent cases ({cases.length})</h2>
      {cases.map((c) => (
        <div className="urgent-case" key={c.urgent_id}>
          <div>
            <b>{c.patient_name}</b> <span className="mono">{c.patient_mobile}</span>
            <span className="badge urgent">{CATEGORY[c.category] || c.category}</span>{c.source === "chat" && <span className="badge">from chat</span>}
            <div className="urgent-reason">“{c.reason}”</div>
            <div className="hint">AI note: {c.why} · {c.created_at.replace("T", " ")}</div>
          </div>
          <button onClick={() => onSeen(c)}>Mark seen</button>
        </div>
      ))}
    </div>
  );
}

function NewWalkIn({ onCreated, notify }) {
  const [name, setName] = useState("");
  const [mobile, setMobile] = useState("");
  async function submit(e) {
    e.preventDefault();
    try {
      const p = await api("/patients", { method: "POST", body: { name, mobile } });
      notify({ kind: "ok", text: `Registered ${p.name} (${p.patient_id}). Choose them in "Book for a patient" below.` });
      setName(""); setMobile("");
      onCreated();
    } catch (e2) { notify({ kind: "error", text: e2.message }); }
  }
  return (
    <form className="card" onSubmit={submit}>
      <h2>Register a new walk-in</h2>
      <div className="row">
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} required /></label>
        <label>Mobile number<input value={mobile} onChange={(e) => setMobile(e.target.value)} inputMode="numeric" placeholder="10 digits" required /></label>
        <button className="primary" type="submit">Register</button>
      </div>
    </form>
  );
}

function UrgentBadge({ count }) {
  useEffect(() => {
    document.title = count ? `(${count}) Urgent · NetraCare` : "NetraCare";
    return () => { document.title = "NetraCare"; };
  }, [count]);
  return (
    <div className={`urgent-badge ${count ? "on" : ""}`} role="status" aria-live="polite" data-testid="urgent-badge">
      <span className="count">{count}</span> unseen urgent {count === 1 ? "case" : "cases"}
    </div>
  );
}

const fmt = (d, t) => `${d} ${t}`;

function RebookingsPanel({ items, onDecide }) {
  if (!items.length) return null;
  return (
    <div className="card"><h2>Rebooking after doctor leave ({items.length})</h2>
      {items.map((r) => (
        <div className="request" key={r.rebooking_id}>
          <div>
            <b>{r.patient_name}</b> ({r.patient_mobile}) · {r.speciality} {r.visit_type === "new" ? "new" : "follow-up"}
            <div>Was: {r.original_doctor}, {fmt(r.original_date, r.original_start)}</div>
            {r.status === "no_slot"
              ? <div className="notice error">No slot: call the patient</div>
              : <div>Proposed: <b>{r.new_doctor}, {fmt(r.new_date, r.new_start)}</b></div>}
          </div>
          {r.status === "pending" && <div className="actions"><button className="primary" onClick={() => onDecide(r, "approve")}>Approve</button>
            <button onClick={() => onDecide(r, "reject")}>Reject</button></div>}
        </div>
      ))}
    </div>
  );
}

/* ---------- Iteration 3: Staff search debug view (R2) ---------- */
function SearchDebug() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState("");
  async function run(e) {
    e.preventDefault();
    setErr("");
    try { setRes(await api(`/staff/search-debug?q=${encodeURIComponent(q)}`)); }
    catch (e2) { setRes(null); setErr(e2.message); }
  }
  const col = (title, rows) => (
    <div className="dbg-col"><h3>{title}</h3>
      {rows.length === 0 && <p className="empty">No matches</p>}
      {rows.map((r, i) => (
        <div className="dbg-row" key={r.chunk_id}><b>{i + 1}. {r.source} › {r.section}</b><span className="mono">score {r.score}</span></div>
      ))}
    </div>
  );
  return (
    <div className="card"><h2>Search debug view</h2>
      <form className="row" onSubmit={run}>
        <label>Type a question<input value={q} onChange={(e) => setQ(e.target.value)} required /></label>
        <button className="primary" type="submit">Search</button>
      </form>
      {err && <p className="notice error">{err}</p>}
      {res && <div className="dbg">{col("Keyword (BM25)", res.keyword)}{col("Vector", res.vector)}{col("Hybrid (top 4)", res.hybrid)}</div>}
    </div>
  );
}

function StaffView({ user, doctors, today, notify }) {
  const [rows, setRows] = useState([]);
  const [patients, setPatients] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [urgent, setUrgent] = useState([]);
  const [rebookings, setRebookings] = useState([]);
  const [tab, setTab] = useState("today");
  const [todayCount, setTodayCount] = useState(0);
  const [date, setDate] = useState("");
  const [doctorId, setDoctorId] = useState("");
  const [resched, setResched] = useState(null);
  const load = useCallback(() => {
    const q = new URLSearchParams();
    if (date) q.set("date", date);
    if (doctorId) q.set("doctor_id", doctorId);
    api(`/appointments?${q}`).then(setRows).catch((e) => notify({ kind: "error", text: e.message }));
    api("/staff/approvals").then(setApprovals).catch(() => {});
    api("/urgent-cases").then(setUrgent).catch(() => {});
    api("/staff/rebookings").then(setRebookings).catch(() => {});
    api(`/appointments?date=${today}&status=booked`).then((r) => setTodayCount(r.length)).catch(() => {});
  }, [date, doctorId, today]); // eslint-disable-line
  useEffect(() => { load(); }, [load]);
  useEffect(() => { const t = setInterval(() => api("/urgent-cases").then(setUrgent).catch(() => {}), 15000); return () => clearInterval(t); }, []);
  const loadPatients = useCallback(() => api("/patients").then(setPatients).catch(() => {}), []);
  useEffect(() => { loadPatients(); }, [loadPatients]);

  async function cancel(a) {
    try {
      await api(`/appointments/${a.appointment_id}/cancel`, { method: "POST" });
      notify({ kind: "ok", text: `Cancelled ${a.patient_name}'s ${a.speciality} appointment (${a.date} ${a.start_time}).` });
    } catch (e) { notify({ kind: "error", text: e.message }); }
    load();
  }
  async function decideRebooking(item, verb) {
    try {
      await api(`/staff/rebookings/${item.rebooking_id}/${verb}`, { method: "POST" });
      notify({ kind: "ok", text: `${verb === "approve" ? "Approved" : "Rejected"} the new time for ${item.patient_name}.` });
    } catch (e) { notify({ kind: "error", text: e.message }); }
    load();
  }
  async function seen(c) {
    try { await api(`/urgent-cases/${c.urgent_id}/seen`, { method: "POST" }); } catch (e) { notify({ kind: "error", text: e.message }); }
    load();
  }
  async function decide(item, verb) {
    try {
      await api(`/staff/approvals/${item.approval_id}/${verb}`, { method: "POST" });
      notify({ kind: "ok", text: `${verb === "approve" ? "Approved" : "Rejected"} ${item.patient_name}'s ${item.action} request.` });
    } catch (e) { notify({ kind: "error", text: e.message }); }
    load();
  }
  const unseen = urgent.filter((c) => !c.seen).length;
  const icon = (I) => <I className="icon" aria-hidden="true" />;
  const tabs = [
    { id: "today", label: "Today", icon: icon(Bell), count: unseen + approvals.length + rebookings.length },
    { id: "appointments", label: "Appointments", icon: icon(ClipboardList) },
    { id: "book", label: "Book & register", icon: icon(CalendarPlus) },
    { id: "calendar", label: "Calendar", icon: icon(CalendarDays) },
    { id: "settings", label: "Clinic settings", icon: icon(Settings) },
    { id: "search", label: "Search debug", icon: icon(Search) },
  ];
  return (
    <>
      <div className="stats">
        <Stat icon={icon(Bell)} n={unseen} label="Unseen urgent cases" tone={unseen ? "alert" : "good"} />
        <Stat icon={icon(ClipboardList)} n={approvals.length} label="Late changes to approve" tone="warn" />
        <Stat icon={icon(CalendarClock)} n={rebookings.length} label="Rebookings to review" tone="accent" />
        <Stat icon={icon(CalendarDays)} n={todayCount} label="Booked today" />
      </div>
      <Tabs tabs={tabs} active={tab} onChange={setTab} label="Staff sections" />
      <Panel id="today" active={tab}>
        <UrgentBadge count={unseen} />
        <UrgentList cases={urgent} onSeen={seen} />
        {approvals.length > 0 && <ApprovalsPanel items={approvals} onDecide={decide} />}
        <RebookingsPanel items={rebookings} onDecide={decideRebooking} />
        {unseen + approvals.length + rebookings.length === 0 && (
          <Empty icon={<Check className="icon" aria-hidden="true" />} title="Nothing needs you right now" hint="Urgent cases, late changes and rebookings will appear here." />
        )}
      </Panel>
      <Panel id="appointments" active={tab}>
        <div className="card"><h2><ClipboardList className="icon" aria-hidden="true" />All appointments</h2>
          <div className="row">
            <label>Day<input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
            <label>Doctor
              <select value={doctorId} onChange={(e) => setDoctorId(e.target.value)}>
                <option value="">All doctors</option>
                {doctors.map((d) => <option key={d.doctor_id} value={d.doctor_id}>{d.name}</option>)}
              </select>
            </label>
            <button onClick={() => { setDate(""); setDoctorId(""); }}>Clear filters</button>
          </div>
          <ApptTable rows={rows} showDoctor showPatient onCancel={cancel} onReschedule={(a) => { setResched(a); setTab("book"); }} />
        </div>
      </Panel>
      <Panel id="book" active={tab}>
        <NewWalkIn onCreated={loadPatients} notify={notify} />
        <BookPanel doctors={doctors} today={today} user={user} patients={patients} reschedule={resched} notify={notify}
          onDone={() => { setResched(null); load(); }} onCancelReschedule={() => setResched(null)} />
      </Panel>
      <Panel id="calendar" active={tab}><WeekCalendar doctors={doctors} today={today} /></Panel>
      <Panel id="settings" active={tab}><ClinicSettings doctors={doctors} today={today} notify={notify} /></Panel>
      <Panel id="search" active={tab}><SearchDebug /></Panel>
    </>
  );
}

/* ---------- Doctor: note on completion (R2) and AI summary (R6) ---------- */
function SummarySections({ r }) {
  return (
    <div className="summary-sections">
      <section><h4>Today</h4><p>{r.today || "…"}</p></section>
      <section><h4>Background</h4><p>{r.background || "…"}</p></section>
      <section><h4>Past visits</h4>
        {r.past_visits.length === 0 ? <p className="empty">None written yet.</p> : (
          <ul className="summary-visits">
            {r.past_visits.map((v, i) => (
              <li key={i}><b className="mono">{v.date || "…"}</b> · {v.doctor}{v.speciality ? ` (${v.speciality})` : ""}: {v.note}</li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function PatientSummary({ appt, onClose }) {
  const [meta, setMeta] = useState(null);       // profile + the full past notes (arrive first)
  const [live, setLive] = useState(null);       // the summary so far, while it is being written
  const [final, setFinal] = useState(null);     // the checked summary (or the fixed fallback)
  const [err, setErr] = useState("");
  useEffect(() => {
    const ctl = new AbortController();
    setMeta(null); setLive(null); setFinal(null); setErr("");
    streamEvents(`/doctor/patients/${appt.patient_id}/summary/stream?appointment_id=${appt.appointment_id}`, (ev) => {
      if (ev.type === "meta") setMeta(ev);
      else if (ev.type === "partial") setLive(ev.structured);
      else if (ev.type === "final") setFinal(ev);
    }, ctl.signal).catch((e) => setErr(e.message));
    return () => ctl.abort();
  }, [appt.appointment_id, appt.patient_id]);

  const writing = meta && !final && !err;
  return (
    <div className="card summary">
      <div className="fact-head"><h2>{appt.patient_name}</h2><button onClick={onClose}>Close</button></div>
      {err && <p className="notice error">{err}</p>}
      {!meta && !err && <p className="hint">Opening the patient…</p>}
      {meta && (
        <>
          <p className="hint">{meta.patient.age != null ? `${meta.patient.age} years` : "Age not recorded"} · Health: {meta.patient.general_health || "not recorded"} · Allergies: {meta.patient.allergies || "not recorded"}</p>
          <p><b>Today’s reason:</b> {meta.today_reason}</p>
          <div className="ai-box" aria-busy={!!writing}>
            {writing && !live && <p className="hint">Writing the summary…</p>}
            {final ? (
              final.structured ? <SummarySections r={final.structured} /> : <p className="summary-text">{final.summary}</p>
            ) : (live && <SummarySections r={live} />)}
            {writing && live && <p className="hint">Still writing…</p>}
            <p className="hint caveat">{meta.label}</p>
          </div>
          <h3>Past notes ({meta.notes.length})</h3>
          {meta.notes.length === 0 && <p className="empty">No previous visits on record.</p>}
          {meta.notes.map((n) => (
            <div className="note" key={n.appointment_id}>
              <div className="note-head"><b>{n.date}</b> · {n.doctor_name} · {n.speciality} ({n.visit_type === "new" ? "new" : "follow-up"})</div>
              <div className="hint">Reason: {n.reason || "—"}</div>
              <div>{n.doctor_note}</div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

function LeavePanel({ today, notify }) {
  const [date, setDate] = useState(addDays(today, 1));
  const [session, setSession] = useState("FULL");
  const [leaves, setLeaves] = useState([]);
  const load = useCallback(() => api("/doctor/leave").then(setLeaves).catch(() => {}), []);
  useEffect(() => { load(); }, [load]);
  async function submit(e) {
    e.preventDefault();
    try {
      const r = await api("/doctor/leave", { method: "POST", body: { date, session } });
      notify({ kind: "ok", text: `Leave marked for ${date} (${session}). ${r.affected} appointment(s) were cancelled and sent to Staff for rebooking.` });
      load();
    } catch (e2) { notify({ kind: "error", text: e2.message }); }
  }
  return (
    <form className="card" onSubmit={submit}>
      <h2>Mark leave</h2>
      <div className="row">
        <label>Date<input type="date" value={date} min={today} max={addDays(today, 30)} onChange={(e) => setDate(e.target.value)} required /></label>
        <label>Session
          <select value={session} onChange={(e) => setSession(e.target.value)}>
            <option value="AM">Morning</option><option value="PM">Afternoon</option><option value="FULL">Full day</option>
          </select>
        </label>
        <button className="primary" type="submit">Mark leave</button>
      </div>
      <p className="hint">Affected appointments are cancelled and Staff rebook the patients. {leaves.length > 0 && "Your leave: " + leaves.map((l) => `${l.date} ${l.session}`).join(", ")}</p>
    </form>
  );
}

function DoctorView({ user, today, notify }) {
  const [date, setDate] = useState(today);
  const [rows, setRows] = useState([]);
  const [open, setOpen] = useState(null);
  const [completing, setCompleting] = useState(null);
  const [note, setNote] = useState("");
  const [tab, setTab] = useState("schedule");
  const load = useCallback(() => api(`/appointments?date=${date}`).then(setRows).catch((e) => notify({ kind: "error", text: e.message })), [date]); // eslint-disable-line
  useEffect(() => { load(); setOpen(null); setCompleting(null); }, [load]);

  async function complete(e) {
    e.preventDefault();
    try {
      await api(`/appointments/${completing.appointment_id}/complete`, { method: "POST", body: { note } });
      notify({ kind: "ok", text: `Marked ${completing.patient_name}'s ${completing.speciality} visit as completed.` });
      setCompleting(null); setNote("");
    } catch (e2) { notify({ kind: "error", text: e2.message }); }
    load();
  }
  const tabs = [
    { id: "schedule", label: "My schedule", icon: <ClipboardList className="icon" aria-hidden="true" /> },
    { id: "leave", label: "Leave", icon: <Plane className="icon" aria-hidden="true" /> },
    { id: "calendar", label: "Week view", icon: <CalendarDays className="icon" aria-hidden="true" /> },
  ];
  return (
    <>
      <Tabs tabs={tabs} active={tab} onChange={setTab} label="Doctor sections" />
      <Panel id="schedule" active={tab}>
        <div className="card"><h2><ClipboardList className="icon" aria-hidden="true" />My schedule</h2>
          <div className="row"><label>Day<input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label></div>
          <ApptTable rows={rows} showPatient onOpen={setOpen} onComplete={(a) => { setCompleting(a); setNote(""); }} />
        </div>
        {completing && (
          <form className="card" onSubmit={complete}>
            <h2><Check className="icon" aria-hidden="true" />Complete visit: {completing.patient_name}</h2>
            <label>Note for the record (required)
              <textarea value={note} rows={3} maxLength={1000} onChange={(e) => setNote(e.target.value)}
                placeholder="Findings, advice given, plan, review date…" />
            </label>
            <div className="row"><button className="primary" type="submit">Save and mark completed</button>
              <button type="button" onClick={() => setCompleting(null)}>Cancel</button></div>
          </form>
        )}
        {open && <PatientSummary appt={open} onClose={() => setOpen(null)} />}
      </Panel>
      <Panel id="leave" active={tab}><LeavePanel today={today} notify={notify} /></Panel>
      <Panel id="calendar" active={tab}><WeekCalendar doctors={[]} fixedDoctor today={today} /></Panel>
    </>
  );
}

/* ---------- App shell ---------- */
export default function App() {
  const [user, setUser] = useState(null);
  const [doctors, setDoctors] = useState([]);
  const [today, setToday] = useState(new Date().toISOString().slice(0, 10));
  const [notice, setNotice] = useState(null);
  const [booting, setBooting] = useState(true);
  const [view, setView] = useState("home"); // a logged-out visitor sees: home | login | symptom

  useEffect(() => {
    (async () => {
      try {
        setDoctors(await api("/doctors"));
        const h = await api("/health");
        setToday(h.clinic_now.slice(0, 10));
        if (hasToken()) setUser(await api("/me"));
      } catch (e) {
        if (e.code !== "NOT_LOGGED_IN") setNotice({ kind: "error", text: e.message });
        setToken("");
      }
      setBooting(false);
    })();
  }, []);

  const logout = () => { setToken(""); setUser(null); setNotice(null); setView("home"); };
  if (booting) return <main><p>Loading…</p></main>;

  if (!user && view === "home") return <Home doctors={doctors} onSymptom={() => setView("symptom")} onLogin={() => setView("login")} />;
  const initialsMe = user ? initials(user.name) : "";
  return (
    <>
      <a className="skip" href="#main">Skip to the content</a>
      <header className="top">
        <a className="logo" href="/" aria-label="NetraCare home" onClick={(e) => { if (!user) { e.preventDefault(); setView("home"); } }}>
          <LogoMark />
          <span className="brand-block"><span className="brand">NetraCare</span><span className="sub">Eye clinic · Mon–Sat 9 AM–6 PM</span></span>
        </a>
        <div className="top-right">
        
        {user && (
          <div className="who">
            <span className="avatar-me" aria-hidden="true">{initialsMe}</span>
            <span className="name">{user.name}</span><span className="rolepill">{user.role}</span>
            <button onClick={logout}><LogOut className="icon" aria-hidden="true" />Log out</button>
          </div>
        )}
        </div>
      </header>
      <main id="main">
        <div className="noticebar"><Notice notice={notice} onClose={() => setNotice(null)} /></div>
        {!user && view === "login" && <Login doctors={doctors} onBack={() => setView("home")} onLoggedIn={(u) => { setUser(u); setNotice(null); }} />}
        {!user && view === "symptom" && <StartWithSymptom onBack={() => setView("home")} onLoggedIn={(u) => { setUser(u); setNotice(null); }} />}
        {user?.role === "patient" && <PatientView user={user} doctors={doctors} today={today} notify={setNotice} />}
        {user?.role === "staff" && <StaffView user={user} doctors={doctors} today={today} notify={setNotice} />}
        {user?.role === "doctor" && <DoctorView user={user} today={today} notify={setNotice} />}
      </main>
      <footer>NetraCare is a fictional clinic. All data is synthetic.</footer>
    </>
  );
}
