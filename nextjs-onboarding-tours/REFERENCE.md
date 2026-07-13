# Code templates

Generalized from a production Payload CMS implementation. Adapt names, auth hook, and persistence to the target app. `APP` below is a brand prefix for CSS classes (e.g. `acme`).

## tours.ts — pure-data tour catalog

```typescript
export type TourStep = {
  /** CSS selector. Omitted = centered modal step (no highlight). */
  element?: string;
  title: string;
  description: string;
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "center" | "end";
};

export type TourDef = {
  id: string;
  /** Card title in the help center. */
  label: string;
  description: string;
  /** Canonical route used to relaunch the tour cross-route. */
  href: string;
  /** Pathname pattern that auto-starts this tour on first visit. */
  match: RegExp;
  steps: TourStep[];
};

export const TOURS: TourDef[] = [
  {
    id: "dashboard",
    label: "The dashboard",
    description: "What each card means and how the menu is organized.",
    href: "/admin",
    match: /^\/admin\/?$/,
    steps: [
      {
        element: '[data-tour="quick-actions"]',
        title: "What do you want to do today?",
        description: "Start here: these cards take you straight to the most common tasks.",
        side: "bottom",
      },
      // ... 3-8 steps
    ],
  },
  // one TourDef per area
];

export const findTourById = (id: string) => TOURS.find((t) => t.id === id);
export const findTourForPath = (p: string) => TOURS.find((t) => t.match.test(p));
```

## OnboardingProvider.tsx — the engine

Key skeleton; every block marked ⚠ encodes a debugged failure mode (see SKILL.md "Hard-won engine rules").

