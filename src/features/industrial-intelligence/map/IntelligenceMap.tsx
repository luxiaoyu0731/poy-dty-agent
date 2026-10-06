import { useEffect, useState } from "react";
import { Alert, Button, Empty, Spin, Typography } from "antd";
import * as maplibregl from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import "maplibre-gl/dist/maplibre-gl.css";
import Map, { AttributionControl, Layer, Source, NavigationControl } from "react-map-gl/maplibre";
import * as intelligenceApi from "../api";
import { categoryLabel } from "../components/EventDetailPanel";

const { Text } = Typography;

// A same-origin worker keeps the map compatible with worker-src 'self'.
maplibregl.setWorkerUrl(workerUrl);

/**
 * Lazy map subview. This module (and MapLibre itself) is imported only when
 * the operator first opens the map tab; the daily summary and radar never
 * load this chunk or the same-origin GeoJSON assets.
 *
 * Basemap: same-origin vendored Natural Earth v5.1.2 (public domain). No
 * third-party tiles, glyphs, sprites, or API keys. Empty `layers`/`sources`
 * style keeps the renderer happy without remote style documents.
 */

const COUNTRIES_GEOJSON = "/geo/ne_110m_admin_0_countries.v5.1.2.geojson";

const EMPTY_STYLE: import("maplibre-gl").StyleSpecification = {
  version: 8,
  sources: {},
  layers: [
    {
      id: "background",
      type: "background",
      paint: { "background-color": "#101828" },
    },
  ],
};

