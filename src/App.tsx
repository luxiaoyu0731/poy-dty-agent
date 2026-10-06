import { ContentLoading } from "./components/ContentLoading";
import { lazy, Suspense } from "react";

const AgentWorkbenchPage = lazy(() => import("./pages/AgentWorkbenchPage").then((module) => ({
  default: module.AgentWorkbenchPage
})));

export default function App() {
  return (
    <Suspense fallback={<div className="app-boot-loading"><ContentLoading title="正在打开研判工作台" /></div>}>
      <AgentWorkbenchPage />
    </Suspense>
  );
}
