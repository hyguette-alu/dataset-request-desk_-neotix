import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "./api";
import { ErrorBanner, StatusBadge, formatDate, formatDateTime } from "./common";
import type { DatasetRequest, RequestDetail, User } from "./types";

export default function ClientView({ user }: { user: User }) {
  const [requests, setRequests] = useState<DatasetRequest[]>([]);
  const [openId, setOpenId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setRequests((await api.listRequests()).items);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load requests");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  return (
    <main>
      <ErrorBanner error={error} />
      <NewRequestForm onCreated={reload} />

      <section className="card">
        <h2>My requests</h2>
        {requests.length === 0 ? (
          <p className="muted">
            No requests yet. Create one above and operations will pick it up.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Task</th>
                <th>Progress</th>
                <th>Deadline</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {requests.map((request) => (
                <tr key={request.id}>
                  <td>{request.id}</td>
                  <td>{request.task_name}</td>
                  <td>
                    {request.assigned_count} / {request.episodes_requested}
                  </td>
                  <td>{formatDate(request.deadline)}</td>
                  <td>
                    <StatusBadge status={request.status} />
                  </td>
                  <td>
                    <div className="actions">
                      {/* Only rendered when the server says this user may do
                          it; the server checks again when it is clicked. */}
                      {request.allowed_transitions.includes("accepted") && (
                        <ReviewButtons
                          requestId={request.id}
                          onDone={reload}
                          onError={setError}
                        />
                      )}
                      <button
                        onClick={() =>
                          setOpenId(openId === request.id ? null : request.id)
                        }
                      >
                        {openId === request.id ? "Hide" : "Details"}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {openId !== null && <RequestDetailCard requestId={openId} />}
      <p className="muted">Signed in as {user.email}</p>
    </main>
  );
}

function ReviewButtons({
  requestId,
  onDone,
  onError,
}: {
  requestId: number;
  onDone: () => void;
  onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);

  async function review(accept: boolean) {
    const note = accept
      ? undefined
      : (window.prompt("Why are you rejecting this delivery?") ?? undefined);
    setBusy(true);
    try {
      await api.changeStatus(requestId, accept ? "accepted" : "rejected", note);
      onDone();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "Could not update the request");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button className="primary" disabled={busy} onClick={() => void review(true)}>
        Accept
      </button>
      <button className="danger" disabled={busy} onClick={() => void review(false)}>
        Reject
      </button>
    </>
  );
}

function NewRequestForm({ onCreated }: { onCreated: () => void }) {
  const [taskName, setTaskName] = useState("");
  const [count, setCount] = useState(10);
  const [deadline, setDeadline] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.createRequest({
        task_name: taskName,
        episodes_requested: count,
        deadline,
        notes: notes || undefined,
      });
      setTaskName("");
      setCount(10);
      setDeadline("");
      setNotes("");
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the request");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <h2>New request</h2>
      <ErrorBanner error={error} />
      <div className="inline-form">
        <label>
          Task
          <input
            value={taskName}
            required
            placeholder="pick cup"
            onChange={(e) => setTaskName(e.target.value)}
          />
        </label>
        <label>
          Episodes needed
          <input
            type="number"
            min={1}
            value={count}
            required
            onChange={(e) => setCount(Number(e.target.value))}
          />
        </label>
        <label>
          Deadline
          <input
            type="date"
            value={deadline}
            required
            onChange={(e) => setDeadline(e.target.value)}
          />
        </label>
        <label>
          Notes
          <input
            value={notes}
            placeholder="optional"
            onChange={(e) => setNotes(e.target.value)}
          />
        </label>
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Submitting…" : "Submit request"}
        </button>
      </div>
    </form>
  );
}

export function RequestDetailCard({ requestId }: { requestId: number }) {
  const [detail, setDetail] = useState<RequestDetail | null>(null);

  useEffect(() => {
    void api.getRequest(requestId).then(setDetail).catch(() => setDetail(null));
  }, [requestId]);

  if (!detail) return null;

  return (
    <section className="card">
      <h2>
        Request #{detail.id} — {detail.task_name}
      </h2>
      {detail.notes && <p className="muted">{detail.notes}</p>}

      <h3 style={{ fontSize: "0.9rem" }}>
        Episodes ({detail.episodes.length}/{detail.episodes_requested})
      </h3>
      {detail.episodes.length === 0 ? (
        <p className="muted">Nothing assigned yet.</p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>Episode</th>
                <th>Robot</th>
                <th>Task</th>
                <th>Recorded</th>
                <th>Quality</th>
              </tr>
            </thead>
            <tbody>
              {detail.episodes.map((episode) => (
                <tr key={episode.episode_id}>
                  <td>{episode.episode_id}</td>
                  <td>{episode.robot_id}</td>
                  <td>{episode.task_name}</td>
                  <td>{formatDate(episode.recorded_at)}</td>
                  <td>
                    <span className={`badge ${episode.quality}`}>{episode.quality}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h3 style={{ fontSize: "0.9rem" }}>History</h3>
      <table>
        <tbody>
          {detail.history.map((event, index) => (
            <tr key={index}>
              <td>{formatDateTime(event.created_at)}</td>
              <td>
                {event.from_status ? `${event.from_status} → ` : ""}
                <StatusBadge status={event.to_status} />
              </td>
              <td>{event.actor_name}</td>
              <td className="muted">{event.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