```tsx
"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { driver, type Driver } from "driver.js";
import "driver.js/dist/driver.css";
import { findTourById, findTourForPath, type TourDef } from "./tours";
// Auth: Payload → useAuth from "@payloadcms/ui"; NextAuth → useSession; else custom.

type OnboardingState = {
  welcomeCompletedAt?: string | null;
  toursCompleted?: string[] | null;
  hideHints?: boolean | null;
};

export default function OnboardingProvider({ children }: { children?: React.ReactNode }) {
  const { user } = useAuth();
  const pathname = usePathname() ?? "";
  const router = useRouter();
  const userId = user?.id;

  // ⚠ Hydrate once per login: state + ref (ref keeps async persists complete).
  const [state, setState] = useState<OnboardingState | null>(null);
  const stateRef = useRef<OnboardingState>({});
  const hydratedFor = useRef<string | number | null>(null);
  useEffect(() => {
    if (!userId || hydratedFor.current === userId) return;
    hydratedFor.current = userId;
    const o = (user as any)?.onboarding;
    const initial = {
      welcomeCompletedAt: o?.welcomeCompletedAt ?? null,
      toursCompleted: Array.isArray(o?.toursCompleted) ? o.toursCompleted : [],
      hideHints: o?.hideHints ?? false,
    };
    stateRef.current = initial;
    setState(initial);
  }, [userId, user]);

  // Optimistic, fire-and-forget persist of the FULL object.
  const persist = useCallback((patch: Partial<OnboardingState>) => {
    const next = { ...stateRef.current, ...patch };
    stateRef.current = next;
    setState(next);
    if (!userId) return;
    void fetch(`/api/users/${userId}`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ onboarding: next }),
    }).catch(() => {}); // non-critical: worst case it shows again next session
  }, [userId]);

  const driverRef = useRef<Driver | null>(null);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const markTourSeen = useCallback((id: string) => {
    const done = stateRef.current.toursCompleted ?? [];
    if (!done.includes(id)) persist({ toursCompleted: [...done, id] });
  }, [persist]);

  const runTour = useCallback((tour: TourDef) => {
    if (retryTimer.current) clearTimeout(retryTimer.current);
    let attempts = 0;
    const tryStart = () => {
      // ⚠ Filter steps whose anchor is missing or hidden right now.
      const steps = tour.steps.filter((s) => {
        if (!s.element) return true;
        const el = document.querySelector<HTMLElement>(s.element);
        return Boolean(el && el.getClientRects().length > 0);
      });
      // ⚠ Client-hydrated forms mount late — retry before giving up.
      if (steps.filter((s) => s.element).length === 0) {
        if (attempts < 8) {
          attempts += 1;
          retryTimer.current = setTimeout(tryStart, 500);
        }
        return;
      }
      driverRef.current?.destroy();
      const d = driver({
        showProgress: steps.length > 1,
        progressText: "{{current}} of {{total}}",
        nextBtnText: "Next",
        prevBtnText: "Back",
        doneBtnText: "Got it",
        popoverClass: "APP-tour",
        overlayOpacity: 0.55,
        stagePadding: 6,
        stageRadius: 10,
        // ⚠ Closing early counts as seen — never re-nag.
        onDestroyed: () => markTourSeen(tour.id),
        steps: steps.map((s) => ({
          element: s.element,
          popover: { title: s.title, description: s.description, side: s.side, align: s.align },
        })),
      });
      driverRef.current = d;
      d.drive();
    };
    // ⚠ 600ms initial delay so late-mounting steps (rich text editors)
    //   aren't filtered out of an otherwise-startable tour.
    retryTimer.current = setTimeout(tryStart, 600);
  }, [markTourSeen]);

  const startTour = useCallback((id: string) => {
    const tour = findTourById(id);
    if (!tour) return;
    if (tour.match.test(pathname)) runTour(tour);
    else router.push(`${tour.href}?tour=${tour.id}`); // ⚠ cross-route relaunch
  }, [pathname, router, runTour]);

  // Wizard
  const [wizardOpen, setWizardOpen] = useState(false);
  const closeWizard = useCallback(() => {
    setWizardOpen(false);
    persist({ welcomeCompletedAt: new Date().toISOString() });
  }, [persist]);
  const pickWizardAction = useCallback((href: string) => {
    setWizardOpen(false);
    persist({ welcomeCompletedAt: new Date().toISOString() });
    router.push(href);
  }, [persist, router]);
  useEffect(() => {
    if (userId && state && !state.welcomeCompletedAt) setWizardOpen(true);
  }, [userId, state]);

  // Route effects: ?wizard / ?tour params + first-visit auto-start.
  useEffect(() => {
    if (!userId || !state) return;
    const search = new URLSearchParams(window.location.search);

    if (search.get("wizard")) {
      setWizardOpen(true);
      search.delete("wizard"); // ⚠ strip so refresh doesn't re-open
      router.replace(`${pathname}${search.toString() ? `?${search}` : ""}`, { scroll: false });
      return;
    }
    const forcedId = search.get("tour");
    if (forcedId) {
      const tour = findTourById(forcedId);
      if (tour && tour.match.test(pathname)) {
        search.delete("tour"); // ⚠ strip so refresh doesn't restart
        router.replace(`${pathname}${search.toString() ? `?${search}` : ""}`, { scroll: false });
        runTour(tour);
      }
      return;
    }
    // ⚠ Auto-start gates: wizard done, hints not muted, wide enough viewport.
    if (!state.welcomeCompletedAt || state.hideHints) return;
    if (window.innerWidth < 900) return;
    const tour = findTourForPath(pathname);
    if (!tour || (state.toursCompleted ?? []).includes(tour.id)) return;
    runTour(tour);

    return () => { if (retryTimer.current) clearTimeout(retryTimer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname, userId, state === null]);

  // ⚠ Tear down active tour when navigating away.
  useEffect(() => () => { driverRef.current?.destroy(); driverRef.current = null; }, [pathname]);

  const currentTour = useMemo(() => findTourForPath(pathname) ?? null, [pathname]);

  return (
    <OnboardingContext.Provider value={{ currentTour, startTour, openWizard: () => setWizardOpen(true) }}>
      {children}
      {user && (
        <>
          {wizardOpen && (
            <WelcomeWizard userName={(user as any).name} onClose={closeWizard} onPickAction={pickWizardAction} />
          )}
          <HelpButton
            hasTourHere={Boolean(currentTour)}
            onStartTour={() => currentTour && runTour(currentTour)}
            onOpenWizard={() => setWizardOpen(true)}
          />
        </>
      )}
    </OnboardingContext.Provider>
  );
}
```

Mounting: Payload → `admin.components.providers: ["@/components/admin/onboarding/OnboardingProvider"]` in `payload.config.ts` + `pnpm generate:importmap`. Plain Next.js → wrap children in the relevant `layout.tsx`.

## Persistence fields (Payload Users collection example)

```typescript
{
  name: "onboarding",
  type: "group",
  admin: {
    position: "sidebar",
    // Visible only to admins so they can reset someone's onboarding.
    // Editors still WRITE via API — the condition only hides the UI.
    condition: (_d, _s, { user }) => (user as { role?: string } | null)?.role === "admin",
    description: "Wizard + tour state. Clear the date to re-show the welcome.",
  },
  fields: [
    { name: "welcomeCompletedAt", type: "date" },
    { name: "toursCompleted", type: "json" },   // string[] of tour ids
    { name: "hideHints", type: "checkbox", defaultValue: false },
  ],
}
```

localStorage fallback: same shape under one key, swap `persist` for `localStorage.setItem`.

## quickActions.ts — shared by wizard step 4 + dashboard

