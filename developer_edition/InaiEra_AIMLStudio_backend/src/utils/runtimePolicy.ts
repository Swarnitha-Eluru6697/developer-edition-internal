export type ServingMode = "development" | "production";

function truthy(value: string | undefined): boolean {
  return /^(1|true|yes|on)$/i.test(value || "");
}

export function getServingMode(): ServingMode {
  const explicit = (process.env.SERVING_MODE || "").trim().toLowerCase();
  if (explicit === "production" || explicit === "prod" || explicit === "real") return "production";
  if (explicit === "development" || explicit === "dev" || explicit === "demo") return "development";
  return process.env.NODE_ENV === "production" ? "production" : "development";
}

export function isProductionServing(): boolean {
  return getServingMode() === "production";
}

export function requiresDurableArtifacts(): boolean {
  if (truthy(process.env.REQUIRE_DURABLE_ARTIFACTS)) return true;
  const explicit = (process.env.ARTIFACT_PERSISTENCE_MODE || "").trim().toLowerCase();
  if (explicit === "required" || explicit === "production" || explicit === "durable") return true;
  if (explicit === "optional" || explicit === "development" || explicit === "ephemeral") return false;
  return process.env.NODE_ENV === "production" || isProductionServing();
}
