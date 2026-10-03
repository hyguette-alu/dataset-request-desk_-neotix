import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "./api";
import { ErrorBanner } from "./common";
import type { Role, User } from "./types";

const ROLES: Role[] = ["client", "operator", "admin"];

/** Admin-only panel: create accounts, change roles, deactivate and restore.
 *
 *  Accounts are never deleted — they are referenced by requests and by the
 *  status-change audit trail. Deactivating takes effect on the user's very
 *  next request, not when their token expires.
 */
export default function AdminUsers({ currentUser }: { currentUser: User }) {
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setUsers(await api.listUsers());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load users");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function act<T>(action: () => Promise<T>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h2>Users</h2>
      <ErrorBanner error={error} />

      <NewUserForm disabled={busy} onCreate={(p) => act(() => api.createUser(p))} />

      <table>
        <thead>
          <tr>
            <th>Email</th>
            <th>Name</th>
            <th>Organisation</th>
            <th>Role</th>
            <th>Status</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {users.map((user) => {
            // The server refuses these too; disabling them avoids an error
            // the admin can do nothing about.
            const isSelf = user.id === currentUser.id;
            return (
              <tr key={user.id}>
                <td>
                  {user.email}
                  {isSelf && <span className="muted"> (you)</span>}
                </td>
                <td>{user.name}</td>
                <td>{user.organisation ?? "—"}</td>
                <td>
                  <select
                    value={user.role}
                    disabled={busy || isSelf}
                    onChange={(e) =>
                      void act(() =>
                        api.updateUser(user.id, { role: e.target.value as Role }),
                      )
                    }
                  >
                    {ROLES.map((role) => (
                      <option key={role} value={role}>
                        {role}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <span className={`badge ${user.is_active ? "accepted" : "rejected"}`}>
                    {user.is_active ? "active" : "deactivated"}
                  </span>
                </td>
                <td>
                  <button
                    className={user.is_active ? "danger" : ""}
                    disabled={busy || isSelf}
                    onClick={() =>
                      void act(() =>
                        api.updateUser(user.id, { is_active: !user.is_active }),
                      )
                    }
                  >
                    {user.is_active ? "Deactivate" : "Reactivate"}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted">
        You cannot change your own role or deactivate yourself, and the last
        active admin cannot be removed.
      </p>
    </section>
  );
}

function NewUserForm({
  disabled,
  onCreate,
}: {
  disabled: boolean;
  onCreate: (payload: {
    email: string;
    name: string;
    role: Role;
    password: string;
    organisation?: string;
  }) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("client");
  const [password, setPassword] = useState("");
  const [organisation, setOrganisation] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    await onCreate({
      email,
      name,
      role,
      password,
      organisation: organisation || undefined,
    });
    setEmail("");
    setName("");
    setPassword("");
    setOrganisation("");
    setRole("client");
  }

  return (
    <form className="inline-form" style={{ marginBottom: "1rem" }} onSubmit={submit}>
      <label>
        Email
        <input
          type="email"
          value={email}
          required
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        Name
        <input value={name} required onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        Role
        <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </label>
      <label>
        Organisation
        <input
          value={organisation}
          placeholder="clients only"
          onChange={(e) => setOrganisation(e.target.value)}
        />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          required
          minLength={8}
          autoComplete="new-password"
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <button className="primary" type="submit" disabled={disabled}>
        Create user
      </button>
    </form>
  );
}