```typescript
import type { LucideIcon } from "lucide-react";

export type QuickAction = {
  id: string;
  title: string;        // task-oriented: "Publish a news article"
  description: string;  // one line
  href: string;         // deep link
  icon: LucideIcon;
  tourId?: string;      // optional contextual tour on arrival
};

export const QUICK_ACTIONS: QuickAction[] = [ /* 5-6 entries */ ];
```

## WelcomeWizard.tsx — structure

A fixed overlay + card with: close X (top-right), step body, footer with skip link ("Explore on my own"), progress dots, Back/Next buttons (last step: "Start"). Each step rendered through a shared shell:

```tsx
function StepShell({ icon, eyebrow, title, children }) {
  return (
    <div className="APP-wizard__step">
      <div className="APP-wizard__step-head">
        <span className="APP-wizard__step-icon">{icon}</span>
        <span className="APP-wizard__eyebrow">{eyebrow}</span>
      </div>
      <h2 className="APP-wizard__title">{title}</h2>
      <div className="APP-wizard__content">{children}</div>
    </div>
  );
}
```

Step content pattern: (0) welcome + what this app replaces, personalized with first name; (1) annotated list of the navigation sections, each with an icon; (2) the app's key differentiating concept; (3) `QUICK_ACTIONS` as buttons calling `onPickAction(a.href)`.

## HelpButton.tsx — FAB

`div.APP-help-fab[data-tour="help-fab"]` fixed bottom-right containing a toggle button (`?` / `X` icons) and, when open, a small menu: "Tour of this screen" (only if `hasTourHere`), link to the help center, "See the welcome again". Close the menu on outside mousedown and on pathname change. Give the FAB its own `data-tour` so the dashboard tour can end by pointing at it ("help is always here").

## Help center view

A static route (`/admin/ayuda`, `/help`, …) rendering:

- A card per `TOURS` entry with a "Relaunch" link to `{tour.href}?tour={tour.id}`, plus one card linking to `?wizard=1`.
- Collapsible step-by-step guides from a typed `guides.ts` (`{ id, title, intro, steps: string[], cta?: { label, href } }`) — `<details>/<summary>` is enough.
- An escalation box: who to contact when something breaks.

## CSS skeleton (adapt tokens to the brand)

```scss
/* driver.js popover — popoverClass: "APP-tour" */
.driver-popover.APP-tour {
  background: #fff; color: var(--text); border-radius: 0.75rem;
  box-shadow: 0 0 0 1px rgb(0 0 0 / 8%), 0 16px 48px rgb(0 0 0 / 35%);
  max-width: 23rem; font-family: var(--font-body);
  .driver-popover-title { font-weight: 700; color: var(--brand-dark); }
  .driver-popover-description { font-size: 0.9375rem; line-height: 1.55; }
  .driver-popover-navigation-btns {
    button { border-radius: 0.5rem; font-weight: 600; }
    .driver-popover-next-btn { background: var(--brand); color: #fff; }
  }
}

/* Wizard overlay: brand-tinted radial + dark scrim + blur; card rises in. */
.APP-wizard__overlay {
  position: fixed; inset: 0; z-index: 600; /* above the app's chrome */
  display: flex; align-items: center; justify-content: center; padding: 1.5rem;
  background: radial-gradient(1200px 700px at 20% 0%, var(--brand-tint), transparent 60%),
    rgb(0 0 0 / 78%);
  backdrop-filter: blur(4px);
}
.APP-wizard {
  width: 100%; max-width: 38rem; max-height: calc(100vh - 3rem);
  overflow-y: auto; background: #fff; border-radius: 1rem;
}

/* FAB: fixed bottom-right circle, menu pops above it. */
.APP-help-fab { position: fixed; right: 1.25rem; bottom: 1.25rem; z-index: 500; }
```

Add dark-mode overrides for the popover and cards if the app supports a dark theme.

## Verification snippets (browser automation)

Step through a running tour and collect titles:

```javascript
new Promise(res => {
  const titles = [];
  const step = () => {
    titles.push(document.querySelector('.driver-popover-title')?.textContent);
    const btn = document.querySelector('.driver-popover-next-btn');
    if (!btn || titles.length > 12) return res(JSON.stringify(titles));
    btn.click(); setTimeout(step, 500);
  };
  step();
})
```

Check wizard/tour presence after navigation (allow hydration time):

```javascript
new Promise(r => setTimeout(() => r(JSON.stringify({
  wizard: !!document.querySelector('.APP-wizard'),
  tour: !!document.querySelector('.driver-popover'),
})), 2500))
```

Embedded test browsers are often narrower than the 900px auto-start gate — widen with CDP `Emulation.setDeviceMetricsOverride` (e.g. 1280×800) before testing auto-start, and `Emulation.clearDeviceMetricsOverride` when done.
