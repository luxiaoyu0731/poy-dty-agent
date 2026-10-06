import { defineConfig, type IndexHtmlTransformContext, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const proxyTarget = process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:8000";

// The workbench page is the only route and is dynamically imported, so the
// browser otherwise discovers its chunk one serial round trip after the entry
// executes. Preloading it with the entry's own static deps removes that
// waterfall without merging the chunks.
function preloadWorkbenchPageChunk(): Plugin {
  return {
    name: "preload-workbench-page-chunk",
    transformIndexHtml: {
      order: "post",
      handler(html, ctx: IndexHtmlTransformContext) {
        const bundle = ctx.bundle;
        if (!bundle) return html;
        // Preload the workbench page chunk plus its whole static import
        // closure: without this the browser discovers antd-icons / reactflow /
        // charts / types only after the page chunk arrives, adding serial
        // round trips before first paint. The intelligence map (maplibre,
        // 1MB+) and EventDetailPanel stay lazy on purpose — they are not on
        // the first-screen path.
        const chunks = Object.values(bundle).filter((item) => item.type === "chunk");
        const byFile = new Map(chunks.map((item) => [item.fileName.split("/").pop() ?? "", item] as const));
        const pageChunk = chunks.find(
          (item) => item.isEntry === false && item.name?.includes("AgentWorkbenchPage")
        );
        if (!pageChunk) return html;
        const closure = new Set<string>([pageChunk.fileName]);
        let grew = true;
        while (grew) {
          grew = false;
          for (const fileName of [...closure]) {
            const base = fileName.split("/").pop() ?? "";
            const item = byFile.get(base);
            if (!item) continue;
            const code = item.type === "chunk" ? item.code : "";
            for (const dep of code.matchAll(/(?:from|import)\s*"\.\/([^"]+\.js)"/g)) {
              const depBase = dep[1].split("/").pop() ?? "";
              const depItem = byFile.get(depBase);
              if (depItem && !closure.has(depItem.fileName)) {
                closure.add(depItem.fileName);
                grew = true;
              }
            }
          }
        }
        // Vite already emits modulepreload for the entry's own static deps
        // (runtime/antd/react); only add tags it did not emit.
        const existing = new Set(
          [...html.matchAll(/modulepreload[^>]*href="\/([^"]+)"/g)].map((match) => match[1])
        );
        return {
          html,
          tags: [...closure]
            .filter((fileName) => !existing.has(fileName))
            .map((fileName) => ({
              tag: "link",
              attrs: { rel: "modulepreload", crossorigin: true, href: `/${fileName}` },
              injectTo: "head" as const
            }))
        };
      }
    }
  };
}

export default defineConfig({
  plugins: [react(), preloadWorkbenchPageChunk()],
  build: {
    chunkSizeWarningLimit: 600,
    rolldownOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules/@ant-design/icons")) return "antd-icons";
          if (id.includes("node_modules/rc-") || id.includes("node_modules/@rc-component")) return "antd-rc";
          if (id.includes("node_modules/antd") || id.includes("node_modules/@ant-design")) return "antd";
          if (id.includes("node_modules/@xyflow")) return "reactflow";
          if (id.includes("node_modules/recharts")) return "charts";
          if (id.includes("node_modules/lucide-react")) return "icons";
          // Do not pull react-map-gl (and its MapLibre imports) into the eager React chunk.
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return "react";
        }
      }
    }
  },
  server: {
    port: 5173,
    strictPort: true,
    allowedHosts: ["app.kaipingrc.com", "kaipingrc.com"],
    proxy: {
      "/api": proxyTarget,
      "/metrics": proxyTarget
    }
  }
});
