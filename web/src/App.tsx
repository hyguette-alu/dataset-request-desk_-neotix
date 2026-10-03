import { useEffect, useState } from "react";
import { ApiError, api } from "./api";
import ClientView from "./ClientView";
import OperatorView from "./OperatorView";
import type { User } from "./types";

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  // The session lives in an HttpOnly cookie, so the only way to find out
  // whether we are logged in is to ask the server.
  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <main>Loading…</main>;
  if (!user) return <Login onLogin={setUser} />;

  return (
    <>
      <header className="app-header">
        <h1>Dataset Request Desk</h1>
        <div className="row" style={{ alignItems: "center" }}>
          <span className="muted">
            {user.name} · <span className="badge">{user.role}</span>
          </span>
          <button
            onClick={async () => {
              await api.logout();
              setUser(null);
            }}
          >
            Log out
          </button>
        </div>
      </header>
      {user.role === "client" ? (
        <ClientView user={user} />
      ) : (
        <OperatorView user={user} />
      )}
    </>
  );
}

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onLogin(await api.login(email, password));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not log in");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="login card" onSubmit={submit}>
      <h2>Sign in</h2>
      {error && <div className="error">{error}</div>}
      <label>
        Email
        <input
          type="email"
          value={email}
          autoComplete="username"
          required
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          required
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <button className="primary" type="submit" disabled={busy}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
