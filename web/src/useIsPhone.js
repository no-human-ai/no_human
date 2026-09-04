import { useEffect, useState } from "react";

// Must stay byte-identical to styles.css's mobile breakpoint header
// (`@media (max-width: 640px)`) — see useIsPhone.test.mjs, which pins the
// two against each other so CSS and JS cannot silently drift apart.
export const PHONE_QUERY = "(max-width: 640px)";

// Default false (desktop) when matchMedia is unavailable — fail toward the
// known-good desktop layout, never toward the newer phone branch.
export function readIsPhone() {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(PHONE_QUERY).matches;
}

// Subscribes `onChange(matches)` to the media query and returns an
// unsubscribe function. Falls back to the deprecated addListener/
// removeListener pair for engines that lack addEventListener on a
// MediaQueryList; always cleans up.
export function subscribeIsPhone(onChange) {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(PHONE_QUERY);
  const listener = () => onChange(mql.matches);
  if (mql.addEventListener) mql.addEventListener("change", listener);
  else if (mql.addListener) mql.addListener(listener);
  return () => {
    if (mql.removeEventListener) mql.removeEventListener("change", listener);
    else if (mql.removeListener) mql.removeListener(listener);
  };
}

// Renders the AI-config nudge into a different container at phone width
// (App.jsx) instead of the old CSS re-lift, which anchored to a sidebar foot
// that sits outside the mobile nav's viewport.
export default function useIsPhone() {
  const [isPhone, setIsPhone] = useState(readIsPhone);
  useEffect(() => {
    // Re-read on mount in case matches changed between first paint and effect.
    setIsPhone(readIsPhone());
    return subscribeIsPhone(setIsPhone);
  }, []);
  return isPhone;
}
