import { useEffect, useRef, useState } from "react";
import { API_BASE_URL, getHealth, newSessionId, sendChat } from "./api.js";

const SIMULATION_PRESETS = [
  { value: "", label: "Off" },
  { value: "key_1=429", label: "key_1 → 429" },
  { value: "key_1=500", label: "key_1 → 500" },
  { value: "key_1=503", label: "key_1 → 503" },
  { value: "*=429", label: "all keys → 429" },
];

let nextId = 1;
const uid = () => nextId++;

function HealthPill({ health }) {
  if (health === null) return <span className="pill pill-muted">checking backend…</span>;
  if (health.error) return <span className="pill pill-bad" title={health.error}>backend offline</span>;
  return (
    <span className="pill pill-good" title={API_BASE_URL}>
      online · {health.keys_configured} keys · cache {health.cache_enabled ? "on" : "off"}
    </span>
  );
}

function AssistantMessage({ msg }) {
  const m = msg.metadata;
  const cached = m.resolution_source === "LLM_CACHE";
  return (
    <div className="row row-assistant">
      <div className="bubble bubble-assistant" data-testid="assistant-message">
        <div className="reply">{msg.content}</div>
        <div className="meta">
          <span className={`chip ${cached ? "chip-cache" : "chip-live"}`} title="Resolution source">
            <span data-testid="meta-source">{m.resolution_source}</span>
          </span>
          <span className="chip" title="Model">
            <span data-testid="meta-model">{m.model}</span>
          </span>
          <span className="chip" title="Latency">
            ⏱ <span data-testid="meta-latency">{m.latency_ms} ms</span>
          </span>
          <span className="chip" title="Input tokens (this request)">
            in <span data-testid="meta-input-tokens">{m.input_tokens}</span>
          </span>
          <span className="chip" title="Output tokens (this reply)">
            out <span data-testid="meta-output-tokens">{m.output_tokens}</span>
          </span>
          <span className="chip chip-subtle" title="Key that answered">
            key <span data-testid="meta-key-id">{m.key_id ?? "—"}</span>
          </span>
          {m.fallback_attempts > 0 && (
            <span className="chip chip-warn" title="Keys that failed before this one">
              ↪ <span data-testid="meta-fallback-attempts">{m.fallback_attempts}</span> failover
              {m.fallback_attempts > 1 ? "s" : ""}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [pending, setPending] = useState(false);
  const [health, setHealth] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch((e) => setHealth({ error: e.message }));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, pending]);

  async function send(text) {
    const message = (text ?? input).trim();
    if (!message || pending) return;
    setInput("");
    setMessages((prev) => [...prev, { id: uid(), role: "user", content: message }]);
    setPending(true);
    try {
      const res = await sendChat({ sessionId, message, systemPrompt, simulateFailure: simulate });
      setMessages((prev) => [
        ...prev,
        { id: uid(), role: "assistant", content: res.reply, metadata: res.metadata },
      ]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { id: uid(), role: "error", content: err.message, status: err.status, retryText: message },
      ]);
    } finally {
      setPending(false);
      inputRef.current?.focus();
    }
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  }

  function newChat() {
    setSessionId(newSessionId());
    setMessages([]);
    setInput("");
    inputRef.current?.focus();
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span>
          <div>
            <h1>Lyftr Chat</h1>
            <div className="session" title="Session id (new on every page load)">
              session <code>{sessionId}</code>
            </div>
          </div>
        </div>
        <div className="topbar-actions">
          <HealthPill health={health} />
          <button className="btn btn-ghost" onClick={() => setShowSettings((s) => !s)}>
            {showSettings ? "Hide settings" : "Settings"}
          </button>
          <button className="btn btn-ghost" onClick={newChat}>
            New chat
          </button>
        </div>
      </header>

      {showSettings && (
        <section className="settings">
          <label className="field">
            <span>System prompt (this session)</span>
            <textarea
              data-testid="system-prompt-input"
              rows={2}
              placeholder="Default: You are a helpful, concise assistant."
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
            />
          </label>
          <label className="field field-narrow">
            <span>Simulate failure (X-Simulate-Failure)</span>
            <select value={simulate} onChange={(e) => setSimulate(e.target.value)}>
              {SIMULATION_PRESETS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
            <small>Applies to each message sent while set. Needs ENABLE_FAILURE_SIMULATION=true.</small>
          </label>
        </section>
      )}

      <main className="chat">
        {messages.length === 0 && (
          <div className="empty">
            <h2>Ask anything</h2>
            <p>
              Every reply shows where it came from (<b>LIVE_INFERENCE</b> or <b>LLM_CACHE</b>), the
              model, latency and the tokens of that request.
            </p>
          </div>
        )}

        {messages.map((msg) => {
          if (msg.role === "user") {
            return (
              <div key={msg.id} className="row row-user">
                <div className="bubble bubble-user" data-testid="user-message">
                  {msg.content}
                </div>
              </div>
            );
          }
          if (msg.role === "assistant") return <AssistantMessage key={msg.id} msg={msg} />;
          return (
            <div key={msg.id} className="row row-assistant">
              <div className="bubble bubble-error" data-testid="error-message" role="alert">
                <strong>
                  {msg.status === 503
                    ? "All providers are unavailable"
                    : msg.status === 422
                      ? "That message was not accepted"
                      : "Something went wrong"}
                </strong>
                <div>
                  {msg.status === 503
                    ? "Every API key in the pool failed (rate limit, quota or server error). Please try again."
                    : msg.content}
                </div>
                {msg.status !== 422 && (
                  <button className="btn btn-link" onClick={() => send(msg.retryText)} disabled={pending}>
                    Retry
                  </button>
                )}
              </div>
            </div>
          );
        })}

        {pending && (
          <div className="row row-assistant">
            <div className="bubble bubble-assistant typing" aria-label="Assistant is typing">
              <span />
              <span />
              <span />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </main>

      <footer className="composer">
        {simulate && <div className="sim-banner">Simulating failure: {simulate}</div>}
        <div className="composer-row">
          <textarea
            ref={inputRef}
            data-testid="chat-input"
            rows={1}
            placeholder="Type a message… (Enter to send, Shift+Enter for a new line)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            autoFocus
          />
          <button
            className="btn btn-primary"
            data-testid="send-button"
            onClick={() => send()}
            disabled={pending || !input.trim()}
          >
            {pending ? "Sending…" : "Send"}
          </button>
        </div>
        <div className={`counter ${input.trim().length > 4000 ? "counter-over" : ""}`}>{input.trim().length}/4000</div>
      </footer>
    </div>
  );
}
