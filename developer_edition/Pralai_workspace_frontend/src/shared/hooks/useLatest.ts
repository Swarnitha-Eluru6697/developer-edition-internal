import { useEffect, useRef } from "react";

/**
 * A ref that always holds the latest `value`.
 *
 * For long-lived subscriptions (window listeners, document listeners) that
 * must call the *current* handler without re-subscribing on every render —
 * the effect depends on the ref (stable), and reads `.current` at event time.
 */
export function useLatest<T>(value: T) {
  const ref = useRef(value);
  useEffect(() => {
    ref.current = value;
  });
  return ref;
}
