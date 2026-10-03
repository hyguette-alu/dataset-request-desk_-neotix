import type {
  DatasetRequest,
  Episode,
  Page,
  Quality,
  RequestDetail,
  RequestStatus,
  User,
} from "./types";

/** An error carrying the server's status and message, so the UI can show
 *  what the API actually said rather than a generic failure. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    // The session is an HttpOnly cookie; there is no token in JS to attach.
    credentials: "same-origin",
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  if (response.status === 204) {
    return undefined as T;
  }

  const text = await response.text();
  const body = text ? JSON.parse(text) : null;

  if (!response.ok) {
    throw new ApiError(response.status, errorMessage(body, response.status));
  }
  return body as T;
}

function errorMessage(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    // FastAPI validation errors arrive as a list of field problems.
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          const loc = Array.isArray(item?.loc) ? item.loc.slice(1).join(".") : "";
          return loc ? `${loc}: ${item.msg}` : item.msg;
        })
        .join("; ");
    }
  }
  return `Request failed (${status})`;
}

function query(params: Record<string, string | number | boolean | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}

export const api = {
  login: (email: string, password: string) =>
    request<User>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  logout: () => request<void>("/api/auth/logout", { method: "POST" }),

  me: () => request<User>("/api/auth/me"),

  listRequests: (params: { status?: string } = {}) =>
    request<Page<DatasetRequest>>(`/api/requests${query({ ...params, limit: 200 })}`),

  getRequest: (id: number) => request<RequestDetail>(`/api/requests/${id}`),

  createRequest: (payload: {
    task_name: string;
    episodes_requested: number;
    deadline: string;
    notes?: string;
  }) =>
    request<DatasetRequest>("/api/requests", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  changeStatus: (id: number, to_status: RequestStatus, note?: string) =>
    request<DatasetRequest>(`/api/requests/${id}/status`, {
      method: "POST",
      body: JSON.stringify({ to_status, note: note || null }),
    }),

  listEpisodes: (params: {
    task_name?: string;
    quality?: Quality | "";
    unassigned_only?: boolean;
  }) => request<Page<Episode>>(`/api/episodes${query({ ...params, limit: 100 })}`),

  assignEpisodes: (id: number, episode_ids: string[]) =>
    request<DatasetRequest>(`/api/requests/${id}/assignments`, {
      method: "POST",
      body: JSON.stringify({ episode_ids }),
    }),

  unassignEpisode: (id: number, episodeId: string) =>
    request<void>(`/api/requests/${id}/assignments/${episodeId}`, {
      method: "DELETE",
    }),
};
