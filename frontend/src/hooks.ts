import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

export function useFetch<T>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const load = useCallback(async (quiet = false) => {
    if (path === null) return;
    const mine = ++seq.current;
    if (!quiet) setLoading(true);
    try {
      const d = await api<T>(path);
      if (mine === seq.current) { setData(d); setError(null); }
    } catch (e) {
      if (mine === seq.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!refreshMs) return;
    const t = setInterval(() => void load(true), refreshMs);
    return () => clearInterval(t);
  }, [load, refreshMs]);

  return { data, error, loading, reload: () => load(true) };
}
