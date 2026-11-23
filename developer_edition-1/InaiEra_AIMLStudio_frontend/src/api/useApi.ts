import { useEffect, useState, useCallback } from "react";

export function useApi<T>(fetcher: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    setData(null); // clear stale data from a previous fetch (e.g. a different
    // model) so a failed/loading fetch never leaves old data on screen
    // looking current — the Model Overview "switch to an unmonitored model"
    // case is exactly what this was silently getting wrong before.
    fetcher()
      .then((d) => setData(d))
      .catch((e) => setError(e instanceof Error ? e.message : "Something went wrong"))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    reload();
  }, [reload]);

  return { data, error, loading, reload };
}
