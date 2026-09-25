import type { components } from "./schema";

type Schemas = components["schemas"];

export type User = Schemas["UserOut"];
export type LoginRequest = Schemas["LoginIn"];
export type RegisterRequest = Schemas["RegisterIn"];
export type AuthResponse = Schemas["TokenOut"];
export type AuthProviders = Schemas["ProvidersOut"];
export type Item = Schemas["ItemOut"];
export type ItemListResponse = Schemas["ItemListOut"];
export type ItemCreateRequest = Schemas["ItemCreateIn"];
export type ItemUpdateRequest = Schemas["ItemUpdateIn"];
export type OkResponse = Schemas["OkOut"];

export interface HealthResponse {
  status: string;
  version: string;
}

export type IncidentRun = Schemas["IncidentRunOut"];
export type IncidentRunList = Schemas["IncidentRunListOut"];
export type IncidentStatus =
  | "running"
  | "no_anomaly"
  | "new_playbook_created"
  | "awaiting_approval"
  | "succeeded"
  | "failed"
  | "advisory_complete";
export type Playbook = Schemas["PlaybookOut"];
export type PlaybookRun = Schemas["PlaybookRunOut"];
