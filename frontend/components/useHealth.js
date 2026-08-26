"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getHealth } from "@/lib/api";

// CONTRACTS §4.1: poll GET /api/health every 10 s, plus once on mount.
export const HEALTH_POLL_MS = 10000;

/**
 * Health poller for AppShell / StatusPill / pages.
 * Returns {health: object|null, offline: bool, refresh: fn} — refresh() runs an
 * immediate out-of-band poll (additive to the CONTRACTS return shape).
 * On failure the last-known health object is kept (offline drives the UI);
 * the first success after an outage clears `offline`.
 *
 * The in-flight guard is scoped to the effect closure (NOT a shared ref):
 * under React StrictMode's dev double-mount, a shared ref let the first
 * mount's cancelled fetch block the second mount's tick, discarding the
 * mount-time response until the next 10 s interval (QA round1 MAJOR-1).
 * Per-mount scoping means every mount applies its own first response
 * immediately; the stale closure's `cancelled` flag still prevents
 * setState after cleanup.
 */
export function useHealth() {
  const [health, setHealth] = useState(null);
  const [offline, setOffline] = useState(false);
  const tickRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    let busy = false; // per-mount guard: never stack overlapping polls

    async function tick() {
      if (busy || cancelled) return;
      busy = true;
      try {
        const next = await getHealth();
        if (!cancelled) {
          setHealth(next);
          setOffline(false);
        }
      } catch {
        // Any failure here (code "offline" included) means: no usable health.
        if (!cancelled) setOffline(true);
      } finally {
        busy = false;
      }
    }

    tickRef.current = tick;
    tick();
    const id = setInterval(tick, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      if (tickRef.current === tick) tickRef.current = null;
      clearInterval(id);
    };
  }, []);

  const refresh = useCallback(() => {
    if (tickRef.current) tickRef.current();
  }, []);

  return { health, offline, refresh };
}
