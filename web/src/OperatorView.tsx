import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "./api";
import { ErrorBanner, STATUS_LABELS, StatusBadge, formatDate, formatDateTime } from "./common";
import type { DatasetRequest, Episode, Quality, RequestDetail, User } from "./types";

export default function OperatorView({ user }: { user: User }) {
  const [requests, setRequests] = useState<DatasetRequest[]>([]);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [openId, setOpenId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setRequests((await api.listRequests({ status: statusFilter })).items);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load requests");
    }
  }, [statusFilter]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return (
    <main>
      <ErrorBanner error={error} />

      <section className="card">
        <h2>All requests</h2>
        <div className="row" style={{ marginBottom: "0.9rem" }}>
          <label>
            Status
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
            >
              <option value="">All</option>
              {Object.entries(STATUS_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <button onClick={() => void reload()}>Refresh</button>
        </div>

        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Client</th>
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
                <td>{request.client.organisation ?? request.client.name}</td>
                <td>{request.task_name}</td>
                <td>
                  {request.assigned_count} / {request.episodes_requested}
                </td>
                <td>{formatDate(request.deadline)}</td>
                <td>
                  <StatusBadge status={request.status} />
                </td>
                <td>
                  <button
                    onClick={() => setOpenId(openId === request.id ? null : request.id)}
                  >
                    {openId === request.id ? "Close" : "Open"}
                  </button>
                </td>
              </tr>
            ))}
            {requests.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No requests match this filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {openId !== null && (
        <OperatorRequestPanel key={openId} requestId={openId} onChanged={reload} />
      )}
      <p className="muted">Signed in as {user.email}</p>
    </main>
  );
}

function OperatorRequestPanel({
  requestId,
  onChanged,
}: {
  requestId: number;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<RequestDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setDetail(await api.getRequest(requestId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load the request");
    }
  }, [requestId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function act<T>(action: () => Promise<T>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await reload();
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  if (!detail) return null;

  const shortfall = detail.episodes_requested - detail.assigned_count;

  return (
    <section className="card">
      <h2>
        Request #{detail.id} — {detail.task_name}{" "}
        <StatusBadge status={detail.status} />
      </h2>
      <p className="muted">
        {detail.client.organisation ?? detail.client.name} · needs{" "}
        {detail.episodes_requested} episodes by {formatDate(detail.deadline)}
        {detail.notes ? ` · ${detail.notes}` : ""}
      </p>
      <ErrorBanner error={error} />

      <div className="actions" style={{ margin: "0.8rem 0" }}>
        {detail.allowed_transitions.map((target) => (
          <button
            key={target}
            className="primary"
            disabled={busy}
            onClick={() => void act(() => api.changeStatus(detail.id, target))}
          >
            Move to {STATUS_LABELS[target]}
          </button>
        ))}
        {detail.allowed_transitions.length === 0 && (
          <span className="muted">
            Nothing for you to do here — this request is waiting on the client.
          </span>
        )}
      </div>
      {shortfall > 0 && (
        <p className="muted">
          {shortfall} more episode{shortfall === 1 ? "" : "s"} needed before this
          can be delivered.
        </p>
      )}

      <h3 style={{ fontSize: "0.9rem" }}>Assigned episodes</h3>
      {detail.episodes.length === 0 ? (
        <p className="muted">Nothing assigned yet.</p>
      ) : (
        <div className="scroll">
          <table>
            <tbody>
              {detail.episodes.map((episode) => (
                <tr key={episode.episode_id}>
                  <td>{episode.episode_id}</td>
                  <td>{episode.robot_id}</td>
                  <td>{episode.task_name}</td>
                  <td>{formatDate(episode.recorded_at)}</td>
                  <td>
                    <span className={`badge ${episode.quality}`}>
                      {episode.quality}
                    </span>
                  </td>
                  <td>
                    <button
                      className="danger"
                      disabled={busy}
                      onClick={() =>
                        void act(() =>
                          api.unassignEpisode(detail.id, episode.episode_id),
                        )
                      }
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <EpisodePicker
        defaultTask={detail.task_name}
        disabled={busy}
        onAssign={(ids) => act(() => api.assignEpisodes(detail.id, ids))}
      />

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

function EpisodePicker({
  defaultTask,
  disabled,
  onAssign,
}: {
  defaultTask: string;
  disabled: boolean;
  onAssign: (episodeIds: string[]) => Promise<void>;
}) {
  const [taskName, setTaskName] = useState(defaultTask);
  const [quality, setQuality] = useState<Quality | "">("");
  const [unassignedOnly, setUnassignedOnly] = useState(true);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const search = useCallback(async () => {
    try {
      const page = await api.listEpisodes({
        task_name: taskName,
        quality,
        unassigned_only: unassignedOnly,
      });
      setEpisodes(page.items);
      setSelected(new Set());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not search episodes");
    }
  }, [taskName, quality, unassignedOnly]);

  useEffect(() => {
    void search();
    // Only on mount: afterwards the operator drives it with the Search button,
    // so typing in the filter does not fire a request per keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function toggle(episodeId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(episodeId)) next.delete(episodeId);
      else next.add(episodeId);
      return next;
    });
  }

  return (
    <>
      <h3 style={{ fontSize: "0.9rem" }}>Assign more episodes</h3>
      <ErrorBanner error={error} />
      <div className="row" style={{ marginBottom: "0.7rem" }}>
        <label>
          Task name
          <input value={taskName} onChange={(e) => setTaskName(e.target.value)} />
        </label>
        <label>
          Quality
          <select
            value={quality}
            onChange={(e) => setQuality(e.target.value as Quality | "")}
          >
            <option value="">Any assignable</option>
            <option value="good">good</option>
            <option value="usable">usable</option>
          </select>
        </label>
        <label style={{ flexDirection: "row", alignItems: "center", gap: "0.4rem" }}>
          <input
            type="checkbox"
            checked={unassignedOnly}
            onChange={(e) => setUnassignedOnly(e.target.checked)}
          />
          Unassigned only
        </label>
        <button onClick={() => void search()}>Search</button>
        <button
          className="primary"
          disabled={disabled || selected.size === 0}
          onClick={() => void onAssign([...selected]).then(search)}
        >
          Assign {selected.size > 0 ? `${selected.size} selected` : ""}
        </button>
      </div>

      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th />
              <th>Episode</th>
              <th>Robot</th>
              <th>Task</th>
              <th>Recorded</th>
              <th>Seconds</th>
              <th>Quality</th>
            </tr>
          </thead>
          <tbody>
            {episodes.map((episode) => (
              <tr key={episode.episode_id}>
                <td>
                  <input
                    type="checkbox"
                    checked={selected.has(episode.episode_id)}
                    // `bad` episodes cannot be assigned; the server refuses
                    // them too, this just avoids an obvious wasted call.
                    disabled={episode.quality === "bad"}
                    onChange={() => toggle(episode.episode_id)}
                  />
                </td>
                <td>{episode.episode_id}</td>
                <td>{episode.robot_id}</td>
                <td>{episode.task_name}</td>
                <td>{formatDate(episode.recorded_at)}</td>
                <td>{episode.duration_seconds}</td>
                <td>
                  <span className={`badge ${episode.quality}`}>{episode.quality}</span>
                </td>
              </tr>
            ))}
            {episodes.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  No episodes match these filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
