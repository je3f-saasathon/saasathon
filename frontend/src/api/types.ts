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

export type Project = Schemas["ProjectOut"];
export type ProjectCreated = Schemas["ProjectCreatedOut"];
export type ProjectCreateRequest = Schemas["ProjectCreateIn"];
export type ProjectUpdateRequest = Schemas["ProjectUpdateIn"];
export type WebhookSecret = Schemas["WebhookSecretOut"];
export type ExecutionMode = Schemas["ExecutionMode"];
export type LLMConfig = Schemas["LLMConfigOut"];
export type LLMConfigRequest = Schemas["LLMConfigIn"];
export type LLMConfigUpdateRequest = Schemas["LLMConfigUpdateIn"];
export type LLMProvider = Schemas["LLMProvider"];
export type PipelineStep = Schemas["PipelineStep"];
export type StepOverride = Schemas["StepOverrideOut"];
export type GitHubStatus = Schemas["GitHubStatusOut"];
export type GitHubConnect = Schemas["GitHubConnectOut"];
export type GitHubInstallation = Schemas["GitHubInstallationOut"];
export type GitHubRepo = Schemas["GitHubRepoOut"];