export function IntelligenceMap(props: {
  onSelectEvent: (eventId: string) => void;
}): JSX.Element {
  const [collection, setCollection] = useState<intelligenceApi.MapFeatureCollection | null>(null);
  const windowHours = collection?.applied_filters.window_hours ?? "48";
  const [error, setError] = useState<string | null>(null);
  const [basemapReady, setBasemapReady] = useState(false);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [slowBasemap, setSlowBasemap] = useState(false);
  const [mapGeneration, setMapGeneration] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  useEffect(() => { const timer = window.setInterval(() => setAttempt(value => value + 1), 60_000); return () => window.clearInterval(timer); }, []);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    intelligenceApi
      .getMap("-180,-85,180,85", 12, controller.signal)
      .then((value) => {
        // 归一化：applied_filters 缺失（网关截断/异常响应）时兜底为空对象，
        // 避免下方读取 window_hours/truncated 时把整个地图视图打崩。
        if (!controller.signal.aborted) setCollection({ ...value, applied_filters: value.applied_filters ?? {} });
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "地图数据加载失败");
      });
    return () => {
      controller.abort();
    };
  }, [attempt]);

  useEffect(() => {
    if (!collection || basemapReady || renderError) return;
    const timer = window.setTimeout(() => setSlowBasemap(true), 15_000);
    return () => window.clearTimeout(timer);
  }, [Boolean(collection), basemapReady, renderError, attempt]);

  const retry = () => {
    setBasemapReady(false);
    setRenderError(null);
    setSlowBasemap(false);
    setMapGeneration(value => value + 1);
    setAttempt((value) => value + 1);
  };

  return (
    <div className="review-map-layout" data-testid="intelligence-map" data-basemap-ready={basemapReady}>
      {error || renderError ? <Alert type="warning" showIcon message="地图暂不可用"
        description={error || `地图渲染失败：${renderError}。可重试，或在全球雷达查看事件文字和来源。`}
        action={<Button onClick={retry}>重试地图</Button>} /> : null}
      {!collection && !error ? (
        <Spin aria-label="正在加载地图数据" />
      ) : collection ? (
        <>
          {!renderError ? (
          <div className="review-map-canvas"><Map
            key={mapGeneration}
            mapLib={maplibregl}
            initialViewState={{ longitude: 0, latitude: 15, zoom: 0.7 }}
            renderWorldCopies={false}
            maxBounds={[-179.99, -85, 179.99, 85]}
            maxZoom={6}
            dragRotate={false}
            touchPitch={false}
            mapStyle={EMPTY_STYLE}
            style={{ width: "100%", height: "100%" }}
            attributionControl={false}
            onIdle={(event) => {
              const map = event.target;
              if (map.getLayer("countries-fill") && map.isSourceLoaded("ne-countries")
                && map.queryRenderedFeatures({ layers: ["countries-fill"] }).length > 0) setBasemapReady(true);
            }}
            onError={(event) => {
              const message = event.error?.message || "浏览器地图资源不可用";
              console.error("intelligence-map", message);
              setRenderError(message);
            }}
            interactiveLayerIds={["events-circle", "event-clusters"]}
            onClick={async (event) => {
              const feature = event.features?.[0];
              if (!feature) return;
              if (feature.properties?.cluster_id != null) {
                const source = event.target.getSource("intelligence-events") as maplibregl.GeoJSONSource;
                try {
                  const leaves = await source.getClusterLeaves(Number(feature.properties.cluster_id), Number(feature.properties.point_count), 0);
                  setSelectedIds(leaves.map(leaf => String(leaf.properties?.event_id)));
                } catch {
                  setError("聚合事件读取失败，请重试地图。");
                }
              } else if (feature.properties?.event_id) props.onSelectEvent(String(feature.properties.event_id));
            }}

          >
            <NavigationControl showCompass={false} />
            <AttributionControl customAttribution="Natural Earth v5.1.2 (public domain)" compact />
            <Source id="ne-countries" type="geojson" data={COUNTRIES_GEOJSON}>
              <Layer
                id="countries-fill"
                type="fill"
                source="ne-countries"
                paint={{ "fill-color": "#1f2b45", "fill-outline-color": "#3b4a6b" }}
              />
            </Source>
            <Source
              id="intelligence-events"
              type="geojson"
              cluster clusterRadius={45} clusterMaxZoom={6}
              data={{ type: "FeatureCollection", features: collection.features }}
            />
            <Layer id="event-clusters" type="circle" source="intelligence-events"
              filter={["has", "point_count"]}
              paint={{ "circle-color": "#7c3aed", "circle-radius": ["step", ["get", "point_count"], 16, 10, 22, 50, 28], "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 }} />
            <Layer
              id="events-circle"
              filter={["!", ["has", "point_count"]]}
              type="circle"
              source="intelligence-events"
              paint={{
                "circle-radius": [
                  "interpolate",
                  ["linear"],
                  ["get", "relevance_score"],
                  60,
                  5,
                  100,
                  12,
                ],
                "circle-color": "#f0a020",
                "circle-stroke-color": "#ffffff",
                "circle-stroke-width": 1,
              }}
            />
          </Map></div>
          ) : null}
          {!basemapReady && !renderError ? <Spin aria-label="正在绘制地图底图" /> : null}
          {slowBasemap && !basemapReady && !renderError ? <Alert type="info" message="底图加载较慢，仍在等待资源" action={<Button onClick={retry}>重新加载底图</Button>} /> : null}
          <div className="review-map-caption" style={{ marginTop: 8 }}>
            <Text type="secondary">
              近 {windowHours} 小时发生的事件；发生时间未知时采用明确发布时间。橙点为单条事件，紫点为聚合，点击查看全部成员；可缩放展开。国家级位置仅为国家示意点。没有可信地点或明确时间的事件仍可在全球雷达查看。
            </Text>
          </div>
          {collection.applied_filters.truncated === "true" ? <Alert type="info" message="地图达到展示上限，完整事件请在全球雷达查看。" /> : null}
          <section aria-label="地图事件文本列表" style={{ marginTop: 8 }}>
            {selectedIds.length ? <Button onClick={() => setSelectedIds([])}>查看全部地图事件（当前聚合 {selectedIds.length} 条）</Button> : null}
            {collection.features.length === 0 ? <Empty description={collection.applied_filters.candidate_count != null
              ? `近${windowHours}小时有 ${collection.applied_filters.candidate_count} 条符合时间条件的候选事件，当前没有可在此范围展示的可信地点。地图只显示经核验的发生地；全部事件可在全球雷达查看。`
              : "当前没有带可信坐标的事件。无坐标事件仍可在全球雷达查看。"} /> : null}
            <ul>
              {collection.features.filter(feature => !selectedIds.length || selectedIds.includes(feature.properties.event_id)).map((feature) => (
                <li key={feature.id}>
                  <button
                    type="button"
                    onClick={() => props.onSelectEvent(feature.properties.event_id)}
                    style={{ background: "none", border: "none", color: "#175cd3", cursor: "pointer", padding: 0 }}
                  >
                    {feature.properties.title}
                  </button>
                  <Text type="secondary">
                    {" "}
                    — {categoryLabel(feature.properties.category)}，位置：{feature.properties.location_precision === "country_area" ? "国家示意点" : "来源地点"}
                    {feature.properties.cluster_count > 1
                      ? `，该网格聚合 ${feature.properties.cluster_count} 个事件`
                      : ""}
                  </Text>
                </li>
              ))}
            </ul>
          </section>
        </>
      ) : null}
    </div>
  );
}

export default IntelligenceMap;
