import { useState } from "react";
import Brand from "./Brand.jsx";
import Icon from "./Icon.jsx";
import { regionMarks } from "./labels.js";

export default function Login({ accounts, onLogin, busy, error, accessRequired }) {
  const [accessCode, setAccessCode] = useState("");
  return (
    <main className="login-page">
      <div className="login-top">
        <Brand />
        <span className="badge">Demo workspace</span>
      </div>
      <div className="login-layout">
        <section className="login-intro">
          <span className="eyebrow">EVENT REVIEW</span>
          <h1>Review safety events.</h1>
          <p>
            Check verdicts, answer questions, and record corrections.
          </p>
          <div className="intro-line">
            <span />
            <span />
            <span />
          </div>
        </section>
        <section className="login-card" aria-labelledby="login-title">
          <h2 id="login-title">Choose a region</h2>
          {accessRequired && <div className="preview-access">
            <label htmlFor="preview-code">Preview access code</label>
            <input id="preview-code" type="password" autoComplete="current-password" value={accessCode} maxLength={128}
              onChange={(event) => setAccessCode(event.target.value)} disabled={busy} />
          </div>}
          <div className="account-list">
            {accounts.map((account) => (
              <button
                key={account.id}
                className="account-button"
                onClick={() => onLogin(account.id, accessCode)}
                disabled={busy || (accessRequired && !accessCode)}
                aria-label={`Sign in as ${account.name}`}
              >
                <span className="region-mark">
                  {regionMarks[account.region]}
                </span>
                <span>
                  <strong>{account.region_label}</strong>
                  <small>{account.name}</small>
                </span>
                <Icon name="arrow" />
              </button>
            ))}
          </div>
          {error && (
            <p role="alert" className="error-text">
              {error}
            </p>
          )}
          <div className="demo-note">
            <Icon name="shield" />
            <span>Demo accounts · Sample events</span>
          </div>
        </section>
      </div>
      <footer className="login-footer">
        Event Review
      </footer>
    </main>
  );
}
