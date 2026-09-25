import { useSearchParams } from "react-router-dom";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { GitHubTab } from "./GitHubTab";
import { ModelsTab } from "./ModelsTab";
import { ProjectsTab } from "./ProjectsTab";

const tabs = ["projects", "models", "github"] as const;
type Tab = (typeof tabs)[number];
const tabLabels: Record<Tab, string> = { projects: "Projects", models: "Models", github: "GitHub" };

export function SettingsPage() {
  // The active tab lives in the URL so GitHub's connect callback can land on its tab.
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab") as Tab | null;
  const tab: Tab = requested && tabs.includes(requested) ? requested : "projects";

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          SRE projects, the models they use, and your GitHub connection.
        </p>
      </div>
      <Tabs value={tab} onValueChange={(value) => setParams({ tab: value })}>
        <TabsList>
          {tabs.map((t) => (
            <TabsTrigger key={t} value={t}>
              {tabLabels[t]}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="projects">
          <ProjectsTab />
        </TabsContent>
        <TabsContent value="models">
          <ModelsTab />
        </TabsContent>
        <TabsContent value="github">
          <GitHubTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
