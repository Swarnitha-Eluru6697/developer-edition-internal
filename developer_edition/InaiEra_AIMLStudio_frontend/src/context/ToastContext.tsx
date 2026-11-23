import { createContext, useContext, useState, useCallback, ReactNode } from "react";
import { Icon } from "../components/Icons";

type Toast = { id: number; text: string; kind: "ok" | "err" };
const ToastContext = createContext<{ toast: (text: string, kind?: "ok" | "err") => void }>({ toast: () => {} });

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toast = useCallback((text: string, kind: "ok" | "err" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3600);
  }, []);
  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      <div className="toast-stack">
        {toasts.map((t) => (
          <div key={t.id} className={"toast" + (t.kind === "err" ? " err" : "")}>
            <Icon name={t.kind === "err" ? "alert" : "check"} />
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
