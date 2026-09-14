// Locally-maintained UI primitives (shadcn-style, source owned by this repo).
// Minimal, accessible, no external dep beyond React. Recorded in
// THIRD_PARTY_NOTICES as a derivative of shadcn/ui's design approach.

import { type ButtonHTMLAttributes, type ReactNode, useEffect, useState } from "react";

export function Button({
  variant = "ghost",
  className = "",
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "danger";
}) {
  const v = variant === "primary" ? "primary" : variant === "danger" ? "danger" : "ghost";
  return (
    <button className={`btn ${v} ${className}`} {...rest}>
      {children}
    </button>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      className={`bg-panel2 r-radius ${className}`}
      style={{ animation: "pulse 1.4s ease-in-out infinite" }}
      aria-hidden
    />
  );
}

const pulse = document.createElement("style");
pulse.textContent = `@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }`;
if (!document.getElementById("bm-pulse-keyframes")) {
  pulse.id = "bm-pulse-keyframes";
  document.head.appendChild(pulse);
}

export function Progress({ value, label }: { value: number; label?: string }) {
  return (
    <div role="progressbar" aria-valuenow={Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <div className="bg-panel2 r-radius" style={{ height: 6, overflow: "hidden" }}>
        <div className="bg-accent" style={{ height: 6, width: `${Math.min(100, value * 100)}%`, transition: "width 0.3s" }} />
      </div>
    </div>
  );
}

export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  size = "sm",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.4)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
      }}
    >
      <div
        className={`dialog-panel dialog-panel--${size} bg-panel r-radius`}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ margin: "0 0 12px", fontSize: 16 }}>{title}</h3>
        <div style={{ marginBottom: 16 }}>{children}</div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>{footer}</div>
      </div>
    </div>
  );
}

export function Sheet({
  open,
  onClose,
  side,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  side: "left" | "right";
  title: string;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={onClose}
      style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", zIndex: 90 }}
    >
      <div
        className="bg-panel"
        onClick={(e) => e.stopPropagation()}
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          [side]: 0,
          width: "80%",
          maxWidth: 320,
          padding: 16,
          overflowY: "auto",
          borderRight: side === "left" ? "1px solid var(--border)" : undefined,
          borderLeft: side === "right" ? "1px solid var(--border)" : undefined,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>{title}</h3>
          <Button onClick={onClose} aria-label="关闭">✕</Button>
        </div>
        {children}
      </div>
    </div>
  );
}

// Lightweight toast — a single queued message rendered at the bottom.
let toastListeners: ((msg: string) => void)[] = [];
export function toast(msg: string) {
  toastListeners.forEach((l) => l(msg));
}
export function ToastHost() {
  const [msg, setMsg] = useState("");
  useEffect(() => {
    const l = (m: string) => {
      setMsg(m);
      setTimeout(() => setMsg(""), 3200);
    };
    toastListeners.push(l);
    return () => {
      toastListeners = toastListeners.filter((x) => x !== l);
    };
  }, []);
  if (!msg) return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="bg-panel r-radius"
      style={{
        position: "fixed",
        bottom: 20,
        left: "50%",
        transform: "translateX(-50%)",
        padding: "10px 18px",
        border: "1px solid var(--border)",
        boxShadow: "0 4px 16px rgba(0,0,0,0.12)",
        zIndex: 200,
        fontSize: 14,
      }}
    >
      {msg}
    </div>
  );
}
