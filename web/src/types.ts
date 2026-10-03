export type Role = "admin" | "operator" | "client";

export type RequestStatus =
  | "submitted"
  | "in_progress"
  | "delivered"
  | "accepted"
  | "rejected";

export type Quality = "good" | "usable" | "bad";

export interface User {
  id: number;
  email: string;
  name: string;
  organisation: string | null;
  role: Role;
  is_active: boolean;
}

export interface Episode {
  episode_id: string;
  robot_id: string;
  task_name: string;
  recorded_at: string;
  duration_seconds: number;
  operator_name: string | null;
  quality: Quality;
}

export interface StatusEvent {
  from_status: RequestStatus | null;
  to_status: RequestStatus;
  actor_id: number;
  actor_name: string;
  note: string | null;
  created_at: string;
}

export interface DatasetRequest {
  id: number;
  task_name: string;
  episodes_requested: number;
  deadline: string;
  notes: string | null;
  status: RequestStatus;
  created_at: string;
  updated_at: string;
  client: { id: number; name: string; organisation: string | null };
  assigned_count: number;
  /** What this user may do next. The server checks again on every call. */
  allowed_transitions: RequestStatus[];
}

export interface RequestDetail extends DatasetRequest {
  episodes: Episode[];
  history: StatusEvent[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
