// useViewport — tracks window width so AppShell can switch layouts responsively
// without reading window.innerWidth during render (which would be stale).

import { useEffect, useState } from "react";

export function useViewport() {
  const [width, setWidth] = useState(() => (typeof window !== "undefined" ? window.innerWidth : 1280));
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return { width };
}
